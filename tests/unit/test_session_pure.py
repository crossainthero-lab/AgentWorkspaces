"""UNIT tests: PNG encoding, pixel format conversion, keymap, wire protocol (socketpair fake server)."""
import os
import socket
import struct
import time
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import wl, xkb  # noqa: E402
from aw.session import encode_png, to_rgb, SessionError  # noqa: E402


def decode_png(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(png):
        ln, tag = struct.unpack(">I4s", png[pos:pos + 8])
        data = png[pos + 8:pos + 8 + ln]
        crc = struct.unpack(">I", png[pos + 8 + ln:pos + 12 + ln])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF
        if tag == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", data)
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    w, h = ihdr[0], ihdr[1]
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h)]
    assert all(raw[y * (w * 3 + 1)] == 0 for y in range(h))
    return w, h, b"".join(rows)


class PngAndFormats(unittest.TestCase):
    def test_png_roundtrip(self):
        w, h = 5, 3
        rgb = bytes((x * 17 + y * 5 + c) % 256 for y in range(h) for x in range(w) for c in range(3))
        self.assertEqual(decode_png(encode_png(w, h, rgb)), (w, h, rgb))

    def test_xrgb8888_to_rgb(self):
        # memory B,G,R,X
        buf = bytes([10, 20, 30, 0, 40, 50, 60, 0])
        self.assertEqual(to_rgb(buf, 2, 1, 8, 1), bytes([30, 20, 10, 60, 50, 40]))

    def test_xbgr8888_to_rgb(self):
        buf = bytes([1, 2, 3, 255, 4, 5, 6, 255])  # R,G,B,X
        self.assertEqual(to_rgb(buf, 2, 1, 8, 0x34324258), bytes([1, 2, 3, 4, 5, 6]))

    def test_bg24_passthrough_with_stride_padding(self):
        buf = bytes([1, 2, 3, 4, 5, 6, 0, 0, 7, 8, 9, 10, 11, 12, 0, 0])  # 2x2, stride 8
        self.assertEqual(to_rgb(buf, 2, 2, 8, 0x34324742), bytes(range(1, 13)))

    def test_y_invert(self):
        buf = bytes([1, 1, 1, 0, 2, 2, 2, 0])  # two 1-pixel rows, xrgb
        self.assertEqual(to_rgb(buf, 1, 2, 4, 1, y_invert=True), bytes([2, 2, 2, 1, 1, 1]))

    def test_unknown_format_rejected(self):
        with self.assertRaises(SessionError):
            to_rgb(b"\0" * 16, 2, 2, 8, 0xDEADBEEF)


class Keymap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.km = xkb.Keymap("us")

    def test_plain_and_shifted(self):
        code_a, mod_a = self.km.char_to_key("a")
        code_A, mod_A = self.km.char_to_key("A")
        self.assertEqual(code_a, code_A)
        self.assertEqual(mod_a, 0)
        self.assertEqual(mod_A, self.km.mod_mask("shift"))
        self.assertEqual(code_a, xkb.KEYS["a"])

    def test_symbols(self):
        self.assertEqual(self.km.char_to_key("!")[1], self.km.mod_mask("shift"))
        self.assertEqual(self.km.char_to_key(" ")[0], xkb.KEYS["space"])
        self.assertIsNone(self.km.char_to_key("é"))  # not in the us layout

    def test_newline_and_tab_use_return_and_tab_keys(self):
        # regression: the us layout has a <LNFD> Linefeed key; "\n" must still type Return (evdev 28)
        self.assertEqual(self.km.char_to_key("\n"), (28, 0))
        self.assertEqual(self.km.char_to_key("\t"), (15, 0))

    def test_mod_masks_distinct(self):
        masks = {self.km.mod_mask(m) for m in ("shift", "ctrl", "alt", "super")}
        self.assertEqual(len(masks), 4)

    def test_keymap_text(self):
        self.assertTrue(self.km.text.startswith(b"xkb_keymap"))
        self.assertTrue(self.km.text.endswith(b"\0"))


class FakeCompositor(unittest.TestCase):
    """The wire codec against a hand-written fake server on a socketpair."""

    def test_registry_globals_and_roundtrip(self):
        a, b = socket.socketpair()
        # Pre-load server replies: registry.global x2 then callback.done. Object ids: registry=2, callback=3.
        def msg(oid, opcode, payload):
            return struct.pack("<II", oid, ((8 + len(payload)) << 16) | opcode) + payload

        def s(text):
            t = text.encode() + b"\0"
            return struct.pack("<I", len(t)) + t + b"\0" * (-len(t) % 4)
        b.sendall(msg(2, 0, struct.pack("<I", 1) + s("wl_seat") + struct.pack("<I", 7))
                  + msg(2, 0, struct.pack("<I", 2) + s("zwlr_virtual_pointer_manager_v1") + struct.pack("<I", 2))
                  + msg(3, 0, struct.pack("<I", 0)))
        c = wl.Connection(sock=a)
        self.assertEqual(c.globals["wl_seat"], (1, 7))
        self.assertEqual(c.globals["zwlr_virtual_pointer_manager_v1"], (2, 2))
        sent = b.recv(4096)
        # first request: wl_display.get_registry(new_id=2)
        oid, w2 = struct.unpack_from("<II", sent)
        self.assertEqual((oid, w2 & 0xFFFF, struct.unpack_from("<I", sent, 8)[0]), (1, 1, 2))
        # second: wl_display.sync(new_id=3)
        oid2, w22 = struct.unpack_from("<II", sent, 12)
        self.assertEqual((oid2, w22 & 0xFFFF, struct.unpack_from("<I", sent, 20)[0]), (1, 0, 3))

    def test_roundtrip_does_not_wait_out_its_timeout_when_another_thread_consumed_the_done_event(self):
        """Regression: the session's pump thread and a caller can dispatch on the same connection; when the pump
        consumed `callback.done`, roundtrip() used to sit in select() for the whole 5 s timeout (a window.set_geometry
        call that stalled 5 s, and for a busy viewer much longer) and then return as if nothing was wrong."""
        a, b = socket.socketpair()

        def msg(oid, opcode, payload):
            return struct.pack("<II", oid, ((8 + len(payload)) << 16) | opcode) + payload
        b.sendall(msg(3, 0, struct.pack("<I", 0)))                 # done for the registry roundtrip
        c = wl.Connection(sock=a)
        real = c.dispatch
        calls = []

        def racing_dispatch(timeout=0):
            if not calls:                                           # "the other thread" gets the event first
                calls.append(1)
                b.sendall(msg(4, 0, struct.pack("<I", 0)))          # callback id 4 = this roundtrip's sync
                real(0)
            return real(timeout)
        c.dispatch = racing_dispatch
        t0 = time.monotonic()
        c.roundtrip(timeout=5.0)
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_socket_path_must_be_absolute_and_exist(self):
        with self.assertRaises(wl.WaylandError):
            wl.Connection("wayland-0")  # relative: never resolved from env
        with self.assertRaises(wl.WaylandError):
            wl.Connection("/nonexistent/wayland-0")


if __name__ == "__main__":
    unittest.main()
