"""Key combos (``ctrl+shift+t``) → X keysyms, and keysyms → Linux evdev codes for uinput."""

from __future__ import annotations

from dataclasses import dataclass

from Xlib import XK

from linuxpilot.errors import ErrorCode, LinuxPilotError

MODIFIERS: dict[str, str] = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "option": "Alt_L",
    "super": "Super_L",
    "meta": "Super_L",
    "win": "Super_L",
    "cmd": "Super_L",
    "altgr": "ISO_Level3_Shift",
}
ALIASES: dict[str, str] = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "del": "Delete",
    "delete": "Delete",
    "backspace": "BackSpace",
    "tab": "Tab",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "pgup": "Page_Up",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pgdn": "Page_Down",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "ins": "Insert",
    "menu": "Menu",
    "print": "Print",
    "capslock": "Caps_Lock",
    "plus": "plus",
    "minus": "minus",
    "comma": "comma",
    "period": "period",
}
EVDEV: dict[str, int] = {
    "Escape": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "minus": 12,
    "equal": 13,
    "BackSpace": 14,
    "Tab": 15,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "bracketleft": 26,
    "bracketright": 27,
    "Return": 28,
    "Control_L": 29,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "semicolon": 39,
    "apostrophe": 40,
    "grave": 41,
    "Shift_L": 42,
    "backslash": 43,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    "comma": 51,
    "period": 52,
    "slash": 53,
    "Alt_L": 56,
    "space": 57,
    "Caps_Lock": 58,
    "F1": 59,
    "F2": 60,
    "F3": 61,
    "F4": 62,
    "F5": 63,
    "F6": 64,
    "F7": 65,
    "F8": 66,
    "F9": 67,
    "F10": 68,
    "F11": 87,
    "F12": 88,
    "Home": 102,
    "Up": 103,
    "Page_Up": 104,
    "Left": 105,
    "Right": 106,
    "End": 107,
    "Down": 108,
    "Page_Down": 109,
    "Insert": 110,
    "Delete": 111,
    "Super_L": 125,
    "Menu": 127,
    "Print": 99,
    "ISO_Level3_Shift": 100,
}
"""Linux input-event codes (``linux/input-event-codes.h``) for the keys a uinput backend can press."""


_KEYSYM_NAMES: dict[int, str] = {}
for _attr in dir(XK):
    if _attr.startswith("XK_"):
        _KEYSYM_NAMES.setdefault(getattr(XK, _attr), _attr[3:])
"""Keysym value → its canonical X name (``0x2b`` → ``plus``)."""


@dataclass(frozen=True, slots=True)
class KeyCombo:
    modifiers: tuple[str, ...]
    """Modifier keysym names in press order."""
    key: str | None
    """The main keysym name (``None`` for a modifier alone, e.g. ``super``)."""

    @property
    def keysyms(self) -> tuple[str, ...]:
        return (*self.modifiers, self.key) if self.key else self.modifiers


def keysym_name(token: str) -> str:
    """A key token → its X keysym name (``f5`` → ``F5``, ``a`` → ``a``, ``enter`` → ``Return``)."""
    low = token.casefold()
    if low in ALIASES:
        return ALIASES[low]
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return f"F{int(low[1:])}"
    if len(token) == 1:
        return _KEYSYM_NAMES.get(char_keysym(token), token)
    if XK.string_to_keysym(token):
        return token
    match = next((n for n in dir(XK) if n.startswith("XK_") and n[3:].casefold() == low), None)
    if match:
        return match[3:]
    raise LinuxPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown key {token!r}.",
        hint="Use names like Return, Escape, Tab, Delete, F5, Page_Down, or a single character.",
    )


def parse_combo(combo: str) -> KeyCombo:
    """``"ctrl+shift+t"`` → modifiers and key; ``"+"`` and ``"ctrl++"`` mean the plus key."""
    text = combo.strip()
    if not text:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "Empty key combo.")
    tokens = ["+"] if text == "+" else [t for t in text.replace("++", "+plus").split("+") if t] or [text]
    modifiers: list[str] = []
    key: str | None = None
    for i, token in enumerate(tokens):
        low = token.strip().casefold()
        if low in MODIFIERS and (i < len(tokens) - 1 or len(tokens) == 1):
            name = MODIFIERS[low]
            if name not in modifiers:
                modifiers.append(name)
            continue
        if key is not None:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{combo!r} has more than one main key.",
                hint='Send several combos as a list: ["ctrl+a", "Delete"].',
            )
        key = keysym_name(token.strip())
    return KeyCombo(tuple(modifiers), key)


def char_keysym(char: str) -> int:
    """The X keysym of one character (Latin-1 keysyms equal the code point; others use 0x01000000+cp)."""
    cp = ord(char)
    if char == "\n":
        return XK.string_to_keysym("Return")
    if char == "\t":
        return XK.string_to_keysym("Tab")
    return cp if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF else 0x01000000 + cp


def keysym_value(name: str) -> int:
    sym = XK.string_to_keysym(name)
    if not sym:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown keysym {name!r}.")
    return sym


def evdev_code(name: str) -> int:
    """evdev code of a keysym name (letters case-insensitive); raises for keys uinput cannot press."""
    code = EVDEV.get(name) or EVDEV.get(name.casefold())
    if code is None:
        raise LinuxPilotError(ErrorCode.NOT_SUPPORTED, f"The uinput backend cannot press {name!r}.")
    return code


_PLAIN = {
    " ": "space",
    "\n": "Return",
    "\t": "Tab",
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    "`": "grave",
    ",": "comma",
    ".": "period",
    "/": "slash",
}
_SHIFTED = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "minus",
    "+": "equal",
    "{": "bracketleft",
    "}": "bracketright",
    "|": "backslash",
    ":": "semicolon",
    '"': "apostrophe",
    "~": "grave",
    "<": "comma",
    ">": "period",
    "?": "slash",
}


def char_code(char: str) -> tuple[int, bool]:
    """``(evdev code, needs Shift)`` for a character on a US layout; keysym names (``plus``) are accepted too."""
    if len(char) > 1:
        sym = XK.string_to_keysym(char)
        if not sym or sym > 0xFF:
            raise LinuxPilotError(ErrorCode.NOT_SUPPORTED, f"The virtual keyboard cannot press {char!r}.")
        char = chr(sym)
    if char in _PLAIN:
        return EVDEV[_PLAIN[char]], False
    if char in _SHIFTED:
        return EVDEV[_SHIFTED[char]], True
    if char.isascii() and char.isalnum():
        return EVDEV[char.lower()], char.isupper()
    raise LinuxPilotError(
        ErrorCode.NOT_SUPPORTED,
        f"The virtual keyboard types US-layout ASCII only, not {char!r}.",
        hint="Type it with linux_type into a field: that uses the accessibility API.",
    )
