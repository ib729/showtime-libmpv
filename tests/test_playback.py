# SPDX-License-Identifier: GPL-3.0-or-later

import threading
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib

from play import Playback


class PlaybackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.file = self.make_clip("hello world.wav", 4)
        self.player = Playback(None, hwdec="no")
        self.addCleanup(self.close_player)
        self.errors = []
        self.player.connect("error", lambda _, message: self.errors.append(message))

    def close_player(self) -> None:
        if worker := self.player.close():
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "mpv did not shut down")

    def make_clip(self, name: str, length: int) -> Gio.File:
        path = Path(self.directory.name, name)
        with wave.open(str(path), "wb") as clip:
            clip.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            clip.writeframes(bytes(length * 16000))
        return Gio.File.new_for_path(str(path))

    def wait_for(self, predicate) -> None:
        loop = GLib.MainLoop()

        def check(*_args) -> None:
            if predicate():
                loop.quit()

        def expire() -> bool:
            loop.quit()
            return GLib.SOURCE_CONTINUE

        ids = [
            self.player.connect(signal, check)
            for signal in ("changed", "loaded", "error", "seek-done", "end-of-stream")
        ]
        source = GLib.timeout_add_seconds(3, expire)
        try:
            if not predicate():
                loop.run()
            self.assertTrue(predicate(), f"Timed out; mpv errors: {self.errors}")
        finally:
            GLib.source_remove(source)
            for signal in ids:
                self.player.disconnect(signal)

    def test_load_seek_and_controls(self) -> None:
        self.assertEqual(self.player.duration, 0)
        self.assertFalse(self.player.seekable)
        self.player.open(self.file)
        self.wait_for(lambda: self.player.ready and self.player.seekable and self.player.duration)
        self.assertTrue(self.player.paused)
        self.assertEqual(self.player.duration, 4_000_000_000)
        seeks = []
        self.player.connect("seek-done", lambda player: seeks.append(
            (player.position, threading.current_thread())
        ))
        self.player.seek(2_000_000_000)
        self.wait_for(lambda: bool(seeks))
        self.assertAlmostEqual(seeks[-1][0] / 1e9, 2, delta=0.05)
        self.assertIs(seeks[-1][1], threading.main_thread())
        self.player.set_volume(0.25)
        self.player.mute = True
        self.player.rate = 1.5
        self.player.play()
        self.wait_for(lambda: self.player.volume == 0.25 and self.player.mute and
                      self.player.rate == 1.5 and not self.player.paused)
        self.player.pause()
        self.wait_for(lambda: self.player.paused)
        self.player.stop()
        self.assertFalse(self.player.ready)
        self.assertEqual(self.player.duration, 0)
        self.assertFalse(self.errors)

    def test_rapid_replacement_ignores_old_events(self) -> None:
        bad = self.file.get_parent().get_child("missing.mp4")
        second = self.make_clip("replacement.wav", 1)
        self.player.open(bad)
        self.player.open(self.file)
        self.player.open(second)
        self.wait_for(lambda: self.player.ready and self.player.duration == 1_000_000_000)
        self.assertEqual(self.player.uri, second.get_uri())
        self.assertFalse(self.errors)

    def test_errors_and_recovery(self) -> None:
        path = Path(self.directory.name, "bad.mp4")
        path.write_text("not a video")
        self.player.open(Gio.File.new_for_path(str(path)))
        self.wait_for(lambda: bool(self.errors))
        self.assertFalse(self.player.ready)
        self.assertFalse(self.player.buffering)
        self.player.open(self.file)
        self.wait_for(lambda: self.player.ready)
        self.assertEqual(len(self.errors), 1)

    def test_end_of_stream(self) -> None:
        ended = []
        self.player.connect("end-of-stream", lambda _: ended.append(True))
        self.player.open(self.make_clip("short.wav", 1))
        self.wait_for(lambda: self.player.ready and self.player.seekable)
        self.player.play()
        self.wait_for(lambda: bool(ended))
        self.assertTrue(self.player.ready)


if __name__ == "__main__":
    unittest.main()
