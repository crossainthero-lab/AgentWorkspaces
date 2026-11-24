"""Tiny libxkbcommon binding (ctypes) used to build a keymap for the virtual keyboard
and to map characters to evdev keycodes + modifier masks. No compositor access."""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import Dict, Optional, Tuple

_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    lib = ctypes.CDLL(name)
    c_void_p, c_char_p, c_uint32, c_int = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_int
    lib.xkb_context_new.restype = c_void_p
    lib.xkb_context_new.argtypes = [c_int]
    lib.xkb_keymap_new_from_names.restype = c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [c_void_p, c_void_p, c_int]
    lib.xkb_keymap_get_as_string.restype = ctypes.c_void_p
    lib.xkb_keymap_get_as_string.argtypes = [c_void_p, c_int]
    lib.xkb_keymap_min_keycode.restype = c_uint32
    lib.xkb_keymap_min_keycode.argtypes = [c_void_p]
    lib.xkb_keymap_max_keycode.restype = c_uint32
    lib.xkb_keymap_max_keycode.argtypes = [c_void_p]
    lib.xkb_keymap_num_layouts_for_key.restype = c_uint32
    lib.xkb_keymap_num_layouts_for_key.argtypes = [c_void_p, c_uint32]
    lib.xkb_keymap_num_levels_for_key.restype = c_uint32
    lib.xkb_keymap_num_levels_for_key.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.xkb_keymap_key_get_syms_by_level.restype = c_int
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        c_void_p, c_uint32, c_uint32, c_uint32, ctypes.POINTER(ctypes.POINTER(c_uint32))]
    lib.xkb_keymap_key_get_mods_for_level.restype = c_uint32
    lib.xkb_keymap_key_get_mods_for_level.argtypes = [
        c_void_p, c_uint32, c_uint32, c_uint32, ctypes.POINTER(c_uint32), c_uint32]
    lib.xkb_keysym_to_utf32.restype = c_uint32
    lib.xkb_keysym_to_utf32.argtypes = [c_uint32]
    lib.xkb_keysym_from_name.restype = c_uint32
    lib.xkb_keysym_from_name.argtypes = [c_char_p, c_int]
    lib.xkb_keymap_mod_get_index.restype = c_uint32
    lib.xkb_keymap_mod_get_index.argtypes = [c_void_p, c_char_p]
    lib.xkb_keymap_unref.argtypes = [c_void_p]
    lib.xkb_context_unref.argtypes = [c_void_p]
    _lib = lib
    return lib


class _RuleNames(ctypes.Structure):
    _fields_ = [("rules", ctypes.c_char_p), ("model", ctypes.c_char_p),
                ("layout", ctypes.c_char_p), ("variant", ctypes.c_char_p),
                ("options", ctypes.c_char_p)]


class Keymap:
    """A compiled keymap for a fixed layout, independent of any user/host configuration."""

    def __init__(self, layout: str = "us", variant: str = "", model: str = "pc105"):
        lib = _load()
        self._lib = lib
        self._ctx = lib.xkb_context_new(0)
        names = _RuleNames(b"evdev", model.encode(), layout.encode(),
                           variant.encode() if variant else None, None)
        self._km = lib.xkb_keymap_new_from_names(self._ctx, ctypes.byref(names), 0)
        if not self._km:
            raise RuntimeError(f"xkb: cannot compile layout {layout!r}")
        ptr = lib.xkb_keymap_get_as_string(self._km, 1)
        self.text: bytes = ctypes.string_at(ptr) + b"\0"
        ctypes.CDLL(None).free(ctypes.c_void_p(ptr))
        self.mod_index = {n: lib.xkb_keymap_mod_get_index(self._km, n.encode())
                          for n in ("Shift", "Control", "Mod1", "Mod4", "Lock", "Mod2", "Mod5")}
        self.char_map: Dict[str, Tuple[int, int]] = {}
        self._build_char_map()

    def _build_char_map(self):
        lib = self._lib
        lo, hi = lib.xkb_keymap_min_keycode(self._km), lib.xkb_keymap_max_keycode(self._km)
        for kc in range(lo, hi + 1):
            if lib.xkb_keymap_num_layouts_for_key(self._km, kc) < 1:
                continue
            for lvl in range(lib.xkb_keymap_num_levels_for_key(self._km, kc, 0)):
                syms = ctypes.POINTER(ctypes.c_uint32)()
                n = lib.xkb_keymap_key_get_syms_by_level(self._km, kc, 0, lvl, ctypes.byref(syms))
                if n != 1:
                    continue
                cp = lib.xkb_keysym_to_utf32(syms[0])
                if cp == 0:
                    continue
                masks = (ctypes.c_uint32 * 4)()
                m = lib.xkb_keymap_key_get_mods_for_level(self._km, kc, 0, lvl, masks, 4)
                if m < 1:
                    continue
                mask = masks[0]
                ch = chr(cp)
                prev = self.char_map.get(ch)
                # prefer the plainest way to type a character (fewest modifier bits)
                if prev is None or bin(mask).count("1") < bin(prev[1]).count("1"):
                    self.char_map[ch] = (kc - 8, mask)  # evdev code = xkb keycode - 8
        # control characters
        self.char_map.setdefault("\n", (28, 0))
        self.char_map.setdefault("\t", (15, 0))

    def mod_mask(self, *names: str) -> int:
        mask = 0
        for n in names:
            idx = self.mod_index.get({"shift": "Shift", "ctrl": "Control", "control": "Control",
                                      "alt": "Mod1", "super": "Mod4", "meta": "Mod4",
                                      "logo": "Mod4"}.get(n.lower(), n))
            if idx is not None and idx != 0xFFFFFFFF:
                mask |= 1 << idx
        return mask

    def char_to_key(self, ch: str) -> Optional[Tuple[int, int]]:
        return self.char_map.get(ch)

    def __del__(self):
        try:
            self._lib.xkb_keymap_unref(self._km)
            self._lib.xkb_context_unref(self._ctx)
        except Exception:
            pass


# Linux evdev codes for named keys (linux/input-event-codes.h). Stable kernel ABI.
KEYS: Dict[str, int] = {
    "esc": 1, "escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "leftbrace": 26, "rightbrace": 27, "return": 28, "enter": 28,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, "comma": 51, "dot": 52, "period": 52,
    "slash": 53, "space": 57, "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65, "f8": 66, "f9": 67, "f10": 68,
    "f11": 87, "f12": 88,
    "home": 102, "up": 103, "pageup": 104, "page_up": 104, "left": 105, "right": 106, "end": 107,
    "down": 108, "pagedown": 109, "page_down": 109, "insert": 110, "delete": 111,
}
MODIFIER_KEYS = {"ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125, "logo": 125}
