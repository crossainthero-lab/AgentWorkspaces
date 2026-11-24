"""Unit tests (no PipeWire needed): audio configuration, environment and analysis."""
import math
import os
import stat
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from aw import audio  # noqa: E402


def tone(freq, seconds=1.0, amp=9000, rate=48000, channels=2):
    n = int(seconds * rate)
    return b"".join(struct.pack("<" + "h" * channels, *([int(amp * math.sin(2 * math.pi * freq * i / rate))] * channels))
                    for i in range(n))


class AudioPure(unittest.TestCase):
    def test_private_graph_has_no_hardware(self):
        c = audio.PIPEWIRE_CONF
        self.assertIn("support.null-audio-sink", c)
        self.assertIn("media.class      = Audio/Sink", c)
        for hw in ("api.alsa", "alsa", "bluez", "v4l2", "libpipewire-module-portal", "module-rt", "jack"):
            self.assertNotIn(hw, c, f"private pipewire must not load {hw}")
        self.assertNotIn("alsa", audio.PULSE_CONF)

    def test_daemon_env_is_private_and_has_no_system_bus(self):
        env = audio.daemon_env("/w/x", "/w/x/rt", "/usr/bin", "unix:path=/w/x/rt/bus")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/w/x/rt")
        self.assertEqual(env["PIPEWIRE_REMOTE"], "/w/x/rt/pipewire-0")
        self.assertEqual(env["DBUS_SYSTEM_BUS_ADDRESS"], "unix:path=/nonexistent")
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)
        for k, v in env.items():
            if k != "PATH":
                self.assertNotIn("/run/user/1000/pipewire", v)

    def test_client_env_points_at_own_server_or_nowhere(self):
        on = audio.client_env("/w/rt", True, "/w/audio")
        off = audio.client_env("/w/rt", False, "/w/audio")
        self.assertEqual(on["PULSE_SERVER"], "unix:/w/rt/pulse/native")
        self.assertEqual(off["PULSE_SERVER"], "unix:/w/rt/pulse/native-disabled")
        self.assertEqual(on["PULSE_CLIENTCONFIG"], "/w/audio/pulse-client.conf")
        self.assertIn("autospawn = no", audio.PULSE_CLIENT_CONF)

    def test_configs_are_private_files(self):
        with tempfile.TemporaryDirectory() as d:
            out = audio.write_configs(os.path.join(d, "audio"))
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "audio")).st_mode), 0o700)
            self.assertEqual(set(out), {"pipewire.conf", "pipewire-pulse.conf", "pulse-client.conf"})

    def test_analysis_tone_and_silence(self):
        r = audio.analyse_pcm16(tone(440))
        self.assertAlmostEqual(r["tone_hz"], 440, delta=2)
        self.assertGreater(r["rms_dbfs"], -20)
        self.assertLess(r["rms_dbfs"], -10)
        r = audio.analyse_pcm16(tone(1000, amp=3000))
        self.assertAlmostEqual(r["tone_hz"], 1000, delta=5)
        s = audio.analyse_pcm16(b"\0" * 96000)
        self.assertEqual((s["peak"], s["tone_hz"]), (0.0, None))
        self.assertEqual(audio.analyse_pcm16(b"")["frames"], 0)

    def test_wav_roundtrip(self):
        import wave
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.wav")
            audio.write_wav(p, tone(440, 0.2))
            with wave.open(p) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (2, 48000, 2))
                self.assertEqual(w.getnframes(), 9600)


if __name__ == "__main__":
    unittest.main()
