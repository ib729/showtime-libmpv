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
    GstPlay,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,
)

from showtime import PREFIX, state_settings
from showtime.utils import lookup_action, nanoseconds_to_timestamp


@Gtk.Template.from_resource(f"{PREFIX}/gtk/options.ui")
class Options(Adw.Bin):
    """A bin containing the options menu button and its popover."""

    __gtype_name__ = "Options"

    menu_button: Gtk.MenuButton = Gtk.Template.Child()
    popover: Gtk.PopoverMenu = Gtk.Template.Child()

    language_menu: Gio.Menu = Gtk.Template.Child()
    subtitles_menu: Gio.Menu = Gtk.Template.Child()

    rate = GObject.Property(type=str)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._chapters_menu = Gio.Menu()
        self._chapters_section = Gio.Menu()
        self.popover.get_menu_model().insert_section(1, None, self._chapters_section)

    def set_chapters(self, chapters: list[tuple[int, str | None]]) -> None:
        if not chapters:
            self._chapters_section.remove_all()
        self._chapters_menu.remove_all()
        for index, (start, title) in enumerate(chapters, 1):
            stamp = nanoseconds_to_timestamp(start)
            name = (title or _("Chapter {}").format(index)).replace("_", "__")
            label = f"{stamp}  {name}"
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "win.seek-chapter", GLib.Variant.new_uint64(start)
            )
            self._chapters_menu.append_item(item)
        if chapters and not self._chapters_section.get_n_items():
            self._chapters_section.append_submenu(_("Chapters"), self._chapters_menu)

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

    def build_menus(self, media_info: GstPlay.PlayMediaInfo) -> None:
        """Fill up the menu with subtitles and languages."""
        self.language_menu.remove_all()
        self.subtitles_menu.remove_all()

        langs = 0
        for index, stream in enumerate(media_info.get_audio_streams()):
            has_title, title = stream.get_tags().get_string("title")
            language = (
                stream.get_language()
                or ngettext(
                    # Translators: The variable is the number of channels
                    # in an audio track
                    "Undetermined, {} Channel",
                    "Undetermined, {} Channels",
                    channels,
                ).format(channels)
                if (channels := stream.get_channels()) > 0
                else _("Undetermined")
            )

            if (title is not None) and (title == language):
                title = None

            self.language_menu.append(
                f"{language}{(' - ' + title) if (has_title and title) else ''}",
                f"win.select-language(uint16 {index})",
            )
            langs += 1

        if not langs:
            self.language_menu.append(_("No Audio"), "nonexistent.action")
            # HACK: This is to make the item insensitive
            # I don't know if there is a better way to do this

        self.subtitles_menu.append(
            _("None"), f"win.select-subtitles(uint16 {GLib.MAXUINT16})"
        )

        subs = 0
        for index, stream in enumerate(media_info.get_subtitle_streams()):
            has_title, title = stream.get_tags().get_string("title")
            if has_title and title:
                title = unquote(title)
            language = stream.get_language() or _("Undetermined Language")
            if language:
                language = unquote(language)

            self.subtitles_menu.append(
                f"{language}{(' - ' + title) if (has_title and title) else ''}",
                f"win.select-subtitles(uint16 {index})",
            )
            subs += 1

        if not subs and (action := lookup_action(self.props.root, "select-subtitles")):
            action.activate(GLib.Variant.new_uint16(GLib.MAXUINT16))

        self.subtitles_menu.append(_("Add Subtitle File…"), "win.choose-subtitles")

    def _on_toggle_loop(self, action: Gio.SimpleAction, _state: GLib.Variant) -> None:
        value = not action.props.state.get_boolean()
        action.set_state(GLib.Variant.new_boolean(value))
        state_settings.set_boolean("looping", value)
