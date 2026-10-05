<div align="center">

# WhisperX11

**Real-time voice typing for Linux/X11, powered by [OpenAI Whisper](https://github.com/openai/whisper) on your GPU.**

Press a hotkey and talk. Each sentence is typed into the focused text box as soon as you pause,
in English, Persian, or any of Whisper's 99 languages.

<img src="assets/demo.gif" alt="WhisperX11 demo: a glowing overlay listens at the mouse pointer, shows a live caption, and types each Persian and English sentence into an editor at the next pause" width="760">

</div>

---

## Features

- **Streams while you talk:** every short pause (0.6 s) ends a segment, which is transcribed and typed
  straight away while you keep speaking. A **live caption** under the overlay shows the words being
  recognised for the part you're still saying.
- **Works everywhere:** browsers, editors, chat apps and terminals. Tested in GTK, Tk, xfce4-terminal,
  Chrome and Firefox.
- **No clipboard needed:** text is typed via XTest, with spare keycodes temporarily remapped to the
  exact characters needed. This works with multi-layout keyboards (e.g. `us,ir`) whichever layout
  is active, where ordinary simulated typing garbles text. A clipboard-paste mode is available too.
- **Bilingual out of the box:** every segment is auto-detected between the languages you choose
  (default `en,fa`), so you can switch language between sentences.
- **Fast:** the model is loaded once, kept on the GPU in fp16 and warmed up at startup.
  On an RX 6700 XT, `turbo` transcribes a 5 s sentence in **~0.45 s**.
- **Hands-free:** recording ends after 2.5 s of silence, or press the hotkey again. Esc cancels.
- **Adaptive voice detection:** the speech threshold follows your room's noise floor, and noise-only
  segments are dropped instead of being "transcribed" into hallucinated text.
- **Glowing overlay:** a frameless pill at the mouse pointer with a rotating gradient border, a
  voice-reactive glow, a live waveform and a "thinking" animation. It never takes focus, and clicks pass through it.
- **GPU or CPU:** NVIDIA CUDA or AMD ROCm through PyTorch, or CPU only.

<p align="center"><img src="assets/states.png" alt="Overlay states: Listening with live caption, Thinking, Inserted" width="380"></p>

## How it works

```
 Ctrl+Alt+Space ─► overlay at pointer ─► mic, 100 ms blocks, speech/silence per block
                                                   │
                       pause ≥ 0.6 s ──────────────┤  every ~0.6 s while speaking
                             ▼                     ▼
                Whisper on GPU (en|fa)      Whisper on the unfinished segment
                             ▼                     ▼
       typed into the focused window (XTest)   live caption under the overlay
```

1. A global hotkey listener (`pynput`) starts and stops recording.
2. Audio is captured with `sounddevice` in 100 ms blocks. Each block is marked as speech or silence
   against a live noise-floor estimate. If the mic can't record at 16 kHz itself, its native rate is
   resampled.
3. A pause of `--pause` seconds ends a **segment**. Segments longer than `--max-segment` are cut at
   their quietest moment. A single worker thread transcribes finished segments first. When none are
   waiting, it re-transcribes the segment you're still speaking for the live caption.
4. Each segment's language is detected (restricted to `--languages`), and the previous segment is
   passed as context to keep punctuation and style consistent.
5. Finished text is inserted in order:
   - **`type`** (default): spare keycodes are bound to the needed characters in every layout group,
     pressed through XTest, then unbound. Your clipboard is never touched.
   - **`paste`**: clipboard + `Ctrl+V` (`Ctrl+Shift+V` in terminals). The old clipboard is restored after 1 s.

## Requirements

- Linux with an **X11** session (tested on XFCE, Ubuntu 26.04). Wayland is not supported because global
  hotkeys and input injection work differently there.
- A **compositor** for the translucent glow (XFCE: *Window Manager Tweaks → Compositor*).
- Python 3.10+ and a microphone.
- **For GPU mode:** a GPU that **already works with PyTorch**. Check that
  `python3 -c "import torch; print(torch.cuda.is_available())"` prints `True`.
  WhisperX11 does not install drivers, CUDA, ROCm or GPU builds of PyTorch. Set those up first
  ([pytorch.org](https://pytorch.org/get-started/locally/)). AMD GPUs work through ROCm, which PyTorch
  exposes through the same `torch.cuda` API.

## Install

```bash
git clone https://github.com/Neo-vortex/WhisperX11.git
cd WhisperX11

./install.sh --cuda --autostart      # GPU: reuses your existing CUDA/ROCm PyTorch
# or
./install.sh --cpu --autostart       # CPU: installs CPU-only PyTorch, defaults to the small model
```

| `install.sh` option | |
|---|---|
| `--cuda` (default) | Use the GPU. Needs a working GPU PyTorch in the Python that creates the venv (`PYTHON=/path/to/python` to choose another one). Stops with an explanation if `torch.cuda.is_available()` is false. |
| `--cpu` | Run on the CPU. Installs the CPU-only PyTorch wheel if PyTorch is missing. |
| `--model NAME` | Model to download and use (default `turbo` for GPU, `small` for CPU). |
| `--autostart` | Start on login (`~/.config/autostart/whisper-x11.desktop`). |

The venv is created with `--system-site-packages`, so an existing GPU PyTorch is reused, and
`openai-whisper` is installed with `--no-deps` so pip can't replace it with a different wheel.

## Usage

```bash
./run.sh
```

| Action | Key |
|---|---|
| Start dictation | **Ctrl+Alt+Space** |
| Insert a sentence | just pause briefly (0.6 s) and keep talking |
| Finish | stop talking for 2.5 s, or **Ctrl+Alt+Space** again |
| Cancel | **Esc** (text already typed stays) |

Each segment is logged with its latency, language and length:

```
[0.44s en 4.6s] The quick brown fox jumps over the lazy dog near the river bank.
[0.45s fa 3.1s] سلام، این یک آزمایش تایپ صوتی است.
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--model` | `turbo` | `tiny`, `base`, `small`, `medium`, `turbo`, `large`. Use a multilingual model for non-English. |
| `--languages` | `en,fa` | Languages to pick between per segment. A single value forces that language. |
| `--insert` | `type` | `type`: keystrokes, clipboard untouched. `paste`: clipboard + Ctrl+V, instant for long text. |
| `--pause` | `0.6` | Pause (s) that ends a segment and inserts it. |
| `--max-segment` | `20` | Longer segments are cut at their quietest moment. |
| `--no-preview` | | Turn off the live caption, which saves GPU work. |
| `--silence` | `2.5` | Seconds of silence that end the dictation (`0` = only the hotkey stops it). |
| `--max-seconds` | `300` | Hard limit per dictation. |
| `--hotkey` | `<ctrl>+<alt>+<space>` | Any [pynput hotkey](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys). |
| `--device` | system default | Input device name (substring) or index. |
| `--list-devices` | | Print input devices and exit. |
| `--threshold` | `0.03` | Minimum RMS treated as speech (raised automatically above room noise). |
| `--cpu` | | Run on the CPU even if a GPU is available. |

**Personal defaults:** put arguments in `config.args` (one per line, git-ignored). `run.sh` picks it up:

```
--device
Rapoo Camera: USB Audio
--languages
en,fa
```

### Type vs paste

| | `type` (default) | `paste` |
|---|---|---|
| Clipboard | untouched | replaced, restored after 1 s |
| Speed | ~8 ms per character | instant |
| Editors with autocomplete / auto-indent | may react to typed text | not affected |
| Where Ctrl+V isn't paste (vim normal mode, some terminals) | works | doesn't |

We also tested `pynput` typing (garbles text on multi-layout keyboards), xdotool (works, but is an
extra dependency), AT-SPI (only some apps expose it) and IBus (switching engines reset the keyboard
layout). The keycode-remapping approach was the only one that passed every test without new dependencies.

### Model choice

| Model | Size | Speed (RX 6700 XT) | Notes |
|---|---|---|---|
| `turbo` | 1.6 GB | ~0.45 s per 5 s sentence | **Recommended.** large-v3 quality, good Persian |
| `small` | 0.5 GB | faster | OK English, weak Persian; reasonable on CPU |
| `base` / `tiny` | <150 MB | fastest | English-centric, fine on CPU |

## Autostart

`./install.sh --autostart` starts the daemon when you log in and keeps the model warm on the GPU.
It can't start before login, because it needs your X session. The log is at `~/.cache/whisper-x11.log`.

```bash
pkill -f 'dictate.py'   # stop
./run.sh &               # start manually
```

Only one instance runs at a time (lock file in `$XDG_RUNTIME_DIR`).

## Troubleshooting

<details>
<summary><b>Recording hangs / no audio</b></summary>

PortAudio's PulseAudio backend can hang on PipeWire systems. Use the ALSA device of your mic instead:
`.venv/bin/python dictate.py --list-devices`, then pass its name with `--device "My Mic: USB Audio"`.
</details>

<details>
<summary><b>AMD GPU not used (RDNA2: RX 6700/6600 series)</b></summary>

ROCm only ships kernels for `gfx1030`. `run.sh` sets `HSA_OVERRIDE_GFX_VERSION=10.3.0` automatically when it
detects gfx1031–gfx1036. Check with:
`.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"`.
</details>

<details>
<summary><b>Sentences are split too often / not often enough</b></summary>

Raise `--pause` (e.g. `0.9`) if your sentences get cut while you think, or lower it for snappier output.
</details>

<details>
<summary><b>Text appears in the wrong language</b></summary>

Restrict detection: `--languages fa` (or `en`). Very short segments (one or two words) are the hardest to detect.
</details>

<details>
<summary><b>Overlay has a black box instead of a glow</b></summary>

Enable your window manager's compositor.
</details>

<details>
<summary><b>Paste mode: pasted into a terminal but nothing happened</b></summary>

Terminals are detected by window class and get `Ctrl+Shift+V`. If yours isn't recognised, add its
`WM_CLASS` (find it with `xprop WM_CLASS`) to `TERMINALS` in `dictate.py`, or use the default `--insert type`.
</details>

## Project layout

```
dictate.py           the app: recorder + segmenter, Whisper worker, overlay (PySide6), typer/paster (XTest)
run.sh               launcher (ROCm override, config.args)
install.sh           venv + dependencies + model + autostart (--cuda / --cpu)
download_model.sh    fetch a Whisper model into ~/.cache/whisper
tools/make_media.py  renders assets/demo.gif and assets/states.png from the real overlay
```

The demo GIF and the state image are rendered headlessly from the actual `Overlay` widget
(`QT_QPA_PLATFORM=offscreen .venv/bin/python tools/make_media.py`). The editor window and the
timings in the GIF are a mock-up.

## Credits

- [OpenAI Whisper](https://github.com/openai/whisper) (MIT)
- [PySide6](https://doc.qt.io/qtforpython-6/), [pynput](https://github.com/moses-palmer/pynput),
  [python-sounddevice](https://github.com/spatialaudio/python-sounddevice), [python-xlib](https://github.com/python-xlib/python-xlib),
  [SciPy](https://scipy.org/)

## License

[MIT](LICENSE)
