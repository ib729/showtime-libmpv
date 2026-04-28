# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlparse

from gi.repository import (
    Gdk,
    Gio,
    Graphene,
    Gst,
    GstPbutils,
    GstPlay,  # pyright: ignore[reportAttributeAccessIssue]
    Gtk,
)

from showtime import logger

SECONDS_ONLY = 2

# Audio channels
CHANNELS_MONO = 1
CHANNELS_MIN_STEREO = 2
CHANNELS_MAX_STEREO = 3
CHANNELS_MIN_SURROUND = 4


def screenshot(paintable: Gdk.Paintable, native: Gtk.Native) -> Gdk.Texture | None:
    """Take a screenshot of the current image of a `GdkPaintable`."""
    # Copied from Workbench
    # https://github.com/workbenchdev/Workbench/blob/1ebbe1e3915aabfd172c166c88ca23ad08861d15/src/Previewer/previewer.vala#L36

    paintable = paintable.get_current_image()
    width = paintable.get_intrinsic_width()
    height = paintable.get_intrinsic_height()

    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)

    if not (node := snapshot.to_node()):
        logger.warning(
            "Could not get node snapshot, width: %s, height: %s.", width, height
        )
        return None

    rect = Graphene.Rect()
    rect.origin = Graphene.Point.zero()
    size = Graphene.Size()
    size.width = width
    size.height = height
    rect.size = size

    return (
        renderer.render_texture(node, rect)
        if (renderer := native.get_renderer())
        else None
    )


def nanoseconds_to_timestamp(ns: int, *, hours: bool = False) -> str:
    """Convert `ns` to a human readable time stamp.

    In the format 1:23 or 1:23:45 depending on length.

    If `hours` is set to True, always return a string in the format 01:23:45.
    """
    formatted = (
        (datetime.min.replace(tzinfo=UTC) + timedelta(microseconds=int(ns / 1000)))
        .time()
        .strftime("%H:%M:%S")
    )

    return (
        formatted
        if hours
        else formatted.lstrip("0:") or "0"
        if len(stripped := formatted.lstrip("0:") or "0") > SECONDS_ONLY
        else f"0:{stripped:0>2}"
    )


def get_title(media_info: GstPlay.PlayMediaInfo | None) -> str | None:
    """Get the title of the video from a `GstPlayMediaInfo`."""
    return (
        (
            title
            if (title := media_info.get_title())
            and title
            not in (
                "Video",
                "Audio",
            )
            else Path(unquote(urlparse(media_info.get_uri()).path)).stem
        )
        if media_info
        else None
    )


def lookup_action(action_map: object, name: str) -> Gio.SimpleAction | None:
    """Look up an action in `action_map` with type checking."""
    if isinstance(action_map, Gio.ActionMap) and isinstance(
        action := action_map.lookup_action(name), Gio.SimpleAction
    ):
        return action

    return None


def get_subtitle_font_desc() -> str | None:
    """Get a font description scaled to the user's preferences.

    Ideal for rendering subtitles.
    """
    if not (settings := Gtk.Settings.get_default()):
        return None

    font_name = settings.props.gtk_font_name

    try:
        size_str = font_name.rsplit(" ", 1)[1]
        size = float(size_str)
    except (ValueError, IndexError):
        return font_name
    else:
        # TODO: Can I always assume that 72 is the default unscaled DPI? Probably not…
        new_size = size * ((settings.props.gtk_xft_dpi / 1024) / 72)
        return font_name[: len(font_name) - len(size_str)] + str(round(new_size))


def convert_bps_to_kbps(bps: int) -> int:
    "Convert bits per second to kilobits per second."
    return int(bps / 1000)


def format_sample_rate(sample_rate: int) -> float:
    "Convert sample rate to kHz."
    return sample_rate / 1000


class Metadata:
    "Video metadata class."

    video_bitrate: str = ""
    video_height: str = ""
    video_width: str = ""
    video_framerate: str = ""
    video_codec: str = ""
    video_title: str = ""
    audio_bitrate: str = ""
    audio_channel: str = ""
    audio_codec: str = ""
    audio_sample_rate: str = ""
    file_comment: str = ""
    file_artist: str = ""
    file_description: str = ""

    def __init__(
        self,
        video_bitrate: str,
        video_height: str,
        video_width: str,
        video_framerate: str,
        video_codec: str,
        video_title: str,
        audio_bitrate: str,
        audio_channel: str,
        audio_codec: str,
        audio_sample_rate: str,
        file_comment: str,
        file_artist: str,
        file_description: str,
    ):
        self.video_bitrate = video_bitrate
        self.video_height = video_height
        self.video_width = video_width
        self.video_framerate = video_framerate
        self.video_codec = video_codec.upper()
        self.video_title = video_title
        self.audio_bitrate = audio_bitrate
        self.audio_channel = audio_channel
        self.audio_codec = audio_codec.upper()
        self.audio_sample_rate = audio_sample_rate
        self.file_comment = file_comment
        self.file_artist = file_artist
        self.file_description = file_description


def get_metadata(video_file: Gio.File) -> Metadata:
    "Get audio and video metadata."
    discoverer_video_info = GstPbutils.DiscovererVideoInfo
    discoverer = GstPbutils.Discoverer()
    info = discoverer.discover_uri(video_file.get_uri())

    # App only allow single video stream
    video_stream = info.get_video_streams()[0]

    video_bitrate = f"{convert_bps_to_kbps(video_stream.get_bitrate())} kbps"
    video_height = discoverer_video_info.get_height(video_stream)
    video_width = discoverer_video_info.get_width(video_stream)
    video_framerate = discoverer_video_info.get_framerate_num(video_stream)
    video_codec = ""
    video_title = str(video_file.get_basename())

    # Only check default audio stream
    audio_stream = info.get_audio_streams()[0]

    audio_bitrate = f"{convert_bps_to_kbps(audio_stream.get_bitrate())} kbps"
    audio_channel = ""
    audio_channels_qty = audio_stream.get_channels()

    if audio_channels_qty == CHANNELS_MONO:
        audio_channel = "Mono"
    elif (
        audio_channels_qty >= CHANNELS_MIN_STEREO
        and audio_channels_qty < CHANNELS_MAX_STEREO
    ):
        audio_channel = "Stereo"
    elif audio_channels_qty >= CHANNELS_MIN_SURROUND:
        audio_channel = "Surround"

    audio_codec = ""
    audio_sample_rate = f"{format_sample_rate(audio_stream.get_sample_rate()):.2f} kHz"
    file_comment = ""
    file_artist = ""
    file_description = ""

    video_caps = (
        cast(Gst.Caps, discoverer_video_info.get_caps(video_stream))
        .to_string()
        .split(",")
    )
    for cap in video_caps:
        # Video codec is more easy to extract from caps
        if "video" in cap:
            video_codec = cap.split("-")[1]

    def extract_tags_info(taglist: Gst.TagList, tag: str, _user_data):
        nonlocal file_artist, file_comment, file_description, audio_codec
        _success, value = taglist.get_string(tag)
        if tag == "comment":
            file_comment = value
        elif tag == "artist":
            file_artist = value
        elif tag == "description":
            file_description = value
        elif tag == "audio-codec":
            audio_codec = value

    tags = cast(Gst.TagList, info.get_tags())
    tags.foreach(extract_tags_info, None)
    return Metadata(
        video_bitrate,
        str(video_height),
        str(video_width),
        f"{video_framerate} FPS",
        video_codec,
        video_title,
        audio_bitrate,
        audio_channel,
        audio_codec,
        audio_sample_rate,
        file_comment,
        file_artist,
        file_description,
    )
