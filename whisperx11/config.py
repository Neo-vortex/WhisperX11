"""Persistent settings (~/.config/whisperx11/settings.json), vocabulary and model files."""

import json
import os

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "whisperx11")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
VOCAB_FILE = os.path.join(CONFIG_DIR, "vocab.txt")
MODEL_DIR = os.path.expanduser("~/.cache/whisper")
TRANSLATE_MODEL = "medium"  # turbo was not trained to translate, it returns empty text

DEFAULTS = {
    "mode": "toggle",       # toggle: hotkey starts/stops | ptt: hold the push-to-talk key
    "task": "transcribe",   # transcribe | translate (to English)
    "languages": "en,fa",   # languages to choose between per segment
    "insert": "type",       # type: keystrokes | paste: clipboard + Ctrl+V
    "ptt_key": "ctrl_r",
    "sounds": True,
    "preview": True,        # live caption of the segment being spoken
    "early_commit": True,   # type words as soon as two previews agree on them
}

VOCAB_TEMPLATE = """\
# Personal vocabulary for WhisperX11, one entry per line.
# Names, product terms and spellings you want Whisper to prefer. Changes apply on the next dictation.
# Keep it short: Whisper only reads about 200 tokens of context.
WhisperX11
"""


class Settings:
    """Settings file values, optionally overridden for this run by command-line flags."""

    def __init__(self, overrides=None):
        self.values = dict(DEFAULTS)
        try:
            with open(SETTINGS_FILE) as f:
                self.values.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        self.listeners = []

    def __getitem__(self, key):
        return self.overrides.get(key, self.values[key])

    def set(self, key, value):
        """Change a setting from the UI: it wins over any command-line flag and is saved."""
        self.overrides.pop(key, None)
        self.values[key] = value
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.values, f, indent=2)
        for listener in self.listeners:
            listener(key, value)


def model_path(name):
    import whisper
    return os.path.join(MODEL_DIR, os.path.basename(whisper._MODELS[name]))


def model_available(name):
    return os.path.exists(model_path(name))


def translate_available():
    return model_available(TRANSLATE_MODEL)


TRANSLATE_HINT = (f"Translation needs the '{TRANSLATE_MODEL}' model (turbo can't translate).\n"
                  f"Download it with:  ./download_model.sh {TRANSLATE_MODEL}")


def ensure_vocab_file():
    if not os.path.exists(VOCAB_FILE):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(VOCAB_FILE, "w") as f:
            f.write(VOCAB_TEMPLATE)
    return VOCAB_FILE


def load_vocab(path=None):
    """Vocabulary as a Whisper prompt fragment, e.g. "WhisperX11, Kubernetes, Tehran"."""
    try:
        with open(path or VOCAB_FILE) as f:
            words = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return ""
    return ", ".join(words)
