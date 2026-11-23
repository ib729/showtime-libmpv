# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo

import json
from bisect import bisect_left
from collections.abc import Callable, Sequence
from functools import partial
from hashlib import sha256
from math import sqrt
from time import time
from typing import TYPE_CHECKING, Any, NamedTuple, cast
from weakref import ref

from gi.repository import (
    Adw,
    Gdk,
    Gio,
    GLib,
    GObject,
    Graphene,
    Gsk,
    Gst,
    GstAudio,  # pyright: ignore[reportAttributeAccessIssue]
    GstPbutils,
    GstPlay,  # pyright: ignore[reportAttributeAccessIssue]
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
from showtime.play import (
    Messenger,
    MissingPlugin,
    gst_play_setup,
)
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
CHAPTER_SNAP_PX = 4

MINUTE_IN_NS = 6e10


class Chapter(NamedTuple):
    start: int
    end: int | None
    title: str | None


class SeekTarget(NamedTuple):
    position: int
    chapter_index: int | None
    x: float
    y: int


class ChapterTrack(Adw.Bin):
    """Paint chapter dots on the native seek scale while keeping its input handling."""

    def __init__(self, scale: Gtk.Scale) -> None:
        super().__init__(
            child=scale, hexpand=scale.get_hexpand(), valign=scale.get_valign()
        )
        self._scale = scale
        trough = next(child for child in scale if child.get_css_name() == "trough")
        self._slider = next(
            child for child in trough if child.get_css_name() == "slider"
        )
        self._stops: tuple[float, ...] = ()
        self._dot_color = Adw.AccentColor.to_rgba(Adw.AccentColor.BLUE)

    def set_stops(self, stops: tuple[float, ...]) -> None:
        """Set chapter starts as fractions of the duration."""
        if self._stops == stops:
            return
        self._stops = stops
        scale = self._scale
        scale.clear_marks()
        for stop in stops:
            scale.add_mark(stop * SCALE_MULT, Gtk.PositionType.TOP, None)
        for child in scale:
            if child.get_css_name() == "marks":
                child.set_visible(False)
        scale.remove_css_class("marks-before")
        self.queue_draw()

    def axis(self) -> tuple[float, float]:
        """Return the zero-time x coordinate and the signed travel in scale coordinates."""
        scale = self._scale
        trough = scale.get_range_rect()
        minimum = self._slider.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        thumb = min(trough.width, minimum)
        travel = trough.width - thumb
        left = trough.x + thumb / 2
        rtl = scale.get_flippable() and scale.get_direction() == Gtk.TextDirection.RTL
        if scale.get_inverted() != rtl:
            return left + travel, -travel
        return left, travel

    def is_on_thumb(self, x: float, y: float) -> bool:
        return self._scale.pick(x, y, Gtk.PickFlags.DEFAULT) == self._slider

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw small chapter dots inside the unplayed part of the native track."""
        Adw.Bin.do_snapshot(self, snapshot)
        scale = self._scale
        upcoming = bisect_left(self._stops, scale.get_value() / SCALE_MULT)
        if upcoming == len(self._stops):
            return
        origin = Graphene.Point().init(0, 0)
        ok, offset = scale.compute_point(self, origin)
        if not ok:
            return
        trough = scale.get_range_rect()
        first, travel = self.axis()
        if not travel or trough.height <= 0:
            return
        thumb_left, thumb_right = scale.get_slider_range()
        left, right = trough.x, trough.x + trough.width
        if travel > 0:
            left = max(left, thumb_right)
        else:
            right = min(right, thumb_left)
        if left >= right:
            return

        radius = min(1.5, trough.height / 3)
        y = trough.y + trough.height / 2
        dots = Gsk.PathBuilder.new()
        for index in range(upcoming, len(self._stops)):
            x = first + travel * self._stops[index]
            dots.add_circle(Graphene.Point().init(x, y), radius)

        snapshot.save()
        snapshot.translate(offset)
        snapshot.push_clip(
            Graphene.Rect().init(left, trough.y, right - left, trough.height)
        )
        snapshot.append_fill(dots.to_path(), Gsk.FillRule.WINDING, self._dot_color)
        snapshot.pop()
        snapshot.restore()


def _chapter_entries(
    entries: Sequence[Gst.TocEntry],
) -> list[Chapter]:
    chapters = []
    for entry in entries:
        if entry.get_entry_type() == Gst.TocEntryType.CHAPTER:
            valid, start, stop = entry.get_start_stop_times()
            if valid and 0 <= start < Gst.CLOCK_TIME_NONE:
                tags = entry.get_tags()
                title = tags.get_string(Gst.TAG_TITLE)[1] if tags else None
                end = stop if start < stop < Gst.CLOCK_TIME_NONE else None
                chapters.append(Chapter(start, end, title))
        chapters.extend(_chapter_entries(entry.get_sub_entries() or ()))
    return chapters


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
    missing_plugin_status_page: Adw.StatusPage = Gtk.Template.Child()

    video_page: Gtk.WindowHandle = Gtk.Template.Child()
    video_overlay: Gtk.Overlay = Gtk.Template.Child()
    overlay_motion: Gtk.EventControllerMotion = Gtk.Template.Child()
    picture: Gtk.Picture = Gtk.Template.Child()

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
        return self.play.props.mute

    @mute.setter
    def mute(self, mute: bool) -> None:
        self.play.props.mute = mute

    @GObject.Property(type=str)
    def rate(self) -> str:
        """Get the playback rate."""
        return str(self.play.props.rate)

    @rate.setter
    def rate(self, rate: str) -> None:
        self.play.props.rate = float(rate)
        self.options.popover.popdown()
        self.emit("rate-changed")

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
        self.stopped = self.stopped and paused

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

        (
            self.paintable,
            self.play,
            self.pipeline,
            self.sink,
        ) = gst_play_setup(self.picture)

        # Connections to objects that may outlive the window, disconnected in teardown
        self._paintable_connection = Connection(
            "invalidate-size", self._on_paintable_invalidate_size, self.paintable
        )
        self._pipeline_connection = Connection(
            "source-setup", self._setup_pipeline_source, self.pipeline
        )

        self._chapters: list[Chapter] = []
        self._chapter_starts: list[int] = []
        self._chapter_generation = 0
        self._chapter_source_generation = None
        self._chapter_bus = self.pipeline.get_bus()
        self._chapter_bus.enable_sync_message_emission()
        self._chapter_bus_connection = Connection(
            "sync-message::toc", self._on_chapter_message, self._chapter_bus
        )
        self._chapter_source_connection = Connection(
            "source-setup", self._on_chapter_source_setup, self.pipeline
        )
        self._setup_seek_preview()

        self._messenger = Messenger(self.play)
        self._messenger_connections = [
            Connection("state-changed", self._on_playback_state_changed, self._messenger),
            Connection("duration-changed", self._on_duration_changed, self._messenger),
            Connection("position-updated", self._on_position_updated, self._messenger),
            Connection("seek-done", self._on_seek_done, self._messenger),
            Connection("media-info-updated", self._on_media_info_updated, self._messenger),
            Connection("volume-changed", self._on_volume_changed, self._messenger),
            Connection("end-of-stream", self._on_end_of_stream, self._messenger),
            Connection("warning", self._on_warning, self._messenger),
            Connection("error", self._on_error, self._messenger),
            Connection("missing-plugins", self._on_missing_plugins, self._messenger),
        ]

        self._state_settings_connection = Connection(
            "changed::end-timestamp-type",
            self._on_end_timestamp_type_changed,
            state_settings,
        )

        self._update_subtitle_font_desc()
        self._font_connection = Connection(
            "notify::gtk-xft-dpi",
            self._update_subtitle_font_desc,
            Gtk.Settings.get_default(),
        )

        # Pending timeouts, removed in teardown
        self._resize_source = SourceId()
        self._hide_overlays_source = SourceId()
        self._buffering_source = SourceId()
        self._seeking_source = SourceId()
        self._build_menus_source = SourceId()

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

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        """Call to set the allocation, if the widget does not have a layout manager."""
        Adw.ApplicationWindow.do_size_allocate(self, width, height, baseline)

        self.sink.props.window_width = self.get_width() * self.props.scale_factor  # pyright: ignore[reportAttributeAccessIssue]
        self.sink.props.window_height = self.get_height() * self.props.scale_factor  # pyright: ignore[reportAttributeAccessIssue]

    def play_video(self, gfile: Gio.File) -> None:
        """Start playing the given `GFile`."""
        if not (app := self.props.application):
            return

        app.save_play_position(self)  # pyright: ignore[reportAttributeAccessIssue]

        self._playing_gfile = gfile

        try:
            file_info = gfile.query_info(
                f"{Gio.FILE_ATTRIBUTE_STANDARD_IS_SYMLINK},{Gio.FILE_ATTRIBUTE_STANDARD_SYMLINK_TARGET}",
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
            )
        except GLib.Error:
            uri = gfile.get_uri()
        else:
            if file_info.get_is_symlink() and (
                target := file_info.get_symlink_target()
            ):
                uri = Gio.File.new_for_path(target).get_uri()
            else:
                uri = gfile.get_uri()

        logger.debug("Playing video: %s", uri)

        self._chapter_generation += 1
        self._chapter_source_generation = None
        self._seek_drag_position = None
        self._seek_drag.reset()
        self._seeking_source.remove()
        self._seek_preview.hide()
        self._set_chapters([])
        self.media_info_updated = False
        self.stack.props.visible_child = self.video_page
        self.placeholder_stack.props.visible_child = self.error_status_page
        self.select_subtitles(0)
        self.select_language(0)
        self.rate = "1.0"

        self.play.props.uri = uri
        self.pause()
        self._on_motion()

        if action := lookup_action(self, "screenshot"):
            action.props.enabled = True

        if action := lookup_action(self, "show-in-files"):
            action.props.enabled = True

    def unpause(self) -> None:
        """Start playing the current video."""
        self._hide_overlay(self.restore_breakpoint_bin)
        self._reveal_overlay(self.controls_box)
        self.play.play()
        logger.debug("Video unpaused")

    def pause(self, *_args) -> None:
        """Pause the currently playing video."""
        self.play.pause()
        logger.debug("Video paused")

    def select_subtitles(self, index: int) -> None:
        """Select subtitles for the given index."""
        if action := lookup_action(self, "select-subtitles"):
            action.activate(GLib.Variant.new_uint16(index))

    def select_language(self, index: int) -> None:
        """Select the audio track for the given index."""
        if action := lookup_action(self, "select-language"):
            action.activate(GLib.Variant.new_uint16(index))

    def teardown_playback(self) -> None:
        """Stop playback and clear all references to self."""
        self._chapter_generation += 1
        self._seek_drag_position = None
        self._seek_preview.hide()
        for connection in (
            self._paintable_connection,
            self._pipeline_connection,
            self._chapter_bus_connection,
            self._chapter_source_connection,
            *self._seek_connections,
            self._state_settings_connection,
            self._font_connection,
            *self._messenger_connections,
        ):
            connection.disconnect()

        self._chapter_bus.disable_sync_message_emission()
        self._chapter_bus = None

        for source in (
            self._resize_source,
            self._hide_overlays_source,
            self._buffering_source,
            self._seeking_source,
            self._build_menus_source,
        ):
            source.remove()

        self._messenger.teardown()
        self._messenger = None

        for name in self.list_actions():
            self.remove_action(name)

        self.play.stop()
        self.play = None
        self.pipeline = None

    @Gtk.Template.Callback()
    def _cycle_end_timestamp_type(self, *_args) -> None:
        state_settings.set_enum(
            "end-timestamp-type",
            int(not showtime.end_timestamp_type),
        )

        self._set_end_timestamp_label(
            self.play.props.position, self.play.props.duration
        )

    @Gtk.Template.Callback()
    def _resume(self, *_args) -> None:
        self.unpause()

    @Gtk.Template.Callback()
    def _play_again(self, *_args) -> None:
        self.play.seek(0)
        self.unpause()

    def _rotate_left(self) -> None:
        match int((props := self.paintable.props).orientation):
            case 0:
                props.orientation = 4
            case 1:
                props.orientation = 4
            case 5:
                props.orientation = 8
            case _:
                props.orientation -= 1

    def _rotate_right(self) -> None:
        match int((props := self.paintable.props).orientation):
            case 0:
                props.orientation = 2
            case 4:
                props.orientation = 1
            case 8:
                props.orientation = 5
            case _:
                props.orientation += 1

    @Gtk.Template.Callback()
    def _on_drop(self, _target, gfile: Gio.File, _x, _y) -> None:
        self.play_video(gfile)

    def _get_previous_play_position(self) -> float | None:
        if not (uri := self.play.props.uri):
            return None

        try:
            hist_file = (showtime.state_path / "playback_history.json").open("r")
        except FileNotFoundError:
            logger.info("Cannot restore play positon, no playback history file")
            return None

        try:
            hist = json.load(hist_file)
        except EOFError as error:
            logger.warning("Cannot restore play positon: %s", error)
            return None

        hist_file.close()
        return hist.get(get_file_hash_identifier(uri))

    def _resize_window(
        self, _obj, paintable: Gdk.Paintable, initial: bool | None = False
    ) -> None:
        logger.debug("Resizing window…")

        if initial:
            self.disconnect_by_func(self._resize_window)

        if not (video_width := paintable.get_intrinsic_width()) or not (
            video_height := paintable.get_intrinsic_height()
        ):
            return

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

    def _setup_pipeline_source(self, *_args) -> None:
        self._pipeline_connection.disconnect()

        if not (pos := self._get_previous_play_position()):
            self.unpause()
            logger.debug("No previous play position")
            return

        if pos < MINUTE_IN_NS:
            self.unpause()
            logger.debug("Previous play position before 60s")
            return

        self._reveal_overlay(self.restore_breakpoint_bin)
        self._hide_overlay(self.controls_box)
        self.play.seek(pos)
        logger.debug("Previous play position restored: %i", pos)

    def _update_subtitle_font_desc(self) -> None:
        self.pipeline.props.subtitle_font_desc = get_subtitle_font_desc()

    def _on_end_timestamp_type_changed(self, *_args) -> None:
        showtime.end_timestamp_type = state_settings.get_enum("end-timestamp-type")
        self._set_end_timestamp_label(
            self.play.props.position, self.play.props.duration
        )

    def _set_end_timestamp_label(self, pos: int, dur: int) -> None:
        match showtime.end_timestamp_type:
            case 0:  # Duration
                self.end_timestamp_button.props.label = nanoseconds_to_timestamp(dur)
            case 1:  # Remaining
                self.end_timestamp_button.props.label = "-" + nanoseconds_to_timestamp(
                    dur - pos
                )

    @Gtk.Template.Callback()
    def _schedule_volume_change(self, _obj, value: float) -> None:
        GLib.idle_add(
            partial(
                self.pipeline.set_volume,  # pyright: ignore[reportAttributeAccessIssue]
                GstAudio.StreamVolumeFormat.CUBIC,
                value,
            )
        )

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
            or self._seek_drag_position is not None
            # Active popover
            or any(button.props.active for button in self.overlay_menu_buttons)
            # Active restore buttons
            or self.restore_breakpoint_bin.props.can_target
        ):
            return

        self._seek_preview.hide()
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
            self._seek_preview.hide()
            self._hide_overlays(self._last_reveal)

        self._toplevel_focused = bool(focused)

    def _on_paintable_invalidate_size(self, paintable: Gdk.Paintable, *_args) -> None:
        if self.is_visible():
            # Add a timeout to not interfere with loading the stream too much
            self._resize_source.add_timeout(
                GLib.PRIORITY_DEFAULT,
                100,
                lambda: self._resize_window(None, paintable),
            )
        else:
            self.connect("map", self._resize_window, paintable, True)

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

    def _on_playback_state_changed(self, _obj, state: GstPlay.PlayState) -> None:
        # Only show a spinner if buffering for more than a second
        if state == GstPlay.PlayState.BUFFERING:
            self.buffering = True
            self._buffering_source.ensure_timeout(
                GLib.PRIORITY_DEFAULT,
                1000,
                lambda: self._reveal_overlay(self.spinner),
            )
            return

        self.buffering = False
        self._buffering_source.remove()
        self._hide_overlay(self.spinner)

        match state:
            case GstPlay.PlayState.PAUSED:
                self.paused = True
            case GstPlay.PlayState.STOPPED:
                self.paused = True
                self.stopped = True
            case GstPlay.PlayState.PLAYING:
                self.paused = False

    def _on_duration_changed(self, _obj, dur: int) -> None:
        self._set_end_timestamp_label(self.play.props.position, dur)
        self._update_chapter_marks(dur)

    def _on_chapter_source_setup(self, *_args) -> None:
        self._chapter_source_generation = self._chapter_generation

    def _on_chapter_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        generation = self._chapter_source_generation
        if generation is None:
            return

        toc, _updated = message.parse_toc()
        if toc is None or toc.get_scope() != Gst.TocScope.GLOBAL:
            return

        by_start = {
            chapter.start: chapter for chapter in _chapter_entries(toc.get_entries() or ())
        }
        chapters = [by_start[start] for start in sorted(by_start)]
        window_ref = ref(self)

        def show_chapters() -> None:
            window = window_ref()
            if (
                window is not None
                and window._chapter_generation == generation
                and window.play is not None
            ):
                window._set_chapters(chapters)

        GLib.idle_add(show_chapters)

    def _set_chapters(self, chapters: list[Chapter]) -> None:
        if chapters == self._chapters:
            return

        self._chapters = chapters
        self._chapter_starts = [chapter.start for chapter in chapters]
        self.options.set_chapters([(chapter.start, chapter.title) for chapter in chapters])
        self._update_chapter_marks(self.play.props.duration)

    def _update_chapter_marks(self, duration: int) -> None:
        stops = (
            tuple(start / duration for start in self._chapter_starts if start < duration)
            if 0 < duration < Gst.CLOCK_TIME_NONE else ()
        )
        self._chapter_track.set_stops(stops)

    def _setup_seek_preview(self) -> None:
        scale = self.seek_scale
        box = scale.get_parent()
        previous = scale.get_prev_sibling()
        box.remove(scale)
        self._chapter_track = ChapterTrack(scale)
        box.insert_child_after(self._chapter_track, previous)

        self._seek_preview_layer = Gtk.Fixed(can_target=False)
        self._seek_preview = Gtk.Box(can_target=False, visible=False)
        self._seek_preview_content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, can_target=False, hexpand=True
        )
        self._seek_preview_content.add_css_class("toolbar")
        self._seek_preview_content.add_css_class("osd")
        self._seek_preview_title = Gtk.Label(
            can_target=False,
            justify=Gtk.Justification.CENTER,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            max_width_chars=36,
            visible=False,
        )
        self._seek_preview_title.add_css_class("heading")
        self._seek_preview_time = Gtk.Label(can_target=False)
        self._seek_preview_time.add_css_class("numeric")
        self._seek_preview_content.append(self._seek_preview_title)
        self._seek_preview_content.append(self._seek_preview_time)
        self._seek_preview.append(self._seek_preview_content)
        self._seek_preview_layer.put(self._seek_preview, 0, 0)
        self.video_overlay.add_overlay(self._seek_preview_layer)

        self._seek_drag_position: int | None = None
        self._seek_drag_start_x = 0.0
        self._seek_drag_dx = 0.0
        self._seek_motion = Gtk.EventControllerMotion()
        self._seek_drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        self._seek_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.seek_scale.add_controller(self._seek_motion)
        self.seek_scale.add_controller(self._seek_drag)
        self._seek_connections = [
            Connection("enter", self._on_seek_preview_motion, self._seek_motion),
            Connection("motion", self._on_seek_preview_motion, self._seek_motion),
            Connection("leave", self._on_seek_preview_leave, self._seek_motion),
            Connection("drag-begin", self._on_seek_drag_begin, self._seek_drag),
            Connection("drag-update", self._on_seek_drag_update, self._seek_drag),
            Connection("drag-end", self._on_seek_drag_end, self._seek_drag),
            Connection("cancel", self._on_seek_drag_cancel, self._seek_drag),
        ]

    def _seek_target(self, x: float) -> SeekTarget | None:
        if self.play is None:
            return None
        duration = self.play.props.duration
        if not 0 < duration < Gst.CLOCK_TIME_NONE:
            return None

        trough = self.seek_scale.get_range_rect()
        first, travel = self._chapter_track.axis()
        if not travel:
            return None
        fraction = max(0.0, min((x - first) / travel, 1.0))
        x = first + travel * fraction
        position = round(fraction * duration)
        index = bisect_left(self._chapter_starts, position)
        closest = None
        distance = CHAPTER_SNAP_PX
        for candidate in (index - 1, index):
            if 0 <= candidate < len(self._chapter_starts):
                start = self._chapter_starts[candidate]
                marker_x = first + travel * start / duration
                if start < duration and abs(x - marker_x) <= distance:
                    closest = candidate
                    distance = abs(x - marker_x)
        if closest is not None:
            position = self._chapter_starts[closest]
            x = first + travel * position / duration
        return SeekTarget(position, closest, x, trough.y)

    def _show_seek_preview(self, target: SeekTarget) -> None:
        duration = self.play.props.duration
        hours = duration >= 3600 * Gst.SECOND
        stamp = nanoseconds_to_timestamp(target.position, hours=hours)
        name = None
        if (index := target.chapter_index) is not None:
            chapter = self._chapters[index]
            name = chapter.title or _("Chapter {}").format(index + 1)
            end = chapter.end
            if end is None:
                next_index = index + 1
                end = (
                    self._chapter_starts[next_index]
                    if next_index < len(self._chapter_starts)
                    else duration
                )
            end_stamp = nanoseconds_to_timestamp(min(end, duration), hours=hours)
            stamp = f"{stamp}–{end_stamp}"
        title = self._seek_preview_title
        if name is not None and title.get_text() != name:
            title.set_text(name)
        title.set_visible(name is not None)
        if self._seek_preview_time.get_text() != stamp:
            self._seek_preview_time.set_text(stamp)

        point = Graphene.Point()
        point.x, point.y = target.x, target.y
        layer = self._seek_preview_layer
        translated, anchor = self.seek_scale.compute_point(layer, point)
        if not translated:
            return

        preview = self._seek_preview
        preview.set_visible(True)
        content = self._seek_preview_content
        minimum, natural, *_ = content.measure(Gtk.Orientation.HORIZONTAL, -1)
        viewport_width = self.video_overlay.get_width()
        width = max(minimum, min(natural, viewport_width - 8))
        if preview.props.width_request != width:
            preview.set_size_request(width, -1)
        height = content.measure(Gtk.Orientation.VERTICAL, width)[1]
        x = max(4, min(anchor.x - width / 2, viewport_width - width - 4))
        y = min(max(4, anchor.y - height - 6), self.video_overlay.get_height() - height - 4)
        layer.move(preview, x, y)

    def _on_seek_preview_motion(self, _motion, x: float, _y: float) -> None:
        if self._seek_drag_position is None and (target := self._seek_target(x)):
            self._show_seek_preview(target)

    def _on_seek_preview_leave(self, *_args) -> None:
        if self._seek_drag_position is None:
            self._seek_preview.hide()

    def _on_seek_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if not self._chapter_starts:
            return

        target = self._seek_target(x)
        if target is None or not gesture.set_state(Gtk.EventSequenceState.CLAIMED):
            return

        self.seek_scale.grab_focus()
        self._seek_drag_start_x = x
        self._seek_drag_dx = 0.0
        self._begin_seek()
        self._seeking_source.remove()
        if self._chapter_track.is_on_thumb(x, y):
            start, travel = self._chapter_track.axis()
            fraction = self.seek_scale.get_value() / SCALE_MULT
            self._seek_drag_start_x = start + travel * fraction
            self._seek_drag_position = round(self.play.props.duration * fraction)
        else:
            self._drag_seek_to(target)

    def _on_seek_drag_update(self, _gesture, dx: float, _dy: float) -> None:
        if (
            self._seek_drag_position is not None
            and dx != self._seek_drag_dx
            and (target := self._seek_target(self._seek_drag_start_x + dx))
        ):
            self._seek_drag_dx = dx
            self._drag_seek_to(target)

    def _drag_seek_to(self, target: SeekTarget) -> None:
        self._show_seek_preview(target)
        if target.position == self._seek_drag_position:
            return
        self._seek_drag_position = target.position
        duration = self.play.props.duration
        self.seek_scale.set_value((target.position / duration) * SCALE_MULT)
        self._commit_seek(target.position)

    def _on_seek_drag_end(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        self._on_seek_drag_update(gesture, dx, dy)
        self._finish_seek_drag()
        if not self._seek_motion.contains_pointer():
            self._seek_preview.hide()

    def _on_seek_drag_cancel(self, *_args) -> None:
        self._finish_seek_drag()
        self._seek_preview.hide()

    def _finish_seek_drag(self) -> None:
        if self._seek_drag_position is not None:
            self._seek_drag_position = None
            self._schedule_seek_resume()
            self._on_motion()

    def _seek_chapter(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        if self.play and (position := target.get_uint64()) in self._chapter_starts:
            self._commit_seek(position)

    def _on_position_updated(self, _obj, pos: int) -> None:
        dur = self.play.props.duration

        if self._seek_drag_position is None and dur:
            self.seek_scale.set_value((pos / dur) * SCALE_MULT)

        # TODO: This can probably be done only every second instead
        self.position_label.props.label = nanoseconds_to_timestamp(pos)
        self._set_end_timestamp_label(pos, dur)

    @Gtk.Template.Callback()
    def _seek(self, _obj, _scroll, val: float) -> None:
        if self._seek_drag_position is not None or self.play is None:
            return
        duration = self.play.props.duration
        if 0 < duration < Gst.CLOCK_TIME_NONE:
            self._begin_seek()
            self._commit_seek(round(duration * max(0, min(val / SCALE_MULT, 1))))
            self._schedule_seek_resume()

    def _begin_seek(self) -> None:
        if not self._seeking_source.added:
            self._seek_paused = self.paused

        if not self.paused:
            self.pause()

    def _commit_seek(self, position: int) -> None:
        self.play.seek(position)
        self.emit("seeked")

    def _schedule_seek_resume(self) -> None:
        def post_seek() -> None:
            if not self._seek_paused:
                self.unpause()

        self._seeking_source.add_timeout(GLib.PRIORITY_DEFAULT, 250, post_seek)

    def _on_seek_done(self, _obj) -> None:
        pos = self.play.props.position
        dur = self.play.props.duration

        if self._seek_drag_position is None and dur:
            self.seek_scale.set_value((pos / dur) * SCALE_MULT)
        self.position_label.props.label = nanoseconds_to_timestamp(pos)
        self._set_end_timestamp_label(pos, dur)
        logger.debug("Seeked to %i", pos)

    def _on_media_info_updated(self, _obj, media_info: GstPlay.PlayMediaInfo) -> None:
        self.title_label.props.label = get_title(media_info) or ""

        # Add a timeout to reduce things happening at once while the video is loading
        # since the user won't want to change languages/subtitles within 500ms anyway
        self._build_menus_source.ensure_timeout(
            GLib.PRIORITY_DEFAULT,
            500,
            lambda: self.options.build_menus(media_info),
        )
        self.emit("media-info-updated")

    def _on_volume_changed(self, _obj) -> None:
        vol = self.pipeline.get_volume(GstAudio.StreamVolumeFormat.CUBIC)  # pyright: ignore[reportAttributeAccessIssue]

        if self._prev_volume == vol:
            return

        self._prev_volume = vol
        self.volume = vol
        self.sound_options.adjustment.props.value = vol

        self.emit("volume-changed")

    def _on_end_of_stream(self, _obj) -> None:
        if not state_settings.get_boolean("looping"):
            self.pause()

        self.play.seek(0)

    def _on_warning(self, _obj, warning: GLib.Error) -> None:
        logger.warning(warning)

    def _on_error(self, _obj, error: GLib.Error) -> None:
        logger.error(error.message)

        if (
            self.placeholder_stack.get_visible_child()
            == self.missing_plugin_status_page
        ):
            return

        def copy_details(*_args) -> None:
            if not (display := Gdk.Display.get_default()):
                return

            display.get_clipboard().set(error.message)

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

    def _on_missing_plugins(
        self, _obj, missing_plugins: tuple[MissingPlugin, ...], partially_missing: bool
    ) -> None:
        if partially_missing:
            self.pause()

        desc, detail = missing_plugins[0]
        group = Adw.PreferencesGroup(
            halign=Gtk.Align.CENTER,
            width_request=250,
            separate_rows=True,
        )

        if GstPbutils.install_plugins_supported():

            def on_install_done(result: GstPbutils.InstallPluginsReturn) -> None:
                if (
                    self.stack.props.visible_child != self.placeholder_page
                    or self.placeholder_stack.props.visible_child
                    != self.missing_plugin_status_page
                ):
                    return

                match result:
                    case GstPbutils.InstallPluginsReturn.SUCCESS:
                        logger.debug("Plugin installed")
                        Gst.update_registry()
                        self._try_again()

                    case GstPbutils.InstallPluginsReturn.NOT_FOUND:
                        logger.error("Plugin installation failed: Not found")
                        self.missing_plugin_status_page.props.description = _(
                            "No plugin available for this media type"
                        )

                    case _:
                        logger.error(
                            "Plugin installation failed, result: %d", int(result)
                        )
                        self.missing_plugin_status_page.props.description = _(
                            "Unable to install the required plugin"
                        )

            def install_plugin(*_args) -> None:
                GstPbutils.install_plugins_async(
                    (detail,) if detail else (), None, on_install_done
                )
                self.toast_overlay.add_toast(Adw.Toast.new(_("Installing…")))
                install.props.sensitive = False

            self.missing_plugin_status_page.props.description = _(
                "“{}” codecs are required to play this video"
            ).format(desc)

            install = Adw.ButtonRow(title=_("Install Plugin"))
            install.add_css_class("suggested-action")
            install.connect("activated", install_plugin)
            group.add(install)
        else:
            self.missing_plugin_status_page.props.description = _(
                "The “{}” codecs required to play this video could not be found"
            ).format(desc)

        if partially_missing:

            def try_to_play(*_args) -> None:
                self.missing_plugin_status_page.props.child = None
                self.stack.props.visible_child = self.video_page
                self.unpause()

            try_anyway = Adw.ButtonRow(title=_("Try Anyway"))
            try_anyway.connect("activated", try_to_play)
            group.add(try_anyway)

        self.missing_plugin_status_page.props.child = group
        self.placeholder_stack.props.visible_child = self.missing_plugin_status_page
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
            ("Esc",),
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
            lambda *_: self.play.set_volume(min(self.play.props.volume + 0.05, 1)),
            ("Up",),
        )

        self._create_action(
            "decrease-volume",
            lambda *_: self.play.set_volume(max(self.play.props.volume - 0.05, 0)),
            ("Down",),
        )

        self._create_action(
            "toggle-mute",
            lambda *_: self.set_property("mute", not self.mute),
            ("m",),
        )

        self._create_action(
            "backwards",
            lambda *_: self.play.seek(max(0, self.play.props.position - 1e10)),
            ("Left",),
        )

        self._create_action(
            "forwards",
            lambda *_: self.play.seek(self.play.props.position + 1e10),
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
            lambda *_: Gtk.FileLauncher.new(
                Gio.File.new_for_uri(self.play.props.uri)
            ).open_containing_folder(),
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
            GLib.VariantType.new("q"),
            GLib.Variant.new_uint16(0),
        )
        subs_action.connect("activate", self._on_subtitles_selected)
        self.add_action(subs_action)

        lang_action = Gio.SimpleAction.new_stateful(
            "select-language",
            GLib.VariantType.new("q"),
            GLib.Variant.new_uint16(0),
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

        chapter_action = Gio.SimpleAction.new("seek-chapter", GLib.VariantType.new("t"))
        chapter_action.connect("activate", self._seek_chapter)
        self.add_action(chapter_action)

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
        about.props.developers = ["kramo https://kramo.page"]
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
        if not (paintable := self.picture.props.paintable):
            logger.warning("Cannot take screenshot, no paintable")
            return

        if not (texture := screenshot(paintable, self)):
            return

        Gtk.FileDialog(
            initial_name=f"{
                get_title(self.play.get_media_info()) or _('Unknown Title')
            } {nanoseconds_to_timestamp(self.play.get_position(), hours=True)}.png"
        ).save(self, None, self._save_screenshot, texture.save_to_png_bytes())

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

        self.play.props.suburi = gfile.get_uri()
        self.select_subtitles(0)
        logger.debug("External subtitle added: %s", gfile.get_uri())

    def _on_subtitles_selected(
        self,
        action: Gio.SimpleAction,
        state: GLib.Variant,
    ) -> None:
        action.props.state = state

        if (index := state.get_uint16()) == GLib.MAXUINT16:
            self.play.set_subtitle_track_enabled(False)
        else:
            self.play.set_subtitle_track(index)
            self.play.set_subtitle_track_enabled(True)

    def _on_language_selected(
        self,
        action: Gio.SimpleAction,
        state: GLib.Variant,
    ) -> None:
        action.props.state = state
        self.play.set_audio_track(state.get_uint16())

    def _on_toggle_loop(
        self,
        action: Gio.SimpleAction,
        _state: GLib.Variant,
    ) -> None:
        value = not action.props.state.get_boolean()
        action.set_state(GLib.Variant.new_boolean(value))
        state_settings.set_boolean("looping", value)
