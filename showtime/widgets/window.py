# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo

import json
from collections.abc import Callable, Sequence
from math import sqrt
from time import time
from typing import TYPE_CHECKING, Any, cast

from gi.repository import (
    Adw,
    Gdk,
    Gio,
    GLib,
    GObject,
    Gtk,
    Pango,
)

import showtime
from showtime import (
    APP_ID,
    PREFIX,
    PROFILE,
    VERSION,
    log_file,
    logger,
    state_settings,
    system,
)
from showtime.play import Playback
from showtime.util.connection import Connection
from showtime.util.source import SourceId
from showtime.utils import (
    get_file_hash_identifier,
    get_subtitle_font_desc,
    get_title,
    lookup_action,
    nanoseconds_to_timestamp,
    screenshot,
)

from .drag_overlay import DragOverlay
from .options import Options
from .sound_options import SoundOptions

if TYPE_CHECKING:
    from showtime.main import Application

# For large enough monitors, occupy 40% of the screen area
# when opening a window with a video
DEFAULT_OCCUPY_SCREEN = 0.4

# Screens with this resolution or smaller are handled as small
SMALL_SCREEN_AREA = 1280 * 1024

# For small monitors, occupy 80% of the screen area
SMALL_OCCUPY_SCREEN = 0.8

SMALL_SIZE_CHANGE = 10

# So that seeking isn't too rough
SCALE_MULT = 500

MINUTE_IN_NS = 6e10


@Gtk.Template.from_resource(f"{PREFIX}/gtk/window.ui")
class Window(Adw.ApplicationWindow):
    """The main application window."""

    __gtype_name__ = "Window"

    drag_overlay: DragOverlay = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    stack: Gtk.Stack = Gtk.Template.Child()

    placeholder_page: Adw.ToolbarView = Gtk.Template.Child()
    placeholder_stack: Gtk.Stack = Gtk.Template.Child()
    placeholder_primary_menu_button: Gtk.MenuButton = Gtk.Template.Child()
    error_status_page: Adw.StatusPage = Gtk.Template.Child()

    video_page: Gtk.WindowHandle = Gtk.Template.Child()
    overlay_motion: Gtk.EventControllerMotion = Gtk.Template.Child()
    video: Gtk.GLArea = Gtk.Template.Child()

    header_handle_start: Gtk.WindowHandle = Gtk.Template.Child()
    header_handle_end: Gtk.WindowHandle = Gtk.Template.Child()
    header_start: Gtk.Box = Gtk.Template.Child()
    header_end: Gtk.Box = Gtk.Template.Child()
    video_primary_menu_button: Gtk.MenuButton = Gtk.Template.Child()

    shade: Adw.Bin = Gtk.Template.Child()
    toolbar_clamp: Adw.Clamp = Gtk.Template.Child()
    controls_box: Gtk.Box = Gtk.Template.Child()
    bottom_overlay_box: Gtk.Box = Gtk.Template.Child()

    title_label: Gtk.Label = Gtk.Template.Child()
    play_button: Gtk.Button = Gtk.Template.Child()
    position_label: Gtk.Label = Gtk.Template.Child()
    seek_scale: Gtk.Scale = Gtk.Template.Child()
    timestamp_box: Gtk.Box = Gtk.Template.Child()
    end_timestamp_button: Gtk.Button = Gtk.Template.Child()

    sound_options: SoundOptions = Gtk.Template.Child()

    options: Options = Gtk.Template.Child()

    spinner: Adw.Spinner = Gtk.Template.Child()  # pyright: ignore[reportAttributeAccessIssue]
    restore_breakpoint_bin: Adw.BreakpointBin = Gtk.Template.Child()
    restore_box: Gtk.Box = Gtk.Template.Child()

    open_video_dialog: Gtk.FileDialog = Gtk.Template.Child()
    choose_subtitles_dialog: Gtk.FileDialog = Gtk.Template.Child()

    overlay_motions: set[Gtk.EventControllerMotion]
    overlay_menu_buttons: set[Gtk.MenuButton]

    stopped: bool = True
    buffering: bool = False

    volume = GObject.Property(type=float)

    media_info_updated = GObject.Signal(name="media-info-updated")
    volume_changed = GObject.Signal(name="volume-changed")
    rate_changed = GObject.Signal(name="rate-changed")
    seeked = GObject.Signal(name="seeked")

    _reveal_animations: dict[Gtk.Widget, Adw.Animation]
    _hide_animations: dict[Gtk.Widget, Adw.Animation]

    _playing_gfile: Gio.File | None = None

    _last_reveal: float = 0.0

    _paused: bool = True
    _seek_paused: bool = False
    _prev_motion_xy: tuple[float | None, float | None] = (0, 0)
    _prev_volume = -1
    _toplevel_focused: bool = False

    @GObject.Property(type=bool, default=False)
    def mute(self) -> bool:
        """Get the mute state."""
        return self.play.mute

    @mute.setter
    def mute(self, mute: bool) -> None:
        self.play.mute = mute

    @GObject.Property(type=str)
    def rate(self) -> str:
        """Get the playback rate."""
        return str(self.play.rate)

    @rate.setter
    def rate(self, rate: str) -> None:
        self.play.rate = float(rate)
        self.options.popover.popdown()

    def _step_rate(self, step: int) -> None:
        rates = ("0.5", "1.0", "1.25", "1.5", "2.0")
        current = self.rate if self.rate in rates else "1.0"
        index = rates.index(current)

        self.rate = rates[max(0, min(index + step, len(rates) - 1))]

    @GObject.Property(type=bool, default=True)
    def paused(self) -> bool:
        """Whether the video is currently paused."""
        return self._paused

    @paused.setter
    def paused(self, paused: bool) -> None:
        if self._paused == paused:
            return

        self._paused = paused

        self.play_button.update_property(
            (Gtk.AccessibleProperty.LABEL,),
            (_("Play") if paused else _("Pause"),),
        )
        self.play_button.props.icon_name = (
            "media-playback-start-symbolic"
            if paused
            else "media-playback-pause-symbolic"
        )

        if not (app := self.props.application):
            return

        (app.uninhibit_win if paused else app.inhibit_win)(self)  # pyright: ignore[reportAttributeAccessIssue]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(decorated=system != "Darwin", **kwargs)

        Gtk.WindowGroup().add_window(self)

        self._reveal_animations = {}
        self._hide_animations = {}

        if system == "Darwin":
            self.header_start.props.visible = False

        self._video_paintable = Gtk.WidgetPaintable.new(self.video)
        self.play = Playback(self.video)
        self._play_connections = [
            Connection("changed", self._on_playback_changed, self.play),
            Connection("loaded", self._on_loaded, self.play),
            Connection("seek-done", self._on_seek_done, self.play),
            Connection("end-of-stream", self._on_end_of_stream, self.play),
            Connection("error", self._on_error, self.play),
        ]
        self._loop_connection = Connection("changed::looping", self._on_loop_changed, state_settings)
        self.play.set_looping(state_settings.get_boolean("looping"))
        self._initial_size = True

        self._state_settings_connection = Connection(
            "changed::end-timestamp-type",
            self._on_end_timestamp_type_changed,
            state_settings,
        )

        self._update_subtitle_font_desc()
        self._font_connection = Connection(
            "notify::gtk-xft-dpi",
            lambda *_: self._update_subtitle_font_desc(),
            Gtk.Settings.get_default(),
        )

        # Pending timeouts, removed in teardown
        self._resize_source = SourceId()
        self._hide_overlays_source = SourceId()
        self._buffering_source = SourceId()
        self._seeking_source = SourceId()

        if PROFILE == "development":
            self.add_css_class("devel")

        self.overlay_motions = set()
        for widget in (
            self.controls_box,
            self.bottom_overlay_box,
            self.header_start,
            self.header_end,
            self.restore_box,
        ):
            widget.add_controller(motion := Gtk.EventControllerMotion())
            self.overlay_motions.add(motion)

        self.overlay_widgets = {
            self.shade,
            self.toolbar_clamp,
            self.header_handle_start,
            self.header_handle_end,
        }

        self.overlay_menu_buttons = {
            self.video_primary_menu_button,
            self.options.menu_button,
            self.sound_options.menu_button,
        }

        # Force playback controls and progress bar to be Left-to-Right
        for widget in (
            self.controls_box,
            self.seek_scale,
            self.timestamp_box,
            self.position_label,
            self.end_timestamp_button,
        ):
            widget.set_direction(Gtk.TextDirection.LTR)

        self._create_actions()
        self._on_volume_changed()
        self.notify("rate")

    def play_video(self, gfile: Gio.File) -> None:
        """Start playing the given `GFile`."""
        if not (app := self.props.application):
            return

        app.save_play_position(self)  # pyright: ignore[reportAttributeAccessIssue]

        self._playing_gfile = gfile

        logger.debug("Playing video: %s", gfile.get_uri())
        self.stack.props.visible_child = self.video_page
        self.stopped = True
        self.paused = True
        self.title_label.props.label = ""
        self._seeking_source.remove()
        self._resize_source.remove()
        self._hide_overlay(self.restore_breakpoint_bin)
        self._reveal_overlay(self.controls_box)
        self.options.build_menus([])
        self.seek_scale.set_sensitive(False)
        self.play.open(gfile)
        self._on_position_updated()
        self.emit("media-info-updated")
        self._on_motion()

        if action := lookup_action(self, "screenshot"):
            action.props.enabled = False

        if action := lookup_action(self, "show-in-files"):
            action.props.enabled = gfile.get_path() is not None

    def unpause(self) -> None:
        """Start playing the current video."""
        if not self.play.ready:
            return
        self._hide_overlay(self.restore_breakpoint_bin)
        self._reveal_overlay(self.controls_box)
        self.play.play()
        logger.debug("Video unpaused")

    def pause(self, *_args) -> None:
        """Pause the currently playing video."""
        self.play.pause()
        logger.debug("Video paused")

    def teardown_playback(self) -> None:
        """Stop playback and clear all references to self."""
        if self.play is None:
            return
        for connection in (
            self._state_settings_connection,
            self._font_connection,
            self._loop_connection,
            *self._play_connections,
        ):
            connection.disconnect()

        for source in (
            self._resize_source,
            self._hide_overlays_source,
            self._buffering_source,
            self._seeking_source,
        ):
            source.remove()

        for name in self.list_actions():
            self.remove_action(name)

        self._video_paintable.set_widget(None)
        self.play.close()
        self.play = None

    @Gtk.Template.Callback()
    def _cycle_end_timestamp_type(self, *_args) -> None:
        state_settings.set_enum(
            "end-timestamp-type",
            int(not showtime.end_timestamp_type),
        )

        self._set_end_timestamp_label(
            self.play.position, self.play.duration
        )

    @Gtk.Template.Callback()
    def _resume(self, *_args) -> None:
        self.unpause()

    @Gtk.Template.Callback()
    def _play_again(self, *_args) -> None:
        self.play.seek(0)
        self.unpause()

    def _rotate_left(self) -> None:
        self.play.rotate(-90)

    def _rotate_right(self) -> None:
        self.play.rotate(90)

    @Gtk.Template.Callback()
    def _on_drop(self, _target, gfile: Gio.File, _x, _y) -> None:
        self.play_video(gfile)

    def _get_previous_play_position(self) -> float | None:
        if not (uri := self.play.uri):
            return None

        try:
            with (showtime.state_path / "playback_history.json").open() as hist_file:
                return json.load(hist_file).get(get_file_hash_identifier(uri))
        except (OSError, ValueError) as error:
            logger.debug("Cannot restore play position: %s", error)
            return None

    def _resize_window(self) -> None:
        video_width, video_height = self.play.video_size
        if not (video_width and video_height):
            return
        if self.props.fullscreened or self.props.maximized:
            return
        initial, self._initial_size = self._initial_size, False
        logger.debug("Resizing window…")

        if not (surface := self.get_surface()):
            logger.error("Could not get GdkSurface to resize window")
            return

        if not (monitor := self.props.display.get_monitor_at_surface(surface)):
            logger.error("Could not get GdkMonitor to resize window")
            return

        video_area = video_width * video_height
        init_width, init_height = self.get_default_size()

        if initial:
            # Algorithm copied from Loupe
            # https://gitlab.gnome.org/GNOME/loupe/-/blob/4ca5f9e03d18667db5d72325597cebc02887777a/src/widgets/image/rendering.rs#L151

            hidpi_scale = surface.props.scale_factor

            monitor_rect = monitor.props.geometry

            monitor_width = monitor_rect.width
            monitor_height = monitor_rect.height

            monitor_area = monitor_width * monitor_height
            logical_monitor_area = monitor_area * pow(hidpi_scale, 2)

            occupy_area_factor = (
                SMALL_OCCUPY_SCREEN
                if logical_monitor_area <= SMALL_SCREEN_AREA
                else DEFAULT_OCCUPY_SCREEN
            )

            size_scale = sqrt(monitor_area / video_area * occupy_area_factor)

            target_scale = min(1, size_scale)
            nat_width = video_width * target_scale
            nat_height = video_height * target_scale

            max_width = monitor_width - 20
            if nat_width > max_width:
                nat_width = max_width
                nat_height = video_height * nat_width / video_width

            max_height = monitor_height - (50 + 35 + 20) * hidpi_scale
            if nat_height > max_height:
                nat_height = max_height
                nat_width = video_width * nat_height / video_height

        else:
            prev_area = init_width * init_height

            if video_width > video_height:
                ratio = video_width / video_height
                nat_width = int(sqrt(prev_area * ratio))
                nat_height = int(nat_width / ratio)
            else:
                ratio = video_height / video_width
                nat_width = int(sqrt(prev_area / ratio))
                nat_height = int(nat_width * ratio)

            if (abs(init_width - nat_width) < SMALL_SIZE_CHANGE) and (
                abs(init_height - nat_height) < SMALL_SIZE_CHANGE
            ):
                return

        nat_width = round(nat_width)
        nat_height = round(nat_height)

        for prop, init, target in (
            ("default-width", init_width, nat_width),
            ("default-height", init_height, nat_height),
        ):
            anim = Adw.TimedAnimation.new(
                self, init, target, 500, Adw.PropertyAnimationTarget.new(self, prop)
            )
            anim.props.easing = Adw.Easing.EASE_OUT_EXPO
            (anim.skip if initial else anim.play)()
            logger.debug("Resized window to %ix%i", nat_width, nat_height)

    def _on_loaded(self, _play) -> None:
        self.stopped = False
        self.notify("paused")
        self._on_playback_state_changed()
        self._on_media_info_updated()
        if (pos := self._get_previous_play_position()) and pos >= MINUTE_IN_NS:
            self._reveal_overlay(self.restore_breakpoint_bin)
            self._hide_overlay(self.controls_box)
            self.play.seek(pos)
        else:
            self.unpause()

    def _update_subtitle_font_desc(self) -> None:
        if font := get_subtitle_font_desc():
            desc = Pango.FontDescription.from_string(font)
            if desc.get_family() and desc.get_size():
                self.play.set_subtitle_font(desc.get_family(), desc.get_size() / Pango.SCALE)

    def _on_end_timestamp_type_changed(self, *_args) -> None:
        showtime.end_timestamp_type = state_settings.get_enum("end-timestamp-type")
        self._set_end_timestamp_label(
            self.play.position, self.play.duration
        )

    def _set_end_timestamp_label(self, pos: int, dur: int) -> None:
        match showtime.end_timestamp_type:
            case 0:  # Duration
                self.end_timestamp_button.props.label = nanoseconds_to_timestamp(dur)
            case 1:  # Remaining
                self.end_timestamp_button.props.label = "-" + nanoseconds_to_timestamp(
                    max(dur - pos, 0)
                )

    @Gtk.Template.Callback()
    def _schedule_volume_change(self, _obj, value: float) -> None:
        if self.play is not None and value != self.play.volume:
            self.play.set_volume(value)

    def _set_overlay_revealed(self, widget: Gtk.Widget, reveal: bool) -> None:
        animations = self._reveal_animations if reveal else self._hide_animations
        animation = animations.get(widget)

        if animation and (animation.props.state == Adw.AnimationState.PLAYING):
            return

        animations[widget] = Adw.TimedAnimation.new(
            widget,
            widget.props.opacity,
            int(reveal),
            250,
            Adw.PropertyAnimationTarget.new(widget, "opacity"),
        )

        widget.props.can_target = reveal
        animations[widget].play()

    def _reveal_overlay(self, widget: Gtk.Widget) -> None:
        self._set_overlay_revealed(widget, True)

    def _hide_overlay(self, widget: Gtk.Widget) -> None:
        self._set_overlay_revealed(widget, False)

    def _hide_overlays(self, timestamp: float) -> None:
        if (
            # Cursor moved
            timestamp != self._last_reveal
            # Cursor is hovering controls
            or any(motion.props.contains_pointer for motion in self.overlay_motions)
            # Active popover
            or any(button.props.active for button in self.overlay_menu_buttons)
            # Active restore buttons
            or self.restore_breakpoint_bin.props.can_target
        ):
            return

        for widget in self.overlay_widgets:
            self._hide_overlay(widget)

        if self.overlay_motion.contains_pointer():
            self.set_cursor_from_name("none")

    def do_realize(self) -> None:
        Adw.ApplicationWindow.do_realize(self)

        if not (surface := self.get_surface()):
            return

        if not isinstance(surface, Gdk.Toplevel):
            return

        surface.connect("notify::state", self._on_toplevel_state_changed)

    def do_move_focus(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, direction: Gtk.DirectionType
    ) -> None:
        self._on_motion()
        Adw.ApplicationWindow.do_move_focus(self, direction)

    def _on_toplevel_state_changed(self, toplevel: Gdk.Toplevel, *_args) -> None:
        if (
            focused := toplevel.get_state() & Gdk.ToplevelState.FOCUSED
        ) == self._toplevel_focused:
            return

        if not focused:
            self._hide_overlays(self._last_reveal)

        self._toplevel_focused = bool(focused)

    @Gtk.Template.Callback()
    def _on_motion(
        self, _obj=None, x: float | None = None, y: float | None = None
    ) -> None:
        if None not in (x, y):
            if (x, y) == self._prev_motion_xy:
                return

            self._prev_motion_xy = (x, y)

        self.set_cursor_from_name(None)

        for widget in self.overlay_widgets:
            self._reveal_overlay(widget)

        self._last_reveal = time()
        self._hide_overlays_source.add_timeout(
            GLib.PRIORITY_DEFAULT,
            2000,
            lambda: self._hide_overlays(self._last_reveal),
        )

    @Gtk.Template.Callback()
    def _on_pointer_left(self, *_args) -> None:
        self._prev_motion_xy = (None, None)
        self._hide_overlays(self._last_reveal)

    def _on_playback_changed(self, _play, name: str) -> None:
        match name:
            case "loading" | "pause" | "paused-for-cache":
                self._on_playback_state_changed()
            case "duration" | "seekable":
                self._on_position_updated()
                self.emit("media-info-updated")
            case "time-pos":
                self._on_position_updated()
            case "media-title" | "track-list":
                self._on_media_info_updated()
            case "video-out-params":
                self._resize_source.add_timeout(GLib.PRIORITY_DEFAULT, 100, self._resize_window)
                if action := lookup_action(self, "screenshot"):
                    action.props.enabled = all(self.play.video_size)
            case "volume":
                self._on_volume_changed()
            case "speed":
                self.notify("rate")
                self.emit("rate-changed")
            case "mute":
                self.notify("mute")

    def _on_playback_state_changed(self) -> None:
        self.buffering = self.play.buffering
        if self.buffering:
            self._buffering_source.ensure_timeout(
                GLib.PRIORITY_DEFAULT,
                1000,
                lambda: self._reveal_overlay(self.spinner),
            )
        else:
            self._buffering_source.remove()
            self._hide_overlay(self.spinner)
        paused = not self.play.ready or self.play.paused
        if not paused:
            self.stopped = False
        if paused != self.paused:
            self.paused = paused

    def _on_position_updated(self) -> None:
        pos, dur = self.play.position, self.play.duration
        self.seek_scale.set_sensitive(bool(dur and self.play.seekable))
        self.seek_scale.set_value(pos / dur * SCALE_MULT if dur else 0)
        self.position_label.props.label = nanoseconds_to_timestamp(pos)
        self._set_end_timestamp_label(pos, dur)

    @Gtk.Template.Callback()
    def _seek(self, _obj, _scroll, val: float) -> None:
        if not self.play.seekable or not self.play.duration:
            return
        if not self._seeking_source.added:
            self._seek_paused = self.paused

        if not self.paused:
            self.pause()

        self.play.seek(max(self.play.duration * (val / SCALE_MULT), 0))

        def post_seek() -> None:
            if not self._seek_paused:
                self.unpause()

        self._seeking_source.add_timeout(GLib.PRIORITY_DEFAULT, 250, post_seek)

    def _on_seek_done(self, _play) -> None:
        self._on_position_updated()
        self.emit("seeked")

    def _on_media_info_updated(self) -> None:
        self.title_label.props.label = get_title(self.play.title, self.play.uri) or ""
        self.options.build_menus(self.play.tracks)
        self.emit("media-info-updated")

    def _on_volume_changed(self) -> None:
        vol = self.play.volume
        if self._prev_volume != vol:
            self._prev_volume = vol
            self.volume = vol
            self.sound_options.adjustment.props.value = vol
            self.emit("volume-changed")

    def _on_end_of_stream(self, _play) -> None:
        if not state_settings.get_boolean("looping"):
            self.pause()
        if self.play.seekable:
            self.play.seek(0)

    def _on_error(self, _play, error: str) -> None:
        logger.error("%s", error)
        if action := lookup_action(self, "screenshot"):
            action.props.enabled = False
        self.stopped = True
        self._on_playback_state_changed()
        self.notify("paused")
        self.emit("media-info-updated")

        def copy_details(*_args) -> None:
            if not (display := Gdk.Display.get_default()):
                return

            display.get_clipboard().set(error)

            self.toast_overlay.add_toast(Adw.Toast.new(_("Details copied")))

        copy = Adw.ButtonRow(title=_("Copy Technical Details"))
        copy.connect("activated", copy_details)

        retry = Adw.ButtonRow(title=_("Try Again"))
        retry.add_css_class("suggested-action")
        retry.connect("activated", self._try_again)

        group = Adw.PreferencesGroup(
            halign=Gtk.Align.CENTER,
            width_request=250,
            separate_rows=True,
        )
        group.add(retry)
        group.add(copy)

        self.error_status_page.props.child = group

        self.placeholder_stack.props.visible_child = self.error_status_page
        self.stack.props.visible_child = self.placeholder_page

    @Gtk.Template.Callback()
    def _on_primary_click_released(self, gesture: Gtk.Gesture, n: int, *_args) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._on_motion()

        if not n % 2:
            self.props.fullscreened = not self.props.fullscreened

    @Gtk.Template.Callback()
    def _on_secondary_click_pressed(
        self, gesture: Gtk.Gesture, _n, x: int, y: int
    ) -> None:
        self.options.on_secondary_click_pressed(self, gesture, x, y)

    def _try_again(self, *_args) -> None:
        if not (app := self.props.application):
            return

        cast("Application", app).do_activate(self._playing_gfile)
        self.close()

    @Gtk.Template.Callback()
    def _get_play_icon(self, _obj, paused: bool) -> str:
        return (
            "media-playback-start-symbolic"
            if paused
            else "media-playback-pause-symbolic"
        )

    @Gtk.Template.Callback()
    def _get_fullscreen_icon(self, _obj, fullscreened: bool) -> str:
        return "view-restore-symbolic" if fullscreened else "view-fullscreen-symbolic"

    def _open_choose_subtitles(self) -> None:
        if self._playing_gfile:
            self.choose_subtitles_dialog.props.initial_folder = (
                self._playing_gfile.get_parent()
            )
        else:
            self.choose_subtitles_dialog.props.initial_folder = None
        self.choose_subtitles_dialog.open(self, callback=self._on_choose_subtitles)

    def _create_actions(self) -> None:
        self._create_action(
            "close-window",
            lambda *_: self.close(),
            ("<primary>w", "q"),
        )

        self._create_action(
            "about",
            lambda *_: self._present_about_dialog(),
        )

        self._create_action(
            "leave-fullscreen",
            lambda *_: self.set_property("fullscreened", False),
            ("Escape",),
        )

        self._create_action(
            "toggle-fullscreen",
            lambda *_: self.set_property("fullscreened", not self.props.fullscreened),
            ("F11", "f"),
        )

        self._create_action(
            "toggle-playback",
            lambda *_: self.unpause() if self.paused else self.pause(),
            ("p", "k", "space"),
        )

        self._create_action(
            "increase-volume",
            lambda *_: self.play.set_volume(min(self.play.volume + 0.05, 1)),
            ("Up",),
        )

        self._create_action(
            "decrease-volume",
            lambda *_: self.play.set_volume(max(self.play.volume - 0.05, 0)),
            ("Down",),
        )

        self._create_action(
            "toggle-mute",
            lambda *_: self.set_property("mute", not self.mute),
            ("m",),
        )

        self._create_action(
            "backwards",
            lambda *_: self.play.seek(max(0, self.play.position - 1e10)),
            ("Left",),
        )

        self._create_action(
            "forwards",
            lambda *_: self.play.seek(self.play.position + 1e10),
            ("Right",),
        )

        self._create_action(
            "decrease-rate",
            lambda *_: self._step_rate(-1),
            ("bracketleft",),
        )

        self._create_action(
            "increase-rate",
            lambda *_: self._step_rate(1),
            ("bracketright",),
        )

        self._create_action(
            "rotate-left",
            lambda *_: self._rotate_left(),
        )

        self._create_action(
            "rotate-right",
            lambda *_: self._rotate_right(),
        )

        self._create_action(
            "screenshot",
            lambda *_args: self._screenshot(),
            ("<primary><alt>s",),
        ).props.enabled = False

        self._create_action(
            "show-in-files",
            lambda *_: Gtk.FileLauncher.new(self._playing_gfile).open_containing_folder(self),
        ).props.enabled = False

        self._create_action(
            "open-video",
            lambda *_args: self.open_video_dialog.open(
                self, callback=self._on_open_video
            ),
            ("<primary>o",),
        )

        self._create_action(
            "choose-subtitles",
            lambda *_: self._open_choose_subtitles(),
        )

        subs_action = Gio.SimpleAction.new_stateful(
            "select-subtitles",
            GLib.VariantType.new("i"),
            GLib.Variant.new_int32(0),
        )
        subs_action.connect("activate", self._on_subtitles_selected)
        self.add_action(subs_action)

        lang_action = Gio.SimpleAction.new_stateful(
            "select-language",
            GLib.VariantType.new("i"),
            GLib.Variant.new_int32(0),
        )
        lang_action.connect("activate", self._on_language_selected)
        self.add_action(lang_action)

        toggle_loop_action = Gio.SimpleAction.new_stateful(
            "toggle-loop",
            None,
            GLib.Variant.new_boolean(state_settings.get_boolean("looping")),
        )
        toggle_loop_action.connect("activate", self._on_toggle_loop)
        toggle_loop_action.connect("change-state", self._on_toggle_loop)
        self.add_action(toggle_loop_action)

    def _create_action(
        self,
        name: str,
        callback: Callable[[Gio.SimpleAction, GLib.Variant], None],
        shortcuts: Sequence[str] | None = None,
    ) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

        if shortcuts and (app := self.props.application):
            if system == "Darwin":
                shortcuts = tuple(s.replace("<primary>", "<meta>") for s in shortcuts)

            app.set_accels_for_action(f"win.{name}", shortcuts)

        return action

    def _present_about_dialog(self) -> None:
        # Get the debug info from the log files
        about = Adw.AboutDialog.new_from_appdata(
            f"{PREFIX}/{APP_ID}.metainfo.xml", VERSION
        )
        about.props.version = f"v{VERSION}"
        about.props.developers = ["kramo https://kramo.page"]
        about.add_credit_section(_("Contributors"), ["Ivan Belousov https://ivanbelousov.com"])
        about.props.designers = [
            "Tobias Bernard https://tobiasbernard.com/",
            "Allan Day https://blogs.gnome.org/aday/",
            "kramo https://kramo.page",
        ]
        about.props.copyright = "© 2024-2025 kramo"
        # Translators: Replace this with your name for it to show up in the about dialog
        about.props.translator_credits = _("translator-credits")

        try:
            about.props.debug_info = log_file.read_text(
                encoding="utf-8", errors="replace"
            )
        except (FileNotFoundError, OSError):
            pass
        else:
            about.props.debug_info_filename = log_file.name

        about.present(self)

    def _screenshot(self) -> None:
        if not (contents := screenshot(self._video_paintable, self, self.play.video_size)):
            return
        title = get_title(self.play.title, self.play.uri) or _("Unknown Title")
        timestamp = nanoseconds_to_timestamp(self.play.position, hours=True)
        dialog = Gtk.FileDialog(initial_name=f"{title} {timestamp}.png")
        dialog.save(self, None, self._save_screenshot, contents)

    def _save_screenshot(
        self,
        dialog: Gtk.FileDialog,
        result: Gio.AsyncResult,
        contents: GLib.Bytes,
    ) -> None:
        logger.debug("Saving screenshot…")

        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            logger.exception("Could not save screenshot")
            return

        gfile.replace_contents_bytes_async(
            contents,
            None,
            False,
            Gio.FileCreateFlags.REPLACE_DESTINATION,
            callback=self._save_screenshot_cb,
        )

    def _save_screenshot_cb(self, gfile: Gio.File, result: Gio.AsyncResult) -> None:
        try:
            gfile.replace_contents_finish(result)
        except GLib.Error:
            logger.exception("Could not save screenshot")
            return

        toast = Adw.Toast(
            title=_("Screenshot captured"),
            priority=Adw.ToastPriority.HIGH,
            button_label=_("Show in Files"),
        )
        toast.connect(
            "button-clicked",
            lambda *_: Gtk.FileLauncher.new(gfile).open_containing_folder(),
        )
        self.toast_overlay.add_toast(toast)

        logger.debug("Screenshot saved")

    def _on_open_video(self, dialog: Gtk.FileDialog, res: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(res)
        except GLib.Error:
            return

        if not gfile:
            return

        self.play_video(gfile)

    def _on_choose_subtitles(
        self, dialog: Gtk.FileDialog, res: Gio.AsyncResult
    ) -> None:
        try:
            gfile = dialog.open_finish(res)
        except GLib.Error:
            return

        if not gfile:
            return

        if self.play is not None and self.play.ready:
            self.play.add_subtitles(gfile)

    def _on_subtitles_selected(
        self,
        _action: Gio.SimpleAction,
        state: GLib.Variant,
    ) -> None:
        if self.play.ready:
            self.play.select_track("sid", state.get_int32() or "no")

    def _on_language_selected(
        self,
        _action: Gio.SimpleAction,
        state: GLib.Variant,
    ) -> None:
        if self.play.ready:
            self.play.select_track("aid", state.get_int32())

    def _on_toggle_loop(
        self,
        action: Gio.SimpleAction,
        _state: GLib.Variant,
    ) -> None:
        value = not action.props.state.get_boolean()
        action.set_state(GLib.Variant.new_boolean(value))
        state_settings.set_boolean("looping", value)

    def _on_loop_changed(self, *_args) -> None:
        looping = state_settings.get_boolean("looping")
        if action := lookup_action(self, "toggle-loop"):
            action.set_state(GLib.Variant.new_boolean(looping))
        self.play.set_looping(looping)
