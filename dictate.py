#!/usr/bin/env python3
"""Push-to-dictate with OpenAI Whisper on X11.

Press the hotkey: a glowing overlay appears at the mouse pointer and starts listening.
Press the hotkey again (or just stop talking) and the text is pasted into the
focused window (clipboard + simulated Ctrl+V, Ctrl+Shift+V in terminals, which
works for Persian and any keyboard layout). Esc cancels.
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
BLOCK = 1600  # 100 ms
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
    p.add_argument("--languages", default="en,fa",
                   help="comma-separated languages to choose between per utterance (one = fixed)")
    p.add_argument("--hotkey", default="<ctrl>+<alt>+<space>", help="pynput hotkey syntax")
    p.add_argument("--device", default=None,
                   help="input device name (substring) or index, see --list-devices (default: system default)")
    p.add_argument("--list-devices", action="store_true", help="print audio input devices and exit")
    p.add_argument("--silence", type=float, default=1.5,
                   help="auto-stop after this many seconds of silence once speech started (0 = off)")
    p.add_argument("--max-seconds", type=float, default=120)
    p.add_argument("--threshold", type=float, default=0.03,
                   help="minimum RMS level counted as speech (raised automatically above room noise)")
    return p.parse_args()


class Recorder:
    def __init__(self, device, threshold):
        self.device = int(device) if device and str(device).isdigit() else device
        self.threshold = threshold
        self.chunks, self.levels = [], []
        self.level = self.noise = 0.0
        self.last_voice = None
        self.started = None
        self.stream = None

    def start(self):
        self.chunks, self.levels, self.level, self.noise, self.last_voice = [], [], 0.0, 0.0, None
        self.started = time.monotonic()
        self.stream = sd.InputStream(device=self.device, samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32", blocksize=BLOCK, callback=self._callback)
        self.stream.start()

    def _callback(self, indata, frames, t, status):
        chunk = indata[:, 0].copy()
        self.chunks.append(chunk)
        level = float(np.sqrt(np.mean(chunk ** 2)))
        if len(self.chunks) == 1:  # skip the click when the device opens
            return
        self.level = level
        self.levels.append(level)
        self.noise = float(np.percentile(self.levels, 10)) if len(self.levels) >= 3 else level
        if level > max(self.threshold, self.noise * 2.5):
            self.last_voice = time.monotonic()

    def loudness(self):
        """Voice level above the noise floor, roughly 0..1."""
        return min(1.0, max(0.0, (self.level - self.noise * 1.2) / 0.12))

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)


class Overlay(QWidget):
    """Frameless, click-through, never-focused glowing pill."""

    W, H = 380, 130          # window, including room for the glow
    PW, PH = 300, 58         # the pill itself
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
        self.t0 = time.monotonic()
        self.frame = 0
        self.font = QFont("Inter")
        self.font.setStyleHint(QFont.SansSerif)
        self.font.setPointSizeF(10.5)
        self.font.setWeight(QFont.DemiBold)
        self.font.setLetterSpacing(QFont.AbsoluteSpacing, 0.3)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._animate)
        self.timer.start(16)

    # --- state -------------------------------------------------------------
    def popup(self):
        pos = QCursor.pos()
        g = (QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()).geometry()
        mx, my = (self.W - self.PW) // 2, (self.H - self.PH) // 2
        px = min(max(pos.x() + 18, g.left() + 8), g.right() - self.PW - 8)
        py = min(max(pos.y() + 22, g.top() + 8), g.bottom() - self.PH - 8)
        self.move(px - mx, py - my)
        self.history = deque([0.0] * self.BARS, maxlen=self.BARS)
        self.level = self.shown_level = self.flash = 0.0
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
        s = 0.92 + 0.08 * self.opacity
        p.translate(self.W / 2, self.H / 2)
        p.scale(s, s)
        p.translate(-self.W / 2, -self.H / 2)

        rect = QRectF((self.W - self.PW) / 2, (self.H - self.PH) / 2, self.PW, self.PH)
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
        p.end()

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


class App:
    def __init__(self, args):
        self.args = args
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
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
        self.state = "idle"  # idle | recording | transcribing
        self.events = queue.Queue()
        self.paster = Paster()
        self.held = set()

        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.overlay = Overlay()

        self.hotkey = keyboard.HotKey(keyboard.HotKey.parse(args.hotkey), lambda: self.events.put("toggle"))
        self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.start()
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
            silent = self.args.silence and r.last_voice and now - r.last_voice > self.args.silence
            if silent or now - r.started > self.args.max_seconds:
                self._handle("toggle")

    def _handle(self, event):
        if event == "toggle" and self.state == "idle":
            try:
                self.recorder.start()
            except Exception as e:
                print(f"audio error: {e}", flush=True)
                return
            self.state = "recording"
            self.overlay.popup()
        elif event == "toggle" and self.state == "recording":
            audio = self.recorder.stop()
            self.state = "transcribing"
            self.overlay.level = 0.0
            self.overlay.set_mode("thinking")
            threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()
        elif event == "cancel" and self.state == "recording":
            self.recorder.stop()
            self.state = "idle"
            self.overlay.dismiss()
        elif isinstance(event, tuple) and event[0] == "done":
            if event[1]:
                self.overlay.set_mode("done")
                self._paste(event[1], time.monotonic() + 3.0)
            else:
                self.overlay.set_mode("empty")
                self.state = "idle"

    def _transcribe(self, audio):
        text = ""
        try:
            if len(audio) > SAMPLE_RATE * 0.3 and self.recorder.last_voice:
                t0 = time.monotonic()
                language = self._detect_language(audio)
                result = self.model.transcribe(audio, fp16=self.fp16, language=language, temperature=0.0,
                                               without_timestamps=True, condition_on_previous_text=False)
                text = result["text"].strip()
                print(f"[{time.monotonic() - t0:.2f}s {language}] {text}", flush=True)
        except Exception as e:
            print(f"transcribe error: {e}", flush=True)
        self.events.put(("done", text))

    def _detect_language(self, audio):
        if len(self.languages) == 1:
            return self.languages[0]
        mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(audio), n_mels=self.model.dims.n_mels)
        mel = mel.to(self.model.device, torch.float16 if self.fp16 else torch.float32)
        _, probs = self.model.detect_language(mel)
        return max(self.languages, key=lambda l: probs.get(l, 0.0))

    def _paste(self, text, deadline):
        # wait until the user lets go of the hotkey modifiers, or they'd combine with our Ctrl+V
        if self.held & MODIFIERS and time.monotonic() < deadline:
            QTimer.singleShot(20, lambda: self._paste(text, deadline))
            return
        clipboard = self.qapp.clipboard()
        previous = QMimeData()
        current = clipboard.mimeData()
        for fmt in (current.formats() if current else []):
            previous.setData(fmt, current.data(fmt))
        clipboard.setText(text + " ")
        QTimer.singleShot(40, self.paster.paste)
        QTimer.singleShot(1000, lambda: self._restore_clipboard(previous))

    def _restore_clipboard(self, previous):
        if previous.formats():
            self.qapp.clipboard().setMimeData(previous)
        self.state = "idle"

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
