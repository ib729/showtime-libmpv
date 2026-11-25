# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo
# SPDX-FileCopyrightText: Copyright 2025 Jamie Gravendeel

from gettext import ngettext
from urllib.parse import unquote

from gi.repository import (
    Adw,
    Gdk,
    Gio,
    GLib,
    GObject,
    Gtk,
)

from showtime import PREFIX, state_settings
from showtime.utils import lookup_action


@Gtk.Template.from_resource(f"{PREFIX}/gtk/options.ui")
class Options(Adw.Bin):
    """A bin containing the options menu button and its popover."""

    __gtype_name__ = "Options"

    menu_button: Gtk.MenuButton = Gtk.Template.Child()
    popover: Gtk.PopoverMenu = Gtk.Template.Child()

    language_menu: Gio.Menu = Gtk.Template.Child()
    subtitles_menu: Gio.Menu = Gtk.Template.Child()

    rate = GObject.Property(type=str)

    def on_secondary_click_pressed(
        self,
        window: Adw.ApplicationWindow,
        gesture: Gtk.Gesture,
        x: int,
        y: int,
    ) -> None:
        """Parent and unparent the popover appropriately."""
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        self.menu_button.props.popover = None
        self.popover.set_parent(window)
        self.popover.props.has_arrow = False
        self.popover.props.halign = Gtk.Align.START

        rectangle = Gdk.Rectangle()
        rectangle.x, rectangle.y, rectangle.width, rectangle.height = x, y, 0, 0
        self.popover.props.pointing_to = rectangle

        self.popover.popup()

        def closed(*_args) -> None:
            self.popover.unparent()
            self.popover.props.has_arrow = True
            self.popover.props.halign = Gtk.Align.FILL

            self.popover.props.pointing_to = None  # pyright: ignore[reportAttributeAccessIssue]
            self.menu_button.props.popover = self.popover

            self.popover.disconnect_by_func(closed)

        self.popover.connect("closed", closed)

    def build_menus(self, tracks: list[dict]) -> None:
        """Fill the menus with mpv's track IDs and actual selections."""
        self.language_menu.remove_all()
        self.subtitles_menu.remove_all()
        self.subtitles_menu.append(_("None"), "win.select-subtitles(int32 0)")
        selected = {"audio": 0, "sub": 0}
        has_audio = False
        for track in tracks:
            kind = track["type"]
            if kind not in selected:
                continue
            language = self._track_text(track.get("lang"))
            title = self._track_text(track.get("title"))
            track_id = track["id"]
            if track.get("selected"):
                selected[kind] = track_id
            if kind == "audio":
                has_audio = True
                if not language and (channels := track.get("demux-channel-count")):
                    language = ngettext(
                        # Translators: The variable is the number of channels
                        # in an audio track
                        "Undetermined, {} Channel",
                        "Undetermined, {} Channels",
                        channels,
                    ).format(channels)
                menu, action = self.language_menu, "select-language"
                language = language or _("Undetermined")
            else:
                menu, action = self.subtitles_menu, "select-subtitles"
                language = language or _("Undetermined Language")
            label = f"{language} - {title}" if title and title != language else language
            menu.append(label, f"win.{action}(int32 {track_id})")

        if not has_audio:
            self.language_menu.append(_("No Audio"), "nonexistent.action")
        self.subtitles_menu.append(_("Add Subtitle File…"), "win.choose-subtitles")
        for kind, name in (("audio", "select-language"), ("sub", "select-subtitles")):
            if action := lookup_action(self.props.root, name):
                action.set_state(GLib.Variant.new_int32(selected[kind]))

    @staticmethod
    def _track_text(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        return unquote(value or "")

    def _on_toggle_loop(self, action: Gio.SimpleAction, _state: GLib.Variant) -> None:
        value = not action.props.state.get_boolean()
        action.set_state(GLib.Variant.new_boolean(value))
        state_settings.set_boolean("looping", value)
