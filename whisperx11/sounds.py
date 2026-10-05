"""Soft synthesized chimes for start / finish / cancel, played without blocking."""

import os
import shutil
import subprocess
import wave

import numpy as np

RATE = 48000
SOUND_DIR = os.path.expanduser("~/.cache/whisperx11/sounds")


def _note(freq, dur, amp=0.25):
    t = np.arange(int(RATE * dur)) / RATE
    env = np.minimum(1.0, t / 0.008) * np.exp(-t * 7.0)  # quick attack, bell-like decay
    tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * freq * 2 * t) * np.exp(-t * 12)
    return amp * env * tone


def _chime(freqs, gap=0.075, tail=0.55):
    total = int(RATE * (gap * len(freqs) + tail))
    out = np.zeros(total)
    for i, f in enumerate(freqs):
        n = _note(f, tail)
        start = int(RATE * gap * i)
        out[start:start + len(n)] += n[:total - start]
    return out / max(1.0, np.abs(out).max() / 0.6)


CHIMES = {
    "start": lambda: _chime([880.0, 1318.5]),          # A5 -> E6, rising
    "stop": lambda: _chime([1318.5, 987.8]),           # E6 -> B5, falling
    "cancel": lambda: _chime([659.3], tail=0.3) * 0.7,  # single soft E5
}


def _write(path, samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


class Sounds:
    def __init__(self, volume=0.45):
        os.makedirs(SOUND_DIR, exist_ok=True)
        self.files = {}
        for name, make in CHIMES.items():
            path = os.path.join(SOUND_DIR, f"{name}.wav")
            if not os.path.exists(path):
                _write(path, make())
            self.files[name] = path
        self.player = None
        if shutil.which("pw-play"):
            self.player = ["pw-play", "--volume", str(volume)]
        elif shutil.which("paplay"):
            self.player = ["paplay", f"--volume={int(volume * 65536)}"]
        elif shutil.which("aplay"):
            self.player = ["aplay", "-q"]

    def play(self, name):
        if self.player and name in self.files:
            try:
                subprocess.Popen(self.player + [self.files[name]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError:
                pass
