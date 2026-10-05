#!/usr/bin/env python3
"""Push-to-dictate with OpenAI Whisper on X11.

Press the hotkey: a glowing overlay appears at the mouse pointer and starts listening.
While you talk, every short pause ends a segment that is transcribed and typed
right away; the part still being spoken is previewed under the overlay. Press the
hotkey again (or stop talking) to finish. Text is typed into the focused window via XTest, with spare keycodes temporarily remapped to the needed
characters, so it works for Persian and any keyboard layout without touching the
clipboard (--insert paste uses clipboard + Ctrl+V instead). Esc cancels.
"""

import argparse
import fcntl
import math
import os
import queue
import signal
import sys
import threading
import time
from collections import deque

import numpy as np
from scipy.signal import resample_poly
import sounddevice as sd
import torch
import whisper
from pynput import keyboard
from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor, QFont, QGuiApplication, QLinearGradient,
                           QPainter, QPainterPath, QPen, QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget
from Xlib import X, XK, display
from Xlib.ext import xtest

SAMPLE_RATE = 16000
BLOCKS_PER_SEC = 10  # audio is handled in 100 ms blocks
TERMINALS = {"xfce4-terminal", "gnome-terminal", "gnome-terminal-server", "konsole", "kitty", "alacritty",
             "xterm", "uxterm", "urxvt", "terminator", "tilix", "wezterm", "foot", "st", "lxterminal",
             "mate-terminal", "qterminal", "terminology", "guake", "tilda", "ghostty", "warp"}
MODIFIERS = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.alt, keyboard.Key.alt_l,
             keyboard.Key.alt_r, keyboard.Key.alt_gr, keyboard.Key.shift, keyboard.Key.shift_l,
             keyboard.Key.shift_r, keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                fromfile_prefix_chars="@")
    p.add_argument("--model", default="turbo",
                   help="multilingual whisper model: tiny, base, small, medium, turbo, large")
    p.add_argument("--cpu", action="store_true", help="run on the CPU even if a GPU is available")
    p.add_argument("--insert", choices=["type", "paste"], default="type",
                   help="type: simulated keystrokes, clipboard untouched; "
                        "paste: clipboard + Ctrl+V (instant for long text)")
    p.add_argument("--languages", default="en,fa",
                   help="comma-separated languages to choose between per utterance (one = fixed)")
    p.add_argument("--hotkey", default="<ctrl>+<alt>+<space>", help="pynput hotkey syntax")
    p.add_argument("--device", default=None,
                   help="input device name (substring) or index, see --list-devices (default: system default)")
    p.add_argument("--list-devices", action="store_true", help="print audio input devices and exit")
    p.add_argument("--silence", type=float, default=2.5,
                   help="auto-stop after this many seconds of silence once speech started (0 = off)")
    p.add_argument("--pause", type=float, default=0.6,
                   help="a pause this long (seconds) ends a segment, which is transcribed and inserted right away")
    p.add_argument("--max-segment", type=float, default=20,
                   help="cut segments longer than this (seconds) at their quietest moment")
    p.add_argument("--no-preview", action="store_true", help="don't show the live caption while speaking")
    p.add_argument("--max-seconds", type=float, default=300)
    p.add_argument("--threshold", type=float, default=0.03,
                   help="minimum RMS level counted as speech (raised automatically above room noise)")
    return p.parse_args()


class Recorder:
    """Captures mono audio in 100 ms blocks and flags each block as speech or not."""

    def __init__(self, device, threshold):
        self.device = int(device) if device and str(device).isdigit() else device
        self.threshold = threshold
        self.rate = SAMPLE_RATE
        self.stream = None
        self._reset()

    def _reset(self):
        self.chunks, self.voiced, self.block_levels, self.levels = [], [], [], []
        self.level = self.noise = 0.0
        self.last_voice = None
        self.started = time.monotonic()

    def start(self):
        self._reset()
        try:
            self._open(SAMPLE_RATE)
        except sd.PortAudioError:
            # raw ALSA devices often can't do 16 kHz: record at the native rate and resample
            self._open(int(sd.query_devices(self.device, "input")["default_samplerate"]))

    def _open(self, rate):
        self.rate = rate
        self.stream = sd.InputStream(device=self.device, samplerate=rate, channels=1, dtype="float32",
                                     blocksize=rate // BLOCKS_PER_SEC, callback=self._callback)
        self.stream.start()

    def _callback(self, indata, frames, t, status):
        chunk = indata[:, 0].copy()
        level = float(np.sqrt(np.mean(chunk ** 2)))
        voiced = False
        if self.chunks:  # the first block holds the click of the device opening
            self.level = level
            self.levels.append(level)
            self.noise = float(np.percentile(self.levels, 10)) if len(self.levels) >= 3 else level
            voiced = level > max(self.threshold, self.noise * 2.5)
            if voiced:
                self.last_voice = time.monotonic()
        self.voiced.append(voiced)
        self.block_levels.append(level if self.chunks else 0.0)
        self.chunks.append(chunk)

    def blocks(self):
        return len(self.chunks)

    def audio(self, start=0, end=None):
        """16 kHz float32 audio of blocks [start, end)."""
        chunks = self.chunks[start:end]
        if not chunks:
            return np.zeros(0, np.float32)
        x = np.concatenate(chunks)
        if self.rate != SAMPLE_RATE:
            g = math.gcd(SAMPLE_RATE, self.rate)
            x = resample_poly(x, SAMPLE_RATE // g, self.rate // g).astype(np.float32)
        return x

    def loudness(self):
        """Voice level above the noise floor, roughly 0..1."""
        return min(1.0, max(0.0, (self.level - self.noise * 1.2) / 0.12))

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None


class Overlay(QWidget):
    """Frameless, click-through, never-focused glowing pill, with a live caption below it."""

    W, H = 620, 190          # window, including room for the glow and the caption
    PX, PY = 40, 36          # pill position inside the window
    PW, PH = 300, 58         # the pill itself
    CAPTION_W = 540
    BARS = 26
    PALETTE = ["#8B5CF6", "#3B82F6", "#22D3EE", "#F472B6", "#8B5CF6"]
    LABELS = {"listening": "Listening", "thinking": "Thinking", "done": "Inserted", "empty": "No speech"}

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.X11BypassWindowManagerHint | Qt.WindowDoesNotAcceptFocus
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(self.W, self.H)
        self.mode = "listening"
        self.level = self.shown_level = 0.0
        self.history = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.opacity = self.target_opacity = 0.0
        self.flash = 0.0
        self.caption = ""
        self.t0 = time.monotonic()
        self.frame = 0
        self.font = QFont("Inter")
        self.font.setStyleHint(QFont.SansSerif)
        self.font.setPointSizeF(10.5)
        self.font.setWeight(QFont.DemiBold)
        self.font.setLetterSpacing(QFont.AbsoluteSpacing, 0.3)
        self.caption_font = QFont("Inter")
        self.caption_font.setFamilies(["Inter", "Vazirmatn", "Noto Sans Arabic", "DejaVu Sans"])
        self.caption_font.setPointSizeF(11)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(16)

    # --- state -------------------------------------------------------------
    def popup(self):
        pos = QCursor.pos()
        g = (QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()).geometry()
        mx, my = self.PX, self.PY
        px = min(max(pos.x() + 18, g.left() + 8), g.right() - self.PW - 8)
        py = min(max(pos.y() + 22, g.top() + 8), g.bottom() - self.PH - 8)
        self.move(px - mx, py - my)
        self.history = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.level = self.shown_level = self.flash = 0.0
        self.caption = ""
        self.mode = "listening"
        self.target_opacity = 1.0
        self.show()
        self.raise_()

    def set_mode(self, mode):
        self.mode = mode
        if mode in ("done", "empty"):
            self.flash = 1.0 if mode == "done" else 0.0
            QTimer.singleShot(550 if mode == "done" else 400, self.dismiss)

    def dismiss(self):
        self.target_opacity = 0.0

    # --- animation ---------------------------------------------------------
    def _animate(self):
        if not self.isVisible():
            return
        self.frame += 1
        self.opacity += (self.target_opacity - self.opacity) * (0.22 if self.target_opacity else 0.16)
        if self.target_opacity == 0 and self.opacity < 0.02:
            self.opacity = 0.0
            self.hide()
            return
        k = 0.45 if self.level > self.shown_level else 0.12  # fast attack, slow release
        self.shown_level += (self.level - self.shown_level) * k
        self.flash *= 0.93
        if self.frame % 3 == 0 and self.mode == "listening":
            self.history.append(self.shown_level)
        self.update()

    def _gradient(self, center, angle):
        g = QConicalGradient(center, angle)
        for i, c in enumerate(self.PALETTE):
            g.setColorAt(i / (len(self.PALETTE) - 1), QColor(c))
        return g

    def paintEvent(self, _):
        t = time.monotonic() - self.t0
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.PX, self.PY, self.PW, self.PH)
        s = 0.92 + 0.08 * self.opacity
        p.translate(rect.center())
        p.scale(s, s)
        p.translate(-rect.center())
        r = self.PH / 2
        thinking = self.mode == "thinking"
        spin = (t * (220 if thinking else 70)) % 360
        ring = self._gradient(rect.center(), -spin)

        # outer glow: stacked soft strokes of the rotating gradient
        if thinking:
            glow = 0.55 + 0.25 * math.sin(t * 5)
        else:
            glow = 0.35 + 0.75 * self.shown_level
        glow = min(1.4, glow + self.flash * 0.9)
        p.setBrush(Qt.NoBrush)
        for i in range(14, 0, -1):
            p.setOpacity(self.opacity * min(1.0, glow * 0.085 * (1.15 - i / 14)))
            p.setPen(QPen(QBrush(ring), i * 2.8))
            p.drawRoundedRect(rect, r, r)

        # glass body
        p.setOpacity(self.opacity)
        body = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        body.setColorAt(0, QColor(28, 26, 44, 238))
        body.setColorAt(1, QColor(10, 10, 18, 245))
        p.setPen(Qt.NoPen)
        p.setBrush(body)
        p.drawRoundedRect(rect, r, r)
        sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
        sheen.setColorAt(0, QColor(255, 255, 255, 22))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(sheen)
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), r - 1, r - 1)

        # crisp gradient border
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(ring), 1.6))
        p.drawRoundedRect(rect.adjusted(0.8, 0.8, -0.8, -0.8), r - 0.8, r - 0.8)

        self._paint_orb(p, QPointF(rect.left() + 29, rect.center().y()), t, thinking)
        self._paint_bars(p, QRectF(rect.left() + 54, rect.top() + 11, 150, rect.height() - 22), t, thinking)
        self._paint_label(p, QRectF(rect.left() + 214, rect.top(), 78, rect.height()), t)
        if self.caption:
            self._paint_caption(p, rect, ring)
        p.end()

    def _paint_caption(self, p, pill, ring):
        p.setFont(self.caption_font)
        fm = p.fontMetrics()
        text = fm.elidedText(self.caption, Qt.ElideLeft, self.CAPTION_W - 32)
        box = QRectF(pill.left(), pill.bottom() + 12, fm.horizontalAdvance(text) + 32, fm.height() + 18)
        p.setOpacity(self.opacity * 0.95)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(14, 14, 24, 228))
        p.drawRoundedRect(box, 12, 12)
        p.setOpacity(self.opacity * 0.55)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QBrush(ring), 1.1))
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        p.setOpacity(self.opacity)
        p.setPen(QColor(232, 232, 245))
        # base direction from the first strong character, so Persian punctuation lands on the right side
        strong = next((c for c in self.caption if c.isalpha()), "a")
        p.setLayoutDirection(Qt.RightToLeft if "\u0590" <= strong <= "\u08ff" else Qt.LeftToRight)
        p.drawText(box.adjusted(16, 0, -16, 0), Qt.AlignVCenter | Qt.AlignAbsolute | Qt.AlignLeft, text)
        p.setLayoutDirection(Qt.LeftToRight)

    def _paint_orb(self, p, c, t, thinking):
        base = 8.5 + 3.5 * self.shown_level + 1.0 * math.sin(t * 3)
        halo = QRadialGradient(c, base * 2.6)
        halo.setColorAt(0, QColor(167, 139, 250, 150))
        halo.setColorAt(0.5, QColor(59, 130, 246, 60))
        halo.setColorAt(1, QColor(59, 130, 246, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, base * 2.6, base * 2.6)
        core = QRadialGradient(QPointF(c.x() - base * 0.3, c.y() - base * 0.35), base * 1.2)
        core.setColorAt(0, QColor("#FFFFFF"))
        core.setColorAt(0.35, QColor("#C4B5FD"))
        core.setColorAt(0.8, QColor("#7C3AED"))
        core.setColorAt(1, QColor("#3B0764"))
        p.setBrush(core)
        p.drawEllipse(c, base, base)
        if thinking:  # orbiting arc
            arc = QPen(QBrush(self._gradient(c, -t * 400)), 2.2)
            arc.setCapStyle(Qt.RoundCap)
            p.setPen(arc)
            p.setBrush(Qt.NoBrush)
            rr = base + 5
            p.drawArc(QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr), int((-t * 400) % 360 * 16), 200 * 16)

    def _paint_bars(self, p, area, t, thinking):
        n, gap = self.BARS, 2.6
        w = (area.width() - gap * (n - 1)) / n
        grad = QLinearGradient(area.topLeft(), area.topRight())
        shift = (t * 0.25) % 1.0
        for i, col in enumerate(self.PALETTE):
            grad.setColorAt((i / (len(self.PALETTE) - 1) + shift) % 1.0, QColor(col))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        mid = area.center().y()
        for i in range(n):
            if thinking:
                h = 3 + 14 * (0.5 + 0.5 * math.sin(t * 7 - i * 0.42)) ** 2
            else:
                center_weight = 0.55 + 0.45 * math.sin(math.pi * (i + 0.5) / n)
                h = 3 + self.history[i] * area.height() * center_weight + 1.2 * (1 + math.sin(t * 4 + i * 0.7))
            h = min(h, area.height())
            x = area.left() + i * (w + gap)
            p.drawRoundedRect(QRectF(x, mid - h / 2, w, h), w / 2, w / 2)

    def _paint_label(self, p, area, t):
        text = self.LABELS.get(self.mode, "")
        x0 = area.left() + ((t * 0.9) % 1.6 - 0.3) * area.width()
        shimmer = QLinearGradient(QPointF(x0 - 30, 0), QPointF(x0 + 30, 0))
        shimmer.setColorAt(0, QColor(170, 170, 200))
        shimmer.setColorAt(0.5, QColor(255, 255, 255))
        shimmer.setColorAt(1, QColor(170, 170, 200))
        shimmer.setSpread(QLinearGradient.PadSpread)
        p.setFont(self.font)
        p.setPen(QPen(QBrush(shimmer), 1))
        p.drawText(area, Qt.AlignVCenter | Qt.AlignLeft, text)


class Paster:
    """Sends Ctrl+V (or Ctrl+Shift+V in terminals) by keycode via XTest, so it works in any layout."""

    def __init__(self):
        self.d = display.Display()
        self.code = {k: self.d.keysym_to_keycode(XK.string_to_keysym(k)) for k in ("Control_L", "Shift_L", "v")}

    def _focused_class(self):
        try:
            atom = self.d.intern_atom("_NET_ACTIVE_WINDOW")
            wid = self.d.screen().root.get_full_property(atom, X.AnyPropertyType).value[0]
            cls = self.d.create_resource_object("window", wid).get_wm_class() or ()
            return {c.lower() for c in cls}
        except Exception:
            return set()

    def paste(self):
        keys = ["Control_L", "Shift_L", "v"] if self._focused_class() & TERMINALS else ["Control_L", "v"]
        for k in keys:
            xtest.fake_input(self.d, X.KeyPress, self.code[k])
        for k in reversed(keys):
            xtest.fake_input(self.d, X.KeyRelease, self.code[k])
        self.d.sync()


class Typer:
    """Types any Unicode text via XTest without the clipboard.

    Simulating real keys breaks with multiple layouts (us,ir): the same keycode means different
    characters per group. Instead, spare (unmapped) keycodes are temporarily bound to the needed
    keysyms in every group and level, pressed, then unbound again.
    """

    KEY_DELAY = 0.003  # between keystrokes
    SETTLE = 0.05      # let clients pick up a keymap change before/after using it

    def __init__(self):
        self.d = display.Display()

    @staticmethod
    def _keysym(ch):
        c = ord(ch)
        return c if 0x20 <= c <= 0x7E or 0xA0 <= c <= 0xFF else 0x01000000 + c

    def type(self, text):
        lo, hi = self.d.display.info.min_keycode, self.d.display.info.max_keycode
        keymap = self.d.get_keyboard_mapping(lo, hi - lo + 1)
        per = len(keymap[0])
        spare = [lo + i for i, syms in enumerate(keymap) if not any(syms)]
        if not spare:
            raise RuntimeError("no spare keycodes to type with; use --insert paste")
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
                    xtest.fake_input(self.d, X.KeyPress, batch[ch])
                    xtest.fake_input(self.d, X.KeyRelease, batch[ch])
                    self.d.sync()
                    time.sleep(self.KEY_DELAY)
                time.sleep(self.SETTLE)
                i = j
        finally:
            for kc in used:
                self.d.change_keyboard_mapping(kc, [[0] * per])
            self.d.sync()


class App:
    """Streams while you talk: every pause ends a segment, which is transcribed and inserted
    right away while recording continues. The segment still being spoken is previewed live."""

    def __init__(self, args):
        self.args = args
        self.device = "cuda" if torch.cuda.is_available() and not args.cpu else "cpu"
        print(f"loading whisper '{args.model}' on {self.device} ...", flush=True)
        self.model = whisper.load_model(args.model, device=self.device)
        self.fp16 = self.device == "cuda"
        if self.fp16:
            # keep weights resident in fp16 (whisper's LayerNorm computes in fp32, so it stays fp32)
            self.model.half()
            for m in self.model.modules():
                if isinstance(m, torch.nn.LayerNorm):
                    m.float()
        self.languages = [l.strip() for l in args.languages.split(",") if l.strip()]
        self.model.transcribe(np.zeros(SAMPLE_RATE, np.float32), fp16=self.fp16, language=self.languages[0])
        print(f"ready — press {args.hotkey} to dictate, Esc to cancel", flush=True)

        self.recorder = Recorder(args.device, args.threshold)
        self.paster = Paster()
        self.typer = Typer()
        self.state = "idle"       # idle | recording | finishing
        self.session = 0          # bumps on every start/cancel; stale results are dropped
        self.seg_start = 0        # first block of the segment being spoken
        self.pending = 0          # segments queued or being transcribed
        self.produced = False
        self.prompt, self.prompt_lang = "", None
        self.last_partial = 0.0
        self.events = queue.Queue()   # -> Qt main thread
        self.jobs = queue.Queue()     # -> transcription worker
        self.inserts = deque()        # finished texts waiting to be typed/pasted, in order
        self.inserting = False
        self.held = set()

        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.overlay = Overlay()

        self.hotkey = keyboard.HotKey(keyboard.HotKey.parse(args.hotkey), lambda: self.events.put("toggle"))
        self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.start()
        threading.Thread(target=self._worker, daemon=True).start()
        self.tick = QTimer()
        self.tick.timeout.connect(self._tick)
        self.tick.start(20)

    # keyboard listener thread -> event queue
    def _on_press(self, key):
        self.held.add(key)
        self.hotkey.press(self.listener.canonical(key))
        if key == keyboard.Key.esc and self.state == "recording":
            self.events.put("cancel")

    def _on_release(self, key):
        self.held.discard(key)
        self.hotkey.release(self.listener.canonical(key))

    # Qt main loop
    def _tick(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.state == "recording":
            r, now = self.recorder, time.monotonic()
            self.overlay.level = r.loudness()
            self._segment()
            silent = self.args.silence and r.last_voice and now - r.last_voice > self.args.silence
            if silent or now - r.started > self.args.max_seconds:
                self._stop()
        self._pump_inserts()
        if self.state == "finishing" and not self.pending and not self.inserts and not self.inserting:
            self.state = "idle"
            self.overlay.set_mode("done" if self.produced else "empty")

    def _handle(self, event):
        if event == "toggle" and self.state == "idle":
            try:
                self.recorder.start()
            except Exception as e:
                print(f"audio error: {e}", flush=True)
                return
            self.session += 1
            self.seg_start, self.pending, self.produced = 0, 0, False
            self.prompt, self.prompt_lang = "", None
            self.state = "recording"
            self.overlay.popup()
        elif event == "toggle" and self.state == "recording":
            self._stop()
        elif event == "cancel" and self.state == "recording":
            self.recorder.stop()
            self.session += 1
            self.pending = 0
            self.state = "idle"
            self.overlay.dismiss()
        elif event == "inserted":
            self.inserting = False
        elif event[0] == "segment" and event[1] == self.session:
            self.pending -= 1
            if event[2]:
                self.produced = True
                self.inserts.append(event[2])
                self.overlay.caption = event[2]
                self.overlay.flash = 0.6
        elif event[0] == "partial" and event[1] == self.session and event[2] == self.seg_start:
            if self.state == "recording" and event[3]:
                self.overlay.caption = event[3]

    def _stop(self):
        self.recorder.stop()
        self._cut(self.recorder.blocks())
        self.state = "finishing"
        self.overlay.level = 0.0
        self.overlay.set_mode("thinking")

    # --- segmentation (main thread) --------------------------------------------
    def _segment(self):
        r, n = self.recorder, self.recorder.blocks()
        voiced = r.voiced[self.seg_start:n]
        if not any(voiced):
            if n - self.seg_start > 3 * BLOCKS_PER_SEC:  # long silence: drop it, keep a short pre-roll
                self.seg_start = n - 3
            return
        trailing = len(voiced) - 1 - max(i for i, v in enumerate(voiced) if v)
        if trailing >= self.args.pause * BLOCKS_PER_SEC:
            self._cut(n - trailing + 3)  # keep 300 ms of the pause
        elif n - self.seg_start >= self.args.max_segment * BLOCKS_PER_SEC:
            lo = n - 5 * BLOCKS_PER_SEC  # cut at the quietest moment of the last 5 s
            self._cut(lo + int(np.argmin(r.block_levels[lo:n])) + 1)

    def _cut(self, end):
        start, self.seg_start = self.seg_start, end
        if sum(self.recorder.voiced[start:end]) < 3:  # under 0.3 s of speech: a click or breath
            return
        self.pending += 1
        self.jobs.put((self.session, self.recorder.audio(start, end)))

    # --- transcription worker thread --------------------------------------------
    def _worker(self):
        while True:
            try:
                session, audio = self.jobs.get(timeout=0.1)
            except queue.Empty:
                self._preview()
                continue
            text = ""
            try:
                text = self._transcribe(audio, final=True)
            except Exception as e:
                print(f"transcribe error: {e}", flush=True)
            self.events.put(("segment", session, text))

    def _preview(self):
        if self.state != "recording" or self.args.no_preview:
            return
        session, start, n = self.session, self.seg_start, self.recorder.blocks()
        if n - start < BLOCKS_PER_SEC or time.monotonic() - self.last_partial < 0.6:
            return
        if sum(self.recorder.voiced[start:n]) < 3:
            return
        self.last_partial = time.monotonic()
        try:
            text = self._transcribe(self.recorder.audio(start, n), final=False)
        except Exception as e:
            print(f"preview error: {e}", flush=True)
            return
        self.events.put(("partial", session, start, text))

    def _transcribe(self, audio, final):
        t0 = time.monotonic()
        language = self._detect_language(audio)
        prompt = self.prompt if self.prompt and language == self.prompt_lang else None
        result = self.model.transcribe(audio, fp16=self.fp16, language=language, temperature=0.0,
                                       without_timestamps=True, condition_on_previous_text=False,
                                       initial_prompt=prompt)
        segments = result.get("segments", [])
        if segments and all(s["no_speech_prob"] > 0.6 and s["avg_logprob"] < -1.0 for s in segments):
            return ""  # whisper hallucinating on noise
        text = result["text"].strip()
        if final:
            print(f"[{time.monotonic() - t0:.2f}s {language} {len(audio) / SAMPLE_RATE:.1f}s] {text}", flush=True)
            if text:
                self.prompt, self.prompt_lang = text[-200:], language
        return text

    def _detect_language(self, audio):
        if len(self.languages) == 1:
            return self.languages[0]
        mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(audio), n_mels=self.model.dims.n_mels)
        mel = mel.to(self.model.device, torch.float16 if self.fp16 else torch.float32)
        _, probs = self.model.detect_language(mel)
        return max(self.languages, key=lambda l: probs.get(l, 0.0))

    # --- inserting (main thread, one text at a time, in order) ------------------
    def _pump_inserts(self):
        # wait while modifiers are held (e.g. the hotkey), or they'd combine with our keystrokes
        if self.inserting or not self.inserts or self.held & MODIFIERS:
            return
        text = self.inserts.popleft() + " "
        self.inserting = True
        if self.args.insert == "type":
            threading.Thread(target=self._type, args=(text,), daemon=True).start()
        else:
            self._paste(text)

    def _type(self, text):
        try:
            self.typer.type(text)
        except Exception as e:
            print(f"type error: {e}", flush=True)
        self.events.put("inserted")

    def _paste(self, text):
        clipboard = self.qapp.clipboard()
        previous = QMimeData()
        current = clipboard.mimeData()
        for fmt in (current.formats() if current else []):
            previous.setData(fmt, current.data(fmt))
        clipboard.setText(text)
        QTimer.singleShot(40, self.paster.paste)
        QTimer.singleShot(1000, lambda: self._restore_clipboard(previous))

    def _restore_clipboard(self, previous):
        if previous.formats():
            self.qapp.clipboard().setMimeData(previous)
        self.inserting = False

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.qapp.exec()


def single_instance():
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "whisper-dictate.lock")
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("whisper-dictate is already running")
    return lock


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"]:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"{i:3}  {d['name']}  [{api}, {int(d['default_samplerate'])} Hz]")


if __name__ == "__main__":
    args = parse_args()
    if args.list_devices:
        list_devices()
        raise SystemExit
    _lock = single_instance()
    App(args).run()
