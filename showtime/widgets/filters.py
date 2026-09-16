from gi.repository import Adw, Gtk, GObject

from showtime import PREFIX


@Gtk.Template.from_resource(f"{PREFIX}/gtk/filters.ui")
class Filters(Adw.Bin):
    """A bin containing the video filters."""

    __gtype_name__ = "Filters"

    menu_video_filters: Gtk.MenuButton = Gtk.Template.Child()
    popover_filters: Gtk.PopoverMenu = Gtk.Template.Child()

    brightness_input: Gtk.Scale = Gtk.Template.Child()
    brightness = 1.0
    saturation_input: Gtk.Scale = Gtk.Template.Child()
    saturation = 1.0
    contrast_input: Gtk.Scale = Gtk.Template.Child()
    contrast = 1.0

    brightness_change = GObject.Signal(arg_types=[float])
    reset_brightness = GObject.Signal()
    saturation_change = GObject.Signal(arg_types=[float])
    reset_saturation = GObject.Signal()
    contrast_change = GObject.Signal(arg_types=[float])
    reset_contrast = GObject.Signal()

    @Gtk.Template.Callback()
    def _brightness_change(self, scale: Gtk.Scale) -> None:
        adjustment = scale.get_adjustment()
        value = adjustment.get_value()
        self.brightness = value
        self.emit(
            "brightness-change",
            value,
        )

    @Gtk.Template.Callback()
    def _reset_brightness(self, *_args) -> None:
        # Automatically triggers brightness-change
        self.brightness = 1.0
        self.brightness_input.set_value(1.0)

    @Gtk.Template.Callback()
    def _saturation_change(self, scale: Gtk.Scale) -> None:
        adjustement = scale.get_adjustment()
        value = adjustement.get_value()
        self.saturation = value
        self.emit("saturation-change", value)

    @Gtk.Template.Callback()
    def _reset_saturation(self, *_args) -> None:
        self.saturation = 1.0
        self.saturation_input.set_value(1.0)

    @Gtk.Template.Callback()
    def _contrast_change(self, scale: Gtk.Scale) -> None:
        adjustement = scale.get_adjustment()
        value = adjustement.get_value()
        self.contrast = value
        self.emit("saturation-change", value)

    @Gtk.Template.Callback()
    def _reset_contrast(self, *_args) -> None:
        self.contrast = 1.0
        self.contrast_input.set_value(1.0)
