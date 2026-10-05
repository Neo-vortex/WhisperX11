"""Whisper models: language detection, transcription and (with a separate model) translation."""

import threading
import time

import numpy as np
import torch
import whisper

from .audio import SAMPLE_RATE
from .config import TRANSLATE_MODEL, translate_available


def _resident_fp16(model):
    # keep weights on the GPU in fp16; whisper's LayerNorm computes in fp32, so it stays fp32
    model.half()
    for m in model.modules():
        if isinstance(m, torch.nn.LayerNorm):
            m.float()
    return model


class Transcriber:
    def __init__(self, model_name, cpu=False):
        self.device = "cuda" if torch.cuda.is_available() and not cpu else "cpu"
        self.fp16 = self.device == "cuda"
        print(f"loading whisper '{model_name}' on {self.device} ...", flush=True)
        self.model = self._load(model_name)
        self.model.transcribe(np.zeros(SAMPLE_RATE, np.float32), fp16=self.fp16, language="en")
        self.translator = None
        self.lock = threading.Lock()

    def _load(self, name):
        model = whisper.load_model(name, device=self.device)
        return _resident_fp16(model) if self.fp16 else model

    def load_translator(self):
        """Load the translation model (once). Returns False when it isn't downloaded."""
        with self.lock:
            if self.translator is None:
                if not translate_available():
                    return False
                t0 = time.monotonic()
                self.translator = self._load(TRANSLATE_MODEL)
                print(f"loaded '{TRANSLATE_MODEL}' for translation in {time.monotonic() - t0:.1f}s", flush=True)
        return True

    def detect_language(self, audio, languages):
        if len(languages) == 1:
            return languages[0]
        mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(audio), n_mels=self.model.dims.n_mels)
        mel = mel.to(self.model.device, torch.float16 if self.fp16 else torch.float32)
        _, probs = self.model.detect_language(mel)
        return max(languages, key=lambda l: probs.get(l, 0.0))

    def transcribe(self, audio, languages, task="transcribe", prompt=None):
        """Returns (text, language). Text is empty when Whisper only heard noise."""
        language = self.detect_language(audio, languages)
        model = self.model
        if task == "translate":
            if language == "en" or not self.load_translator():
                task = "transcribe"  # English needs no translation; no model: fall back
            else:
                model = self.translator
        result = model.transcribe(audio, fp16=self.fp16, language=language, task=task, temperature=0.0,
                                  without_timestamps=True, condition_on_previous_text=False,
                                  initial_prompt=prompt or None)
        segments = result.get("segments", [])
        if segments and all(s["no_speech_prob"] > 0.6 and s["avg_logprob"] < -1.0 for s in segments):
            return "", language  # Whisper hallucinating on noise
        return result["text"].strip(), language
