from gi.repository import Adw, GObject, Gtk

from showtime import PREFIX


class VideoMetadata(GObject.Object):
    """Video metadata object."""

    duration = GObject.Property(type=str, default="")
    title = GObject.Property(type=str, default="")
    video_bitrate = GObject.Property(type=str, default="")
    resolution = GObject.Property(type=str, default="")
    framerate = GObject.Property(type=str, default="")
    video_codec = GObject.Property(type=str, default="")
    audio_bitrate = GObject.Property(type=str, default="")
    audio_channel = GObject.Property(type=str, default="")
    audio_codec = GObject.Property(type=str, default="")
    sample_rate = GObject.Property(type=str, default="")
    comment = GObject.Property(type=str, default="")
    artist = GObject.Property(type=str, default="")
    description = GObject.Property(type=str, default="")


@Gtk.Template.from_resource(f"{PREFIX}/gtk/video-metadata.ui")
class VideoMetadataDialog(Adw.Dialog):
    "Display video metadata."

    __gtype_name__ = "VideoMetadata"
    row_duration: Adw.ActionRow = Gtk.Template.Child()
    row_title: Adw.ActionRow = Gtk.Template.Child()
    row_video_bitrate: Adw.ActionRow = Gtk.Template.Child()
    row_resolution: Adw.ActionRow = Gtk.Template.Child()
    row_framerate: Adw.ActionRow = Gtk.Template.Child()
    row_video_codec: Adw.ActionRow = Gtk.Template.Child()
    row_audio_bitrate: Adw.ActionRow = Gtk.Template.Child()
    row_audio_channel: Adw.ActionRow = Gtk.Template.Child()
    row_audio_codec: Adw.ActionRow = Gtk.Template.Child()
    row_sample_rate: Adw.ActionRow = Gtk.Template.Child()
    row_comment: Adw.ActionRow = Gtk.Template.Child()
    row_artist: Adw.ActionRow = Gtk.Template.Child()
    row_description: Adw.ActionRow = Gtk.Template.Child()

    metadata: VideoMetadata = VideoMetadata()

    def __init__(self):
        super().__init__()
        for prop in self.metadata.list_properties():
            prop_name = prop.get_name()
            ui_row = getattr(self, f"row_{prop_name.replace('-', '_')}")
            self.metadata.bind_property(
                prop_name, ui_row, "subtitle", GObject.BindingFlags.DEFAULT
            )
            self.metadata.bind_property(
                prop_name,
                ui_row,
                "visible",
                GObject.BindingFlags.DEFAULT,
                lambda _binding, value: bool(value),
            )

    def set_metadata(self, metadata: VideoMetadata) -> None:
        "Update metadata values, automatically triggers ui change."
        for meta in metadata.list_properties():
            self.metadata.set_property(
                meta.get_name(), metadata.get_property(meta.get_name())
            )
