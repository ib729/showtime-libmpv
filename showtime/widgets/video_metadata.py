from gi.repository import Adw, Gtk

from showtime import PREFIX


@Gtk.Template.from_resource(f"{PREFIX}/gtk/video-metadata.ui")
class VideoMetadata(Adw.Dialog):
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

    def __init__(self):
        super().__init__()
