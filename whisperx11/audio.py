"""Microphone capture in 100 ms blocks, with per-block speech detection (Silero VAD or energy)."""

import math
import queue
import threading
import time

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

SAMPLE_RATE = 16000
BLOCKS_PER_SEC = 10


def resample(x, rate):
    if rate == SAMPLE_RATE:
        return x
    g = math.gcd(SAMPLE_RATE, rate)
    return resample_poly(x, SAMPLE_RATE // g, rate // g).astype(np.float32)


class SileroVAD:
    """Neural voice activity detection: ignores keyboard clicks, fans and other non-speech noise."""

    WINDOW = 512  # samples at 16 kHz, what the model expects

    def __init__(self):
        import torch
        from silero_vad import load_silero_vad
        self.torch = torch
        self.model = load_silero_vad()
        self.buf = np.zeros(0, np.float32)
        self.last = 0.0

    def reset(self):
        self.model.reset_states()
        self.buf = np.zeros(0, np.float32)
        self.last = 0.0

    def prob(self, x16):
        """Highest speech probability among the windows completed by this block."""
        self.buf = np.concatenate([self.buf, x16])
        probs = []
        with self.torch.no_grad():
            while len(self.buf) >= self.WINDOW:
                window = self.torch.from_numpy(self.buf[:self.WINDOW].copy())
                probs.append(self.model(window, SAMPLE_RATE).item())
                self.buf = self.buf[self.WINDOW:]
        if probs:
            self.last = max(probs)
        return self.last


class Recorder:
    """Captures mono audio in 100 ms blocks and flags each block as speech or not.

    The audio callback only stores blocks; speech detection runs on its own thread, so `voiced`
    can lag `chunks` by a block or two. Use `ready()` for the number of classified blocks.
    """

    def __init__(self, device=None, threshold=0.03, vad="silero"):
        self.device = int(device) if device and str(device).isdigit() else device
        self.threshold = threshold
        self.vad = None
        if vad == "silero":
            try:
                self.vad = SileroVAD()
            except Exception as e:  # missing package: fall back to the energy detector
                print(f"silero-vad unavailable ({e}); using energy-based speech detection", flush=True)
        self.rate = SAMPLE_RATE
        self.stream = None
        self.thread = None
        self._reset()

    def _reset(self):
        self.chunks, self.voiced, self.block_levels, self.levels = [], [], [], []
        self.level = self.noise = 0.0
        self.last_voice = None
        self.started = time.monotonic()
        self.pending = queue.Queue()
        self.running = False

    def start(self):
        self._reset()
        if self.vad:
            self.vad.reset()
        self.running = True
        self.thread = threading.Thread(target=self._classify, daemon=True)
        self.thread.start()
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
        if self.chunks:  # the first block holds the click of the device opening
            self.level = level
            self.levels.append(level)
            self.noise = float(np.percentile(self.levels[-300:], 10)) if len(self.levels) >= 3 else level
        self.chunks.append(chunk)
        self.pending.put((chunk, level if len(self.chunks) > 1 else 0.0))

    def _classify(self):
        speaking = False
        while self.running or not self.pending.empty():
            try:
                chunk, level = self.pending.get(timeout=0.05)
            except queue.Empty:
                continue
            if level == 0.0:
                voiced = False
            elif self.vad:
                p = self.vad.prob(resample(chunk, self.rate))
                speaking = p > (0.35 if speaking else 0.5)  # hysteresis
                voiced = speaking
            else:
                voiced = level > max(self.threshold, self.noise * 2.5)
            self.block_levels.append(level)
            self.voiced.append(voiced)
            if voiced:
                self.last_voice = time.monotonic()

    def ready(self):
        """Number of blocks whose speech flag is known."""
        return len(self.voiced)

    def audio(self, start=0, end=None):
        """16 kHz float32 audio of blocks [start, end)."""
        chunks = self.chunks[start:end]
        if not chunks:
            return np.zeros(0, np.float32)
        return resample(np.concatenate(chunks), self.rate)

    def loudness(self):
        """Voice level above the noise floor, roughly 0..1 (for the meter)."""
        return min(1.0, max(0.0, (self.level - self.noise * 1.2) / 0.12))

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)  # finish classifying the last blocks


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"]:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"{i:3}  {d['name']}  [{api}, {int(d['default_samplerate'])} Hz]")
