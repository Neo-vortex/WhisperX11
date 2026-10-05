"""Global key events as plain names ("ctrl_l", "space", "a", ...), from X11 (pynput) or evdev.

On Wayland, clients can't see global key presses; the evdev backend reads /dev/input directly,
which works under any compositor but needs read access (add yourself to the `input` group).
"""

import os
import threading

MODIFIER_GROUPS = {"ctrl": {"ctrl_l", "ctrl_r"}, "alt": {"alt_l", "alt_r", "alt_gr"},
                   "shift": {"shift_l", "shift_r"}, "super": {"super_l", "super_r"}}

KEY_LABELS = {"ctrl_r": "Right Ctrl", "ctrl_l": "Left Ctrl", "alt_r": "Right Alt", "alt_l": "Left Alt",
              "alt_gr": "AltGr", "super_r": "Right Super", "super_l": "Super", "shift_r": "Right Shift",
              "caps_lock": "Caps Lock", "menu": "Menu", "space": "Space", "esc": "Esc",
              "scroll_lock": "Scroll Lock", "pause": "Pause", "insert": "Insert"}


def key_label(name):
    return KEY_LABELS.get(name, name.upper() if len(name) <= 3 else name.replace("_", " ").title())


def parse_combo(text):
    """"<ctrl>+<alt>+<space>" -> [{"ctrl_l","ctrl_r"}, {"alt_l","alt_r","alt_gr"}, {"space"}]"""
    parts = []
    for raw in text.lower().split("+"):
        name = raw.strip().strip("<>")
        name = {"control": "ctrl", "cmd": "super", "win": "super", "escape": "esc"}.get(name, name)
        parts.append(MODIFIER_GROUPS.get(name, {name}))
    return parts


def combo_label(text):
    names = [p.strip().strip("<>") for p in text.split("+")]
    return "+".join(n.capitalize() if len(n) > 1 else n.upper() for n in names)


class PynputBackend:
    def __init__(self, on_press, on_release):
        from pynput import keyboard
        self.keyboard = keyboard
        self.on_press, self.on_release = on_press, on_release
        self.listener = keyboard.Listener(on_press=lambda k: self._emit(self.on_press, k),
                                          on_release=lambda k: self._emit(self.on_release, k))

    def _emit(self, cb, key):
        name = self.name(key)
        if name:
            cb(name)

    def name(self, key):
        k = self.keyboard.Key
        if isinstance(key, k):
            n = key.name  # ctrl_l, alt_gr, space, esc, f9, cmd_r, ...
            n = {"ctrl": "ctrl_l", "alt": "alt_l", "shift": "shift_l", "cmd": "super_l",
                 "cmd_l": "super_l", "cmd_r": "super_r"}.get(n, n)
            return n
        if getattr(key, "char", None):
            return key.char.lower()
        if getattr(key, "vk", None):
            return f"vk{key.vk}"
        return None

    def start(self):
        self.listener.start()


class EvdevBackend:
    def __init__(self, on_press, on_release):
        import evdev
        self.evdev = evdev
        self.on_press, self.on_release = on_press, on_release
        self.devices = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
            if evdev.ecodes.KEY_SPACE in keys and evdev.ecodes.KEY_A in keys:
                self.devices.append(dev)
        if not self.devices:
            raise RuntimeError("no readable keyboards in /dev/input (join the 'input' group and log in again)")

    @staticmethod
    def name(code):
        n = code[4:].lower() if code.startswith("KEY_") else code.lower()
        return {"leftctrl": "ctrl_l", "rightctrl": "ctrl_r", "leftalt": "alt_l", "rightalt": "alt_r",
                "leftshift": "shift_l", "rightshift": "shift_r", "leftmeta": "super_l", "rightmeta": "super_r",
                "capslock": "caps_lock", "scrolllock": "scroll_lock", "compose": "menu"}.get(n, n)

    def _read(self, dev):
        ecodes = self.evdev.ecodes
        for ev in dev.read_loop():
            if ev.type != ecodes.EV_KEY or ev.value == 2:  # ignore autorepeat
                continue
            code = ecodes.KEY.get(ev.code)
            code = code[0] if isinstance(code, list) else code
            if code:
                (self.on_press if ev.value == 1 else self.on_release)(self.name(code))

    def start(self):
        for dev in self.devices:
            threading.Thread(target=self._read, args=(dev,), daemon=True).start()


def make_key_listener(on_press, on_release):
    wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
    if wayland:
        try:
            return EvdevBackend(on_press, on_release)
        except Exception as e:
            print(f"evdev hotkeys unavailable: {e}", flush=True)
    return PynputBackend(on_press, on_release)
