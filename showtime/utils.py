# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlparse

from gi.repository import Gdk, Gio, GLib, Graphene, Gtk

SECONDS_ONLY = 2


def screenshot(
    paintable: Gdk.Paintable, native: Gtk.Native, size: tuple[int, int]
) -> GLib.Bytes | None:
    """Capture the displayed video without the GTK controls or letterbox bars."""
    video_width, video_height = size
    paintable = paintable.get_current_image()
    width, height = paintable.get_intrinsic_width(), paintable.get_intrinsic_height()
    if not (video_width and video_height and width and height and (renderer := native.get_renderer())):
        return None

    # Adapted from Workbench's paintable snapshot (upstream Showtime's screenshot path).
    # shortcut: captures at viewport resolution; use native frame capture once hwdec screenshots work reliably.
    scale = min(width / video_width, height / video_height)
    w, h = video_width * scale, video_height * scale
    rect = Graphene.Rect().init((width - w) / 2, (height - h) / 2, w, h)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    if node := snapshot.to_node():
        return renderer.render_texture(node, rect).save_to_png_bytes()
    return None


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


def get_title(title: str | None, uri: str | None) -> str | None:
    """Prefer the media title over the filename."""
    path = Path(unquote(urlparse(uri).path)) if uri else None
    if title and title not in {"Video", "Audio"} and (not path or title != path.name):
        return title
    return path.stem if path else None


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


def get_file_hash_identifier(uri: str) -> str:
    """Create a SHA256 hash using the file path."""
    file_path = uri
    if os.getenv("FLATPAK_ID") and uri.startswith("file:"):
        try:
            file_info = Gio.File.new_for_uri(uri).query_info(
                "xattr::document-portal.host-path", Gio.FileQueryInfoFlags.NONE
            )
            file_path = file_info.get_attribute_as_string("xattr::document-portal.host-path") or uri
        except GLib.Error:
            pass

    return sha256(file_path.encode("utf-8")).hexdigest()
