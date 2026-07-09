#!/usr/bin/env python3
"""
MagicBridge USB HID Keyboard and Mouse Report Handler

Translates browser WebSocket input events (JavaScript event.code)
into raw USB HID reports written to /dev/hidg0 (keyboard) and /dev/hidg1 (mouse).

Keyboard report (8 bytes): [modifiers, 0x00, key1..key6]
Mouse report (4 bytes):    [buttons, dx, dy, wheel]
"""
import struct
import threading
import time
import random
import logging

log = logging.getLogger("magicbridge.hid")

# Keyboard scancode table
# JS event.code → USB HID Usage ID (Keyboard/Keypad Usage Page 0x07)
KEY_MAP: dict = {
    # Letters (0x04-0x1D)
    "KeyA": 0x04, "KeyB": 0x05, "KeyC": 0x06, "KeyD": 0x07,
    "KeyE": 0x08, "KeyF": 0x09, "KeyG": 0x0A, "KeyH": 0x0B,
    "KeyI": 0x0C, "KeyJ": 0x0D, "KeyK": 0x0E, "KeyL": 0x0F,
    "KeyM": 0x10, "KeyN": 0x11, "KeyO": 0x12, "KeyP": 0x13,
    "KeyQ": 0x14, "KeyR": 0x15, "KeyS": 0x16, "KeyT": 0x17,
    "KeyU": 0x18, "KeyV": 0x19, "KeyW": 0x1A, "KeyX": 0x1B,
    "KeyY": 0x1C, "KeyZ": 0x1D,
    # Digits (0x1E-0x27)
    "Digit1": 0x1E, "Digit2": 0x1F, "Digit3": 0x20, "Digit4": 0x21,
    "Digit5": 0x22, "Digit6": 0x23, "Digit7": 0x24, "Digit8": 0x25,
    "Digit9": 0x26, "Digit0": 0x27,
    # Core keys
    "Enter":        0x28,
    "Escape":       0x29,
    "Backspace":    0x2A,
    "Tab":          0x2B,
    "Space":        0x2C,
    "Minus":        0x2D,
    "Equal":        0x2E,
    "BracketLeft":  0x2F,
    "BracketRight": 0x30,
    "Backslash":    0x31,
    "Semicolon":    0x33,
    "Quote":        0x34,
    "Backquote":    0x35,
    "Comma":        0x36,
    "Period":       0x37,
    "Slash":        0x38,
    "CapsLock":     0x39,
    # Function keys (0x3A-0x45)
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C,  "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40,  "F8": 0x41,
    "F9": 0x42, "F10":0x43, "F11":0x44,  "F12":0x45,
    # System / Navigation
    "PrintScreen":  0x46,
    "ScrollLock":   0x47,
    "Pause":        0x48,
    "Insert":       0x49,
    "Home":         0x4A,
    "PageUp":       0x4B,
    "Delete":       0x4C,
    "End":          0x4D,
    "PageDown":     0x4E,
    "ArrowRight":   0x4F,
    "ArrowLeft":    0x50,
    "ArrowDown":    0x51,
    "ArrowUp":      0x52,
    # Numpad
    "NumLock":          0x53,
    "NumpadDivide":     0x54,
    "NumpadMultiply":   0x55,
    "NumpadSubtract":   0x56,
    "NumpadAdd":        0x57,
    "NumpadEnter":      0x58,
    "Numpad1":          0x59, "Numpad2":  0x5A, "Numpad3": 0x5B,
    "Numpad4":          0x5C, "Numpad5":  0x5D, "Numpad6": 0x5E,
    "Numpad7":          0x5F, "Numpad8":  0x60, "Numpad9": 0x61,
    "Numpad0":          0x62, "NumpadDecimal": 0x63,
    "IntlBackslash":    0x64,
    "ContextMenu":      0x65,
    # Extended function keys (F13-F24)
    "F13": 0x68, "F14": 0x69, "F15": 0x6A, "F16": 0x6B,
    "F17": 0x6C, "F18": 0x6D, "F19": 0x6E, "F20": 0x6F,
    "F21": 0x70, "F22": 0x71, "F23": 0x72, "F24": 0x73,
}

# Modifier keys
# JS event.code → bitmask in modifier byte (byte 0 of keyboard report)
MODIFIER_MAP: dict = {
    "ControlLeft":  0x01,   # bit 0 = Left Control
    "ShiftLeft":    0x02,   # bit 1 = Left Shift
    "AltLeft":      0x04,   # bit 2 = Left Alt
    "MetaLeft":     0x08,   # bit 3 = Left GUI (Windows/Command)
    "ControlRight": 0x10,   # bit 4 = Right Control
    "ShiftRight":   0x20,   # bit 5 = Right Shift
    "AltRight":     0x40,   # bit 6 = Right Alt (AltGr)
    "MetaRight":    0x80,   # bit 7 = Right GUI
}

# Character -> (JS code, shift_required) for the paste/AI-typed-text feature.
# IMPORTANT: KEY_MAP above sends USB HID keyboard USAGE codes, which the HID
# spec defines by PHYSICAL KEY POSITION (usage 0x04 is "the key where A sits
# on a US layout"), not by printed character. The target OS's own keyboard
# layout setting is what turns "physical position 0x04" into an actual
# typed character. So CHAR_MAP is really "which physical key position do I
# need to hold (and with Shift or not) to produce this character, assuming
# the target is set to THIS layout" - it's inherently layout-specific, and
# a single hardcoded US-only table will type the wrong characters (garbled
# paste/AI output) on a target set to a non-US layout, even though nothing
# is "broken" - both sides are behaving correctly per their own assumptions.
#
# CHAR_MAPS holds one table per target OS keyboard layout. Only "us" is
# built out and verified right now; adding another (uk/de/fr/etc.) means
# adding a new entry here with correct per-layout physical-position mappings
# - not a design change, just data entry - once there's an actual UK/DE/FR
# target to verify against. get_layout_names()/set_layout() below is the
# intended integration point (magicbridge.py's keyboard-settings endpoint
# calls set_layout() with a value read from config.json).
CHAR_MAPS: dict = {}

CHAR_MAPS["us"] = {
    # Lowercase letters
    "a":("KeyA",False),"b":("KeyB",False),"c":("KeyC",False),"d":("KeyD",False),
    "e":("KeyE",False),"f":("KeyF",False),"g":("KeyG",False),"h":("KeyH",False),
    "i":("KeyI",False),"j":("KeyJ",False),"k":("KeyK",False),"l":("KeyL",False),
    "m":("KeyM",False),"n":("KeyN",False),"o":("KeyO",False),"p":("KeyP",False),
    "q":("KeyQ",False),"r":("KeyR",False),"s":("KeyS",False),"t":("KeyT",False),
    "u":("KeyU",False),"v":("KeyV",False),"w":("KeyW",False),"x":("KeyX",False),
    "y":("KeyY",False),"z":("KeyZ",False),
    # Uppercase letters
    "A":("KeyA",True),"B":("KeyB",True),"C":("KeyC",True),"D":("KeyD",True),
    "E":("KeyE",True),"F":("KeyF",True),"G":("KeyG",True),"H":("KeyH",True),
    "I":("KeyI",True),"J":("KeyJ",True),"K":("KeyK",True),"L":("KeyL",True),
    "M":("KeyM",True),"N":("KeyN",True),"O":("KeyO",True),"P":("KeyP",True),
    "Q":("KeyQ",True),"R":("KeyR",True),"S":("KeyS",True),"T":("KeyT",True),
    "U":("KeyU",True),"V":("KeyV",True),"W":("KeyW",True),"X":("KeyX",True),
    "Y":("KeyY",True),"Z":("KeyZ",True),
    # Digits and shifted symbols
    "1":("Digit1",False),"2":("Digit2",False),"3":("Digit3",False),
    "4":("Digit4",False),"5":("Digit5",False),"6":("Digit6",False),
    "7":("Digit7",False),"8":("Digit8",False),"9":("Digit9",False),
    "0":("Digit0",False),
    "!":("Digit1",True),"@":("Digit2",True),"#":("Digit3",True),
    "$":("Digit4",True),"%":("Digit5",True),"^":("Digit6",True),
    "&":("Digit7",True),"*":("Digit8",True),"(":("Digit9",True),
    ")":("Digit0",True),
    # Whitespace
    " ": ("Space",    False),
    "\n":("Enter",    False),
    "\t":("Tab",      False),
    # Punctuation / symbols
    "-":("Minus",       False), "_":("Minus",       True),
    "=":("Equal",       False), "+":("Equal",        True),
    "[":("BracketLeft", False), "{":("BracketLeft",  True),
    "]":("BracketRight",False), "}":("BracketRight", True),
    "\\":("Backslash",  False), "|":("Backslash",    True),
    ";":("Semicolon",   False), ":":("Semicolon",    True),
    "'":("Quote",       False), '"':("Quote",         True),
    "`":("Backquote",   False), "~":("Backquote",    True),
    ",":("Comma",       False), "<":("Comma",         True),
    ".":("Period",      False), ">":("Period",        True),
    "/":("Slash",       False), "?":("Slash",         True),
}

_active_layout = "us"
# Back-compat alias - existing code (and this module's own send_text default)
# referenced the bare CHAR_MAP name; keep it pointing at whichever layout is
# active so nothing else needs to change if a caller reads CHAR_MAP directly.
CHAR_MAP = CHAR_MAPS["us"]


def get_layout_names() -> list:
    return sorted(CHAR_MAPS.keys())


def set_layout(name: str) -> bool:
    """Switch the active target-keyboard-layout table. Returns False (and
    leaves the previous layout in effect) for an unknown name rather than
    raising, since a bad/old config value shouldn't take typing down."""
    global _active_layout, CHAR_MAP
    if name not in CHAR_MAPS:
        log.warning("Unknown keyboard layout %r requested, keeping %r", name, _active_layout)
        return False
    _active_layout = name
    CHAR_MAP = CHAR_MAPS[name]
    return True


def get_layout() -> str:
    return _active_layout


class HIDKeyboard:
    """
    Writes 8-byte USB HID keyboard reports to /dev/hidg0.

    Report format:
        [modifier_byte, 0x00, keycode_1, keycode_2, ..., keycode_6]
    """
    REPORT_SIZE  = 8
    NULL_REPORT  = bytes(8)

    def __init__(self, device: str = "/dev/hidg0"):
        self.device    = device
        self.modifiers = 0
        self.pressed: set = set()      # HID keycodes currently held
        self._lock = threading.Lock()

    # Internal

    def _write(self, modifiers: int, keys: set):
        key_list = sorted(keys)[:6]        # USB HID max 6 simultaneous keys
        key_list += [0] * (6 - len(key_list))
        report = struct.pack("8B", modifiers, 0, *key_list)
        try:
            with open(self.device, "wb") as f:
                f.write(report)
        except OSError as e:
            log.debug("HID keyboard write: %s", e)

    # Public API

    def key_down(self, code: str):
        with self._lock:
            if code in MODIFIER_MAP:
                self.modifiers |= MODIFIER_MAP[code]
                self._write(self.modifiers, self.pressed)
            elif code in KEY_MAP:
                self.pressed.add(KEY_MAP[code])
                self._write(self.modifiers, self.pressed)

    def key_up(self, code: str):
        with self._lock:
            if code in MODIFIER_MAP:
                self.modifiers &= ~MODIFIER_MAP[code]
                self._write(self.modifiers, self.pressed)
            elif code in KEY_MAP:
                self.pressed.discard(KEY_MAP[code])
                self._write(self.modifiers, self.pressed)

    def release_all(self):
        """Send all-keys-released. Call on client disconnect or loss of focus."""
        with self._lock:
            self.modifiers = 0
            self.pressed.clear()
            self._write(0, set())

    def combo(self, codes: list):
        """
        Press all codes simultaneously (modifier + key combos),
        hold 80 ms, then release all.
        Safe to call from a thread pool executor.
        """
        mods = 0
        keys: set = set()
        for code in codes:
            if code in MODIFIER_MAP:
                mods |= MODIFIER_MAP[code]
            elif code in KEY_MAP:
                keys.add(KEY_MAP[code])
        with self._lock:
            self._write(mods, keys)
        time.sleep(0.08)
        with self._lock:
            self.modifiers = 0
            self.pressed.clear()
            self._write(0, set())

    def send_text(self, text: str, delay: float = 0.012):
        """
        Type a string character-by-character (paste feature).
        Safe to call from a thread pool executor.
        Skips characters not in CHAR_MAP (non-ASCII, etc.).

        Timing is jittered rather than a fixed interval - a perfectly
        uniform inter-keystroke delay is an obvious synthetic-input
        signature. This only affects timing between reports, never what
        gets typed. Occasional slightly longer pauses after spaces/
        punctuation mimic natural word-boundary hesitation.
        """
        for ch in text:
            entry = CHAR_MAP.get(ch)
            if not entry:
                continue
            code, need_shift = entry
            hid_code = KEY_MAP.get(code)
            if hid_code is None:
                continue
            with self._lock:
                saved_mods = self.modifiers
                mods = saved_mods | (MODIFIER_MAP["ShiftLeft"] if need_shift else 0)
                self._write(mods, {hid_code})
            hold = delay * random.uniform(0.55, 1.85)
            if ch in " .,!?\n" and random.random() < 0.15:
                hold += random.uniform(0.04, 0.14)
            time.sleep(hold)
            with self._lock:
                self._write(saved_mods, self.pressed)
            time.sleep(hold * random.uniform(0.35, 0.85))


class HIDMouse:
    """
    Writes 4-byte USB HID mouse reports to /dev/hidg1.

    Report format:
        [buttons, dx, dy, wheel]
        buttons: bit 0=left, bit 1=right, bit 2=middle (unsigned)
        dx/dy/wheel: signed bytes, -127 to +127 (relative movement)
    """
    def __init__(self, device: str = "/dev/hidg1"):
        self.device  = device
        self.buttons = 0       # current button bitmask
        self._lock   = threading.Lock()

    def _sb(self, v: int) -> int:
        """Clamp to [-127,127] and convert to unsigned byte (two's complement)."""
        v = max(-127, min(127, v))
        return v & 0xFF          # negative → 256+v (e.g. -1 → 255)

    def _write(self, buttons: int, dx: int, dy: int, wheel: int = 0):
        report = bytes([buttons & 0x07,
                        self._sb(dx),
                        self._sb(dy),
                        self._sb(wheel)])
        try:
            with open(self.device, "wb") as f:
                f.write(report)
        except OSError as e:
            log.debug("HID mouse write: %s", e)

    def move(self, dx: int, dy: int):
        """Send relative movement, chunking large deltas into ±127 steps."""
        with self._lock:
            while dx != 0 or dy != 0:
                sx = max(-127, min(127, dx))
                sy = max(-127, min(127, dy))
                self._write(self.buttons, sx, sy)
                dx -= sx
                dy -= sy

    def button_down(self, button: int):
        """button: 0=left, 1=right, 2=middle"""
        with self._lock:
            self.buttons |= (1 << min(max(int(button),0),2))
            self._write(self.buttons, 0, 0)

    def button_up(self, button: int):
        with self._lock:
            self.buttons &= ~(1 << min(max(int(button),0),2))
            self._write(self.buttons, 0, 0)

    def scroll(self, dy: int):
        """Positive = scroll down, negative = scroll up."""
        with self._lock:
            self._write(self.buttons, 0, 0, dy)

    def release_all(self):
        """Release all buttons. Call on disconnect."""
        with self._lock:
            self.buttons = 0
            self._write(0, 0, 0)
