"""Putting text into the focused window: X11 (XTest) and Wayland (wtype / ydotool) backends."""

import os
import shutil
import subprocess
import time

TERMINALS = {"xfce4-terminal", "gnome-terminal", "gnome-terminal-server", "konsole", "kitty", "alacritty",
             "xterm", "uxterm", "urxvt", "terminator", "tilix", "wezterm", "foot", "st", "lxterminal",
             "mate-terminal", "qterminal", "terminology", "guake", "tilda", "ghostty", "warp", "ptyxis"}


def is_wayland():
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or (
        "WAYLAND_DISPLAY" in os.environ and "DISPLAY" not in os.environ)


class X11Inserter:
    """XTest keystrokes. Typing binds spare keycodes to the needed keysyms in every layout group,
    so any Unicode text comes out right whichever layout (e.g. us or ir) is active."""

    KEY_DELAY = 0.003  # between keystrokes
    SETTLE = 0.05      # let clients pick up a keymap change before/after using it

    def __init__(self):
        from Xlib import X, XK, display
        from Xlib.ext import xtest
        self.X, self.xtest = X, xtest
        self.d = display.Display()
        self.XK = XK
        self.code = {k: self.d.keysym_to_keycode(XK.string_to_keysym(k))
                     for k in ("Control_L", "Shift_L", "v", "BackSpace")}

    # --- modifiers ------------------------------------------------------------------
    # A physically held modifier (the hotkey, or a push-to-talk key like Right Ctrl) would turn our
    # keystrokes into shortcuts. Like xdotool --clearmodifiers: release them in the X server for the
    # duration of the insert, then press them again.
    def _held_modifiers(self):
        keymap = self.d.query_keymap()
        mods = {kc for group in self.d.get_modifier_mapping() for kc in group if kc}
        return [kc for kc in mods if keymap[kc // 8] & (1 << (kc % 8))]

    def _with_clear_modifiers(self, fn):
        held = self._held_modifiers()
        for kc in held:
            self.xtest.fake_input(self.d, self.X.KeyRelease, kc)
        self.d.sync()
        try:
            fn()
        finally:
            for kc in held:
                self.xtest.fake_input(self.d, self.X.KeyPress, kc)
            self.d.sync()

    KEYSYMS = {"ctrl_l": "Control_L", "ctrl_r": "Control_R", "alt_l": "Alt_L", "alt_r": "Alt_R",
               "alt_gr": "ISO_Level3_Shift", "shift_l": "Shift_L", "shift_r": "Shift_R", "super_l": "Super_L",
               "super_r": "Super_R", "caps_lock": "Caps_Lock", "scroll_lock": "Scroll_Lock", "menu": "Menu",
               "space": "space", "esc": "Escape", "pause": "Pause", "insert": "Insert"}

    def is_down(self, name):
        """Whether a key (by listener name) is currently held, per the X server."""
        sym = self.XK.string_to_keysym(self.KEYSYMS.get(name, name.upper() if name.startswith("f") and
                                                         name[1:].isdigit() else name))
        kc = self.d.keysym_to_keycode(sym) if sym else 0
        if not kc:
            return None
        keymap = self.d.query_keymap()
        return bool(keymap[kc // 8] & (1 << (kc % 8)))

    @staticmethod
    def _keysym(ch):
        c = ord(ch)
        return c if 0x20 <= c <= 0x7E or 0xA0 <= c <= 0xFF else 0x01000000 + c

    def _tap(self, keycode):
        self.xtest.fake_input(self.d, self.X.KeyPress, keycode)
        self.xtest.fake_input(self.d, self.X.KeyRelease, keycode)
        self.d.sync()
        time.sleep(self.KEY_DELAY)

    def type_text(self, text):
        self._with_clear_modifiers(lambda: self._type(text))

    def _type(self, text):
        lo, hi = self.d.display.info.min_keycode, self.d.display.info.max_keycode
        keymap = self.d.get_keyboard_mapping(lo, hi - lo + 1)
        per = len(keymap[0])
        spare = [lo + i for i, syms in enumerate(keymap) if not any(syms)]
        if not spare:
            raise RuntimeError("no spare keycodes to type with; use paste mode")
        used, i = set(), 0
        try:
            while i < len(text):
                # bind as many distinct characters as there are spare keycodes, then type them
                batch, j = {}, i
                while j < len(text) and (text[j] in batch or len(batch) < len(spare)):
                    if text[j] not in batch:
                        batch[text[j]] = spare[len(batch)]
                    j += 1
                for ch, kc in batch.items():
                    self.d.change_keyboard_mapping(kc, [[self._keysym(ch)] * per])
                    used.add(kc)
                self.d.sync()
                time.sleep(self.SETTLE)
                for ch in text[i:j]:
                    self._tap(batch[ch])
                time.sleep(self.SETTLE)
                i = j
        finally:
            for kc in used:
                self.d.change_keyboard_mapping(kc, [[0] * per])
            self.d.sync()

    def backspace(self, n):
        self._with_clear_modifiers(lambda: [self._tap(self.code["BackSpace"]) for _ in range(n)])

    def focused_is_terminal(self):
        X = self.X
        try:
            atom = self.d.intern_atom("_NET_ACTIVE_WINDOW")
            wid = self.d.screen().root.get_full_property(atom, X.AnyPropertyType).value[0]
            cls = self.d.create_resource_object("window", wid).get_wm_class() or ()
            return bool({c.lower() for c in cls} & TERMINALS)
        except Exception:
            return False

    def paste_keys(self):
        """Ctrl+V, or Ctrl+Shift+V in terminals, by keycode (layout independent)."""
        keys = ["Control_L", "Shift_L", "v"] if self.focused_is_terminal() else ["Control_L", "v"]

        def press():
            for k in keys:
                self.xtest.fake_input(self.d, self.X.KeyPress, self.code[k])
            for k in reversed(keys):
                self.xtest.fake_input(self.d, self.X.KeyRelease, self.code[k])
            self.d.sync()
        self._with_clear_modifiers(press)


class WaylandInserter:
    """wtype (virtual-keyboard protocol: wlroots compositors such as Sway, Hyprland, labwc) types
    Unicode directly. Elsewhere (GNOME, KDE) ydotool can only press keys, so typing falls back to
    clipboard + Ctrl+V."""

    def __init__(self):
        self.wtype = shutil.which("wtype")
        self.ydotool = shutil.which("ydotool")
        if not (self.wtype or self.ydotool):
            raise RuntimeError("Wayland needs wtype (wlroots compositors) or ydotool + ydotoold")

    def type_text(self, text):
        if self.wtype and subprocess.run([self.wtype, "-d", "3", "--", text]).returncode == 0:
            return
        self.paste_text(text)

    def paste_text(self, text):
        subprocess.run(["wl-copy", "--", text], check=True)
        time.sleep(0.05)
        self.paste_keys()

    def backspace(self, n):
        if self.wtype:
            subprocess.run([self.wtype] + ["-k", "BackSpace"] * n)
        elif self.ydotool:
            subprocess.run([self.ydotool, "key"] + ["14:1", "14:0"] * n)  # KEY_BACKSPACE

    def focused_is_terminal(self):
        return False  # Wayland doesn't expose the focused window to clients

    def is_down(self, name):
        return None  # unknown; the evdev listener reports physical keys directly

    def paste_keys(self):
        if self.wtype:
            subprocess.run([self.wtype, "-M", "ctrl", "v", "-m", "ctrl"])
        elif self.ydotool:
            subprocess.run([self.ydotool, "key", "29:1", "47:1", "47:0", "29:0"])  # Ctrl+V


def make_inserter():
    return WaylandInserter() if is_wayland() else X11Inserter()
