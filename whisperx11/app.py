"""WhisperX11 daemon: hotkeys -> recorder -> segmenter -> Whisper worker -> inserter, with the UI."""

import argparse
import fcntl
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np

from .audio import BLOCKS_PER_SEC, SAMPLE_RATE, Recorder, list_devices
from .config import Settings, ensure_vocab_file, load_vocab, translate_available
from .hotkeys import MODIFIER_GROUPS, combo_label, key_label, make_key_listener, parse_combo
from .insert import is_wayland, make_inserter
from .streaming import SegmentAgreement

MODIFIERS = set().union(*MODIFIER_GROUPS.values())
LANG_LABELS = {"en,fa": "EN / FA", "en": "English", "fa": "Persian"}


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="whisperx11", fromfile_prefix_chars="@",
                                description="Real-time Whisper voice typing for Linux. Settings changed in the "
                                            "tray / settings panel are saved; flags override them for one run.")
    p.add_argument("--model", default="turbo", help="whisper model: tiny, base, small, medium, turbo, large")
    p.add_argument("--cpu", action="store_true", help="run on the CPU even if a GPU is available")
    p.add_argument("--hotkey", default="<ctrl>+<alt>+<space>", help="start/stop hotkey in toggle mode")
    p.add_argument("--mode", choices=["toggle", "ptt"], help="toggle hotkey, or hold the push-to-talk key")
    p.add_argument("--ptt-key", help="push-to-talk key name, e.g. ctrl_r, alt_r, f9")
    p.add_argument("--task", choices=["transcribe", "translate"], help="translate needs the medium model")
    p.add_argument("--languages", help="languages to choose between per segment, e.g. en,fa")
    p.add_argument("--insert", choices=["type", "paste"], help="type: keystrokes | paste: clipboard + Ctrl+V")
    p.add_argument("--no-sounds", dest="sounds", action="store_const", const=False, help="no chimes")
    p.add_argument("--no-preview", dest="preview", action="store_const", const=False, help="no live caption")
    p.add_argument("--no-early-commit", dest="early_commit", action="store_const", const=False,
                   help="only type a segment once it's finished")
    p.add_argument("--vocab", help="vocabulary file (default ~/.config/whisperx11/vocab.txt)")
    p.add_argument("--vad", choices=["silero", "energy"], default="silero", help="speech detector")
    p.add_argument("--device", default=None, help="input device name (substring) or index, see --list-devices")
    p.add_argument("--list-devices", action="store_true", help="print audio input devices and exit")
    p.add_argument("--pause", type=float, default=0.6, help="pause (s) that ends a segment and inserts it")
    p.add_argument("--max-segment", type=float, default=20, help="cut longer segments at their quietest moment")
    p.add_argument("--silence", type=float, default=2.5, help="silence (s) that ends a toggle dictation (0 = off)")
    p.add_argument("--max-seconds", type=float, default=300)
    p.add_argument("--threshold", type=float, default=0.03, help="energy detector: minimum speech RMS")
    return p.parse_args(argv)


class App:
    def __init__(self, args):
        from .engine import Transcriber
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        self.args = args
        self.settings = Settings({"mode": args.mode, "ptt_key": args.ptt_key, "task": args.task,
                                  "languages": args.languages, "insert": args.insert, "sounds": args.sounds,
                                  "preview": args.preview, "early_commit": args.early_commit})
        if self.settings["task"] == "translate" and not translate_available():
            self.settings.overrides["task"] = "transcribe"
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setApplicationName("WhisperX11")

        self.engine = Transcriber(args.model, cpu=args.cpu)
        self.recorder = Recorder(args.device, args.threshold, vad=args.vad)
        self.inserter = make_inserter()
        from .sounds import Sounds
        self.sounds = Sounds()
        self.combo = parse_combo(args.hotkey)

        self.state = "idle"        # idle | recording | finishing
        self.session = 0           # bumps on every start/cancel; stale results are dropped
        self.ptt_session = False
        self.seg_start = 0
        self.pending = 0
        self.produced = False
        self.prompt, self.prompt_lang, self.vocab = "", None, ""
        self.agreements = {}       # worker thread: segment start block -> SegmentAgreement
        self.last_preview = 0.0
        self.events = queue.Queue()    # -> Qt main thread
        self.jobs = queue.Queue()      # -> transcription worker
        self.inserts = deque()         # ordered insert actions: ("text", s) / ("backspace", n)
        self.inserting = False
        self.injecting_until = 0.0     # ignore our own synthetic modifier events until then
        self.held = set()
        self.combo_armed = True
        self.ptt_down_at = None
        self.ptt_other_key = False
        self.capture_cb = None

        from .overlay import Overlay
        from .panel import SettingsPanel, Tray
        self.overlay = Overlay()
        self.panel = SettingsPanel(self.settings, args.hotkey, self._capture_key, self._edit_vocab, self._status)
        self.tray = Tray(self.settings, self.panel, self.qapp.quit, self._edit_vocab)
        self.settings.listeners.append(self._on_setting)

        self.keys = make_key_listener(lambda n: self.events.put(("press", n)),
                                      lambda n: self.events.put(("release", n)))
        self.keys.start()
        threading.Thread(target=self._worker, daemon=True).start()
        self.tick = QTimer()
        self.tick.timeout.connect(self._tick)
        self.tick.start(20)
        trigger = (f"hold {key_label(self.settings['ptt_key'])}" if self.settings["mode"] == "ptt"
                   else f"press {combo_label(args.hotkey)}")
        print(f"ready — {trigger} to dictate, Esc to cancel (mode: {self.settings['mode']}, "
              f"task: {self.settings['task']})", flush=True)

    def _status(self):
        import torch
        dev = torch.cuda.get_device_name(0) if self.engine.device == "cuda" else "CPU"
        return f"{self.args.model} on {dev}"

    def _edit_vocab(self):
        path = ensure_vocab_file()
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _capture_key(self, callback):
        self.capture_cb = callback

    def _on_setting(self, key, value):
        if key == "task" and value == "translate":
            threading.Thread(target=self.engine.load_translator, daemon=True).start()
        self.overlay.subtitle = self._subtitle()

    def _subtitle(self):
        s = self.settings
        mode = "Push to talk" if s["mode"] == "ptt" else "Toggle"
        task = " · → English" if s["task"] == "translate" else ""
        return f"{mode} · {LANG_LABELS.get(s['languages'], s['languages'])}{task}"

    # --- keys (main thread) ---------------------------------------------------------
    def _key(self, kind, name):
        if name in MODIFIERS and time.monotonic() < self.injecting_until:
            return  # our own clear-modifiers dance while inserting
        if kind == "press":
            if self.capture_cb:
                cb, self.capture_cb = self.capture_cb, None
                cb(name)
                return
            self.held.add(name)
            if name == "esc" and self.state == "recording":
                self._cancel()
                return
            mode = self.settings["mode"]
            if mode == "ptt":
                if name == self.settings["ptt_key"]:
                    if self.state == "idle" and self.ptt_down_at is None:
                        self.ptt_down_at, self.ptt_other_key = time.monotonic(), False
                elif self.ptt_down_at is not None and self.state == "idle":
                    self.ptt_other_key = True  # it's a shortcut like Ctrl+C, not push-to-talk
            elif self.combo_armed and all(self.held & group for group in self.combo):
                self.combo_armed = False
                self._toggle()
        else:
            self.held.discard(name)
            if not any(self.held & group for group in self.combo):
                self.combo_armed = True
            if name == self.settings["ptt_key"]:
                self.ptt_down_at = None
                if self.state == "recording" and self.ptt_session:
                    self._stop()

    def _toggle(self):
        if self.state == "idle":
            self._start(ptt=False)
        elif self.state == "recording":
            self._stop()

    # --- main loop ------------------------------------------------------------------
    def _tick(self):
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        now = time.monotonic()
        if self.ptt_down_at is not None and self.state == "idle" and not self.ptt_other_key \
                and now - self.ptt_down_at > 0.25:
            self._start(ptt=True)
        if self.state == "recording":
            r = self.recorder
            self.overlay.level = r.loudness()
            self._segment()
            silent = (not self.ptt_session and self.args.silence and r.last_voice
                      and now - r.last_voice > self.args.silence)
            if silent or now - r.started > self.args.max_seconds:
                self._stop()
        self._pump_inserts()
        if self.state == "finishing" and not self.pending and not self.inserts and not self.inserting:
            self.state = "idle"
            self.tray.set_active(False)
            self.overlay.set_mode("done" if self.produced else "empty")
            if self.settings["sounds"]:
                self.sounds.play("stop" if self.produced else "cancel")

    def _handle(self, event):
        kind = event[0]
        if kind in ("press", "release"):
            self._key(kind, event[1])
        elif kind == "inserted":
            self.inserting = False
            self.injecting_until = time.monotonic() + 0.15
            self._resync_keys()
        elif event[1] != self.session:
            return  # result of a cancelled / earlier dictation
        elif kind == "insert":
            self.inserts.extend(event[2])
        elif kind == "segment":
            self.pending -= 1
            if event[2]:
                self.produced = True
                self.overlay.set_caption(event[2])
                self.overlay.flash = 0.6
        elif kind == "partial" and event[2] == self.seg_start and self.state == "recording" and event[3]:
            self.overlay.set_caption(event[3])

    def _resync_keys(self):
        """After inserting, trust the X server about which keys are really down."""
        for name in list(self.held):
            if name in MODIFIERS and self.inserter.is_down(name) is False:
                self.held.discard(name)
        ptt = self.settings["ptt_key"]
        if self.ptt_session and self.state == "recording" and self.inserter.is_down(ptt) is False:
            self.ptt_down_at = None
            self._stop()

    def _start(self, ptt):
        try:
            self.recorder.start()
        except Exception as e:
            print(f"audio error: {e}", flush=True)
            return
        self.session += 1
        self.ptt_session = ptt
        self.ptt_down_at = None
        self.seg_start, self.pending, self.produced = 0, 0, False
        self.prompt, self.prompt_lang = "", None
        self.vocab = load_vocab(self.args.vocab)
        self.state = "recording"
        self.overlay.subtitle = self._subtitle()
        self.overlay.LABELS["listening"] = "Translating" if self.settings["task"] == "translate" else "Listening"
        self.overlay.popup(at_cursor=not is_wayland())
        self.tray.set_active(True)
        if self.settings["sounds"]:
            self.sounds.play("start")

    def _stop(self):
        self.recorder.stop()
        self._cut(self.recorder.ready())
        self.state = "finishing"
        self.overlay.level = 0.0
        self.overlay.set_mode("thinking")

    def _cancel(self):
        self.recorder.stop()
        self.session += 1
        self.pending = 0
        self.state = "idle"
        self.tray.set_active(False)
        self.overlay.dismiss()
        if self.settings["sounds"]:
            self.sounds.play("cancel")

    # --- segmentation (main thread) -------------------------------------------------
    def _segment(self):
        r, n = self.recorder, self.recorder.ready()
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
        self.jobs.put((self.session, start, self.recorder.audio(start, end)))

    # --- transcription worker thread ------------------------------------------------
    def _worker(self):
        while True:
            try:
                session, start, audio = self.jobs.get(timeout=0.1)
            except queue.Empty:
                self._preview()
                continue
            text = ""
            try:
                text = self._transcribe(audio, final=True)
            except Exception as e:
                print(f"transcribe error: {e}", flush=True)
            agreement = self.agreements.pop(start, None) or SegmentAgreement()
            self.events.put(("insert", session, agreement.final(text)))
            self.events.put(("segment", session, text))

    def _preview(self):
        if self.state != "recording" or not (self.settings["preview"] or self.settings["early_commit"]):
            return
        session, start, n = self.session, self.seg_start, self.recorder.ready()
        if n - start < BLOCKS_PER_SEC or time.monotonic() - self.last_preview < 0.6:
            return
        if sum(self.recorder.voiced[start:n]) < 3:
            return
        self.last_preview = time.monotonic()
        try:
            text = self._transcribe(self.recorder.audio(start, n), final=False)
        except Exception as e:
            print(f"preview error: {e}", flush=True)
            return
        if self.settings["early_commit"] and session == self.session:
            agreement = self.agreements.setdefault(start, SegmentAgreement())
            piece = agreement.preview(text)
            if piece:
                self.events.put(("insert", session, [("text", piece)]))
        if self.settings["preview"]:
            self.events.put(("partial", session, start, text))

    def _transcribe(self, audio, final):
        t0 = time.monotonic()
        task = self.settings["task"]
        languages = [l.strip() for l in self.settings["languages"].split(",") if l.strip()]
        parts = [self.vocab] if self.vocab else []
        if self.prompt and task == "transcribe":
            parts.append(self.prompt)
        text, language = self.engine.transcribe(audio, languages, task, ". ".join(parts) or None)
        if final:
            arrow = " → en" if task == "translate" and language != "en" else ""
            print(f"[{time.monotonic() - t0:.2f}s {language}{arrow} {len(audio) / SAMPLE_RATE:.1f}s] {text}",
                  flush=True)
            if text:
                self.prompt, self.prompt_lang = text[-200:], language
        return text

    # --- inserting (main thread, one action at a time, in order) ----------------------
    def _pump_inserts(self):
        if self.inserting or not self.inserts:
            return
        if is_wayland() and self.held & MODIFIERS:
            return  # can't clear modifiers on Wayland: wait until they're released
        action = self.inserts.popleft()
        self.inserting = True
        self.injecting_until = float("inf")
        if action[0] == "text" and self.settings["insert"] == "paste" and not is_wayland():
            self._paste(action[1])
        else:
            threading.Thread(target=self._run_insert, args=(action,), daemon=True).start()

    def _run_insert(self, action):
        try:
            if action[0] == "text":
                self.inserter.type_text(action[1])
            else:
                self.inserter.backspace(action[1])
        except Exception as e:
            print(f"insert error: {e}", flush=True)
        self.events.put(("inserted",))

    def _paste(self, text):
        from PySide6.QtCore import QMimeData, QTimer
        clipboard = self.qapp.clipboard()
        previous = QMimeData()
        current = clipboard.mimeData()
        for fmt in (current.formats() if current else []):
            previous.setData(fmt, current.data(fmt))
        clipboard.setText(text)
        QTimer.singleShot(40, self.inserter.paste_keys)

        def restore():
            if previous.formats():
                clipboard.setMimeData(previous)
            self.events.put(("inserted",))
        QTimer.singleShot(700, restore)

    def run(self):
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.qapp.exec()


def single_instance():
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "whisper-dictate.lock")
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("WhisperX11 is already running")
    return lock


def main(argv=None):
    args = parse_args(argv)
    if args.list_devices:
        list_devices()
        return
    if is_wayland():
        # the overlay and panel run through XWayland: Wayland doesn't let clients position windows
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    _lock = single_instance()
    App(args).run()
