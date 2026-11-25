# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright 2024-2026 kramo

import ctypes as C
from collections.abc import Callable
from logging import getLogger
from math import isfinite
from threading import Lock, Thread
from urllib.parse import urlsplit

from gi.repository import Gio, GLib, GObject, Gtk
from mpv import (
    MPV,
    ErrorCode,
    MpvEventEndFile,
    MpvEventID,
    MpvGlGetProcAddressFn,
    MpvRenderContext,
)

logger = getLogger("showtime")


class Playback(GObject.Object):
    """One libmpv player and render context per window."""

    changed = GObject.Signal(arg_types=(str,))
    loaded = GObject.Signal()
    seek_done = GObject.Signal()
    end_of_stream = GObject.Signal()
    error = GObject.Signal(arg_types=(str,))

    _media_properties = (
        "time-pos", "duration", "seekable", "paused-for-cache", "eof-reached",
        "media-title", "track-list", "video-out-params",
    )
    _player_properties = ("pause", "volume", "mute", "speed")

    def __init__(self, area: Gtk.GLArea | None, *, hwdec: str = "auto-safe") -> None:
        super().__init__()
        self._area = area
        self._render = None
        self._render_source = 0
        self._render_lock = Lock()
        self._closed = False
        self._entry = None
        self._event_entry = None
        self._seek_pending = False
        self._pending_file = None
        self.uri = None
        self.ready = False
        self.loading = False
        self._values = {"pause": True, "volume": 100.0, "mute": False, "speed": 1.0}
        self._mpv = MPV(
            vo="libmpv" if area is not None else "null",
            ao="pipewire,pulse," if area is not None else "null",
            config=False,
            load_scripts=False,
            input_default_bindings=False,
            pause=True,
            idle=True,
            keep_open="always",
            volume_max=100,
            hwdec=hwdec,
            log_handler=lambda level, prefix, text: logger.debug(
                "mpv %s [%s]: %s", level, prefix, text
            ),
            loglevel="warn",
        )
        if self._mpv.option_info("ytdl") is not None:
            self._mpv.ytdl = False
        for name in self._media_properties + self._player_properties:
            self._mpv.observe_property(name, self._observe)
        self._mpv.register_event_callback(self._event)
        self._area_signals = () if area is None else (
            area.connect("realize", self._realize),
            area.connect("render", self._draw),
            area.connect("unrealize", self._unrealize),
        )

    def _reset_media(self) -> None:
        for name in self._media_properties:
            self._values.pop(name, None)
        self._seek_pending = False

    def _dispatch(self, callback: Callable, *args) -> None:
        def run() -> None:
            if not self._closed:
                callback(*args)

        GLib.idle_add(run)

    def _observe(self, name, value) -> None:
        self._dispatch(self._update, self._event_entry, name, value)

    def _update(self, entry, name, value) -> None:
        if name in self._media_properties and (entry is None or entry != self._entry):
            return
        if self._values.get(name) == value:
            return
        self._values[name] = value
        self.emit("changed", name)
        if name == "eof-reached" and value:
            self.emit("end-of-stream")

    def _event(self, event) -> None:
        match event.event_id.value:
            case MpvEventID.START_FILE:
                self._event_entry = event.data.playlist_entry_id
            case (MpvEventID.FILE_LOADED | MpvEventID.SEEK | MpvEventID.PLAYBACK_RESTART
                  | MpvEventID.VIDEO_RECONFIG):
                self._dispatch(self._handle_event, self._event_entry, event.event_id.value)
            case MpvEventID.END_FILE:
                data = event.data
                self._dispatch(
                    self._handle_end, data.playlist_entry_id, data.reason, data.error,
                    data.playlist_insert_id,
                )

    def _handle_event(self, entry: int, event: int) -> None:
        if entry is None or entry != self._entry:
            return
        match event:
            case MpvEventID.FILE_LOADED:
                self.ready = True
                self.loading = False
                # Observers need not repeat values shared by consecutive files.
                for name in self._media_properties:
                    self._update(entry, name, getattr(self._mpv, name.replace("-", "_")))
                self.emit("loaded")
            case MpvEventID.VIDEO_RECONFIG:
                self._update(entry, "video-out-params", self._mpv.video_out_params)
            case MpvEventID.SEEK:
                self._seek_pending = True
            case MpvEventID.PLAYBACK_RESTART if self._seek_pending:
                self._seek_pending = False
                self._update(entry, "time-pos", self._mpv.time_pos)
                self.emit("seek-done")

    def _handle_end(self, entry: int, reason: int, code: int, redirect: int) -> None:
        if entry != self._entry:
            return
        if reason == MpvEventEndFile.ERROR:
            self._fail(ErrorCode.human_readable(code))
        elif reason == MpvEventEndFile.REDIRECT:
            # The next START_FILE belongs to the playlist expanded by mpv.
            self._entry = redirect

    def _fail(self, message: str) -> None:
        self.ready = self.loading = False
        self._entry = None
        self._pending_file = None
        self._reset_media()
        self.emit("error", message)

    @property
    def position(self) -> int:
        return round((self._values.get("time-pos") or 0) * 1e9)

    @property
    def duration(self) -> int:
        return round((self._values.get("duration") or 0) * 1e9)

    @property
    def seekable(self) -> bool:
        return bool(self._values.get("seekable"))

    @property
    def paused(self) -> bool:
        return bool(self._values["pause"])

    @property
    def buffering(self) -> bool:
        return self.loading or bool(self._values.get("paused-for-cache"))

    @property
    def title(self) -> str | None:
        title = self._values.get("media-title")
        return title.decode(errors="replace") if isinstance(title, bytes) else title

    @property
    def tracks(self) -> list[dict]:
        return self._values.get("track-list") or []

    @property
    def video_size(self) -> tuple[int, int]:
        params = self._values.get("video-out-params") or {}
        width, height = params.get("dw", 0), params.get("dh", 0)
        return (height, width) if params.get("rotate", 0) % 180 else (width, height)

    @property
    def rate(self) -> float:
        return self._values["speed"]

    @rate.setter
    def rate(self, value: float) -> None:
        self._mpv.speed = value

    @property
    def volume(self) -> float:
        return self._values["volume"] / 100

    def set_volume(self, value: float) -> None:
        self._mpv.volume = min(max(value, 0), 1) * 100

    @property
    def mute(self) -> bool:
        return self._values["mute"]

    @mute.setter
    def mute(self, value: bool) -> None:
        self._mpv.mute = value

    def open(self, file: Gio.File) -> None:
        self.uri = uri = file.get_uri()
        self.ready = False
        self.loading = True
        self._entry = None
        self._reset_media()
        self.emit("changed", "loading")
        path = file.get_path()
        if path is None and urlsplit(uri).scheme not in {"http", "https", "rtsp", "smb"}:
            self.stop()
            self._fail("Unsupported media URI")
            return
        self._pending_file = path or uri
        if self._area is None or self._render is not None:
            self._load()
        elif self._area.get_realized():
            self._realize(self._area)

    def _load(self) -> None:
        path, self._pending_file = self._pending_file, None
        if path is None:
            return
        try:
            self._mpv.pause = True
            self._mpv.speed = 1.0
            self._mpv.video_rotate = 0
            self._mpv.aid = "auto"
            self._mpv.sid = "auto"
            self._entry = self._mpv.command("loadfile", path, "replace")["playlist_entry_id"]
        except (KeyError, ValueError, RuntimeError, SystemError) as error:
            self._fail(str(error))

    def play(self) -> None:
        if self.ready:
            self._mpv.pause = False

    def pause(self) -> None:
        self._mpv.pause = True

    def stop(self) -> None:
        self._entry = None
        self._pending_file = None
        self.ready = self.loading = False
        self._reset_media()
        self._mpv.command("stop")
        self.emit("changed", "loading")

    def seek(self, position: int | float) -> None:
        if self.ready and isfinite(position):
            self._command("seek", max(0, position) / 1e9, "absolute", "exact")

    def select_track(self, kind: str, track: int | str) -> None:
        self._mpv[kind] = track

    def add_subtitles(self, file: Gio.File) -> None:
        self._command("sub-add", file.get_path() or file.get_uri(), "select")

    def set_looping(self, looping: bool) -> None:
        self._mpv.loop_file = "inf" if looping else "no"

    def set_subtitle_font(self, family: str, size: float) -> None:
        self._mpv.sub_font = family
        self._mpv.sub_font_size = size

    def rotate(self, angle: int) -> None:
        self._mpv.video_rotate = (self._mpv.video_rotate + angle) % 360

    def _command(self, *args) -> None:
        def done(result) -> None:
            if error := result.exception():
                self._dispatch(logger.warning, "mpv %s: %s", args[0], error)

        self._mpv.command_async(*args).add_done_callback(done)

    @staticmethod
    def _native_display(area: Gtk.GLArea) -> dict:
        # GDK's native Wayland handle is not introspectable (transfer-none pointer).
        display = area.get_display()
        backend = display.__gtype__.name
        match backend:
            case "GdkWaylandDisplay":
                param, getter = "wl_display", "gdk_wayland_display_get_wl_display"
            case "GdkX11Display":
                param, getter = "x11_display", "gdk_x11_display_get_xdisplay"
            case _:
                return {}
        native = getattr(C.CDLL("libgtk-4.so.1"), getter)
        native.argtypes, native.restype = [C.c_void_p], C.c_void_p
        unwrap = C.pythonapi.PyCapsule_GetPointer
        unwrap.argtypes, unwrap.restype = [C.py_object, C.c_char_p], C.c_void_p
        return {param: native(unwrap(display.__gpointer__, None))}

    def _realize(self, area: Gtk.GLArea) -> None:
        area.make_current()
        if error := area.get_error():
            self._fail(error.message)
            return
        try:
            egl = C.CDLL("libEGL.so.1")
            egl.eglGetCurrentContext.restype = C.c_void_p
            if egl.eglGetCurrentContext():
                resolve = egl.eglGetProcAddress
            else:
                resolve = C.CDLL("libGLX.so.0").glXGetProcAddressARB
            resolve.argtypes, resolve.restype = [C.c_char_p], C.c_void_p
            self._resolve = MpvGlGetProcAddressFn(lambda _ctx, name: resolve(name))
            self._get_integer = C.CFUNCTYPE(None, C.c_uint, C.POINTER(C.c_int))(
                resolve(b"glGetIntegerv")
            )
            self._viewport = C.CFUNCTYPE(None, *([C.c_int] * 4))(resolve(b"glViewport"))
            self._render = MpvRenderContext(
                self._mpv,
                "opengl",
                opengl_init_params={"get_proc_address": self._resolve},
                **self._native_display(area),
            )
            self._render.update_cb = self._queue_render
        except (AttributeError, OSError, TypeError, ValueError, RuntimeError) as error:
            self._fail(str(error))
            return
        self._load()

    def _queue_render(self) -> None:
        def render() -> None:
            with self._render_lock:
                self._render_source = 0
            if self._render is not None and not self._closed:
                self._area.queue_render()

        with self._render_lock:
            if not self._closed and not self._render_source:
                self._render_source = GLib.idle_add(render, priority=GLib.PRIORITY_HIGH_IDLE)

    def _draw(self, _area: Gtk.GLArea, _context) -> bool:
        if self._render is None:
            return False
        fbo, viewport = C.c_int(), (C.c_int * 4)()
        self._get_integer(0x8CA6, C.byref(fbo))  # GL_DRAW_FRAMEBUFFER_BINDING
        self._get_integer(0x0BA2, viewport)  # GL_VIEWPORT
        try:
            self._render.render(
                opengl_fbo={"fbo": fbo.value, "w": viewport[2], "h": viewport[3]},
                flip_y=True,
                block_for_target_time=False,
            )
        except (ValueError, RuntimeError) as error:
            self._dispatch(self._fail, str(error))
        finally:
            self._viewport(*viewport)
        return True

    def _unrealize(self, area: Gtk.GLArea) -> None:
        if self._render is not None:
            area.make_current()
            # Free also removes the update callback while its Python trampoline is alive.
            self._render.free()
            self._render = None
        with self._render_lock:
            if self._render_source:
                GLib.source_remove(self._render_source)
                self._render_source = 0

    def close(self) -> Thread | None:
        if self._closed:
            return None
        with self._render_lock:
            self._closed = True
        if self._area is not None:
            self._unrealize(self._area)
            for signal in self._area_signals:
                self._area.disconnect(signal)
            self._area = None
        # Network inputs can delay termination; all GTK and GL cleanup is already done.
        worker = Thread(target=self._mpv.terminate, name="showtime-mpv-close")
        worker.start()
        return worker
