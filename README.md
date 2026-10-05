<div align="center">

# WhisperX11

**Voice typing for Linux/X11, powered by [OpenAI Whisper](https://github.com/openai/whisper) on your GPU.**

Press a hotkey, speak, and the text lands in whatever text box has focus,
in English, Persian, or any of Whisper's 99 languages.

<img src="assets/demo.gif" alt="WhisperX11 demo: a glowing overlay listens at the mouse pointer and inserts English and Persian text into an editor" width="760">

</div>

---

## Features

- **Works everywhere:** browsers, editors, chat apps and terminals. Text goes into the focused
  window through the clipboard and a simulated paste.
- **Bilingual out of the box:** each utterance is auto-detected between the languages you
  choose (default `en,fa`), so you can switch language between sentences.
- **Fast:** the model is loaded once, kept on the GPU in fp16 and warmed up at startup.
  On an RX 6700 XT, `turbo` transcribes about 5 s of speech in **~0.8 s**.
- **Hands-free stop:** recording ends after 1.5 s of silence. You can also press the hotkey
  again, or Esc to cancel.
- **Adaptive voice detection:** the speech threshold follows your room's noise floor.
- **Glowing overlay:** a frameless pill at the mouse pointer with a rotating gradient border, a voice-reactive
  glow, a live waveform and a "thinking" animation. It never takes focus, and clicks pass through it.
- **Keyboard-layout proof:** pasting by keycode via XTest works with multi-layout setups
  (e.g. `us,ir`), where simulated per-key typing garbles text.
- **Your clipboard survives:** the previous clipboard contents (text, images, anything) are restored after the paste.
- **AMD ROCm, NVIDIA CUDA or CPU:** it uses whatever PyTorch build you have.

<p align="center"><img src="assets/states.png" alt="Overlay states: Listening, Thinking, Inserted" width="900"></p>

## How it works

```
 Ctrl+Alt+Space ──► overlay at pointer ──► mic (16 kHz mono) ──► silence / hotkey
                                                                     │
   focused window ◄── Ctrl+V via XTest ◄── clipboard ◄── Whisper on GPU (lang: en|fa)
```

1. A global hotkey listener (`pynput`) toggles recording.
2. Audio is captured with `sounddevice`. The noise floor is estimated live, and recording stops after
   `--silence` seconds without speech.
3. Whisper detects the language (restricted to `--languages`) and transcribes it with greedy decoding.
4. The text is put on the clipboard and a `Ctrl+V` keystroke is injected by **keycode** through the X
   XTest extension, or `Ctrl+Shift+V` when the focused window is a terminal. The old clipboard is
   restored one second later.

## Requirements

- Linux with an **X11** session (tested on XFCE, Ubuntu 26.04). Wayland is not supported because global
  hotkeys and input injection work differently there.
- A **compositor** for the translucent glow (XFCE: *Window Manager Tweaks → Compositor*). Without one
  the overlay still works, but sits on a dark box.
- Python 3.10+ and **PyTorch** for your hardware.
- A microphone.

## Install

```bash
git clone https://github.com/Neo-vortex/WhisperX11.git
cd WhisperX11

# 1. PyTorch: if your system Python already has a GPU build, the venv reuses it.
#    Otherwise install the right one, see https://pytorch.org/get-started/locally/
python3 -m venv --system-site-packages .venv
.venv/bin/python -c "import torch; print(torch.cuda.is_available())"   # True = GPU

# 2. Whisper + app dependencies, model download (~1.6 GB for turbo), start on login
./install.sh --autostart
```

`install.sh` installs `openai-whisper` with `--no-deps` on purpose, so pip can't replace your
ROCm/CUDA PyTorch with a different wheel.

## Usage

```bash
./run.sh
```

| Action | Key |
|---|---|
| Start dictation | **Ctrl+Alt+Space** |
| Stop and insert | stop talking for 1.5 s, or **Ctrl+Alt+Space** again |
| Cancel | **Esc** |

Each transcription is logged with its latency and language:

```
[0.79s en] Let's ship the new release on Friday.
[0.96s fa] سلام، این یک آزمایش تایپ صوتی است.
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--model` | `turbo` | `tiny`, `base`, `small`, `medium`, `turbo`, `large`. Use a multilingual model for non-English. |
| `--languages` | `en,fa` | Languages to pick between per utterance. A single value forces that language. |
| `--hotkey` | `<ctrl>+<alt>+<space>` | Any [pynput hotkey](https://pynput.readthedocs.io/en/latest/keyboard.html#global-hotkeys). |
| `--device` | system default | Input device name (substring) or index. |
| `--list-devices` | | Print input devices and exit. |
| `--silence` | `1.5` | Seconds of silence that end a recording (`0` = only the hotkey stops it). |
| `--threshold` | `0.03` | Minimum RMS treated as speech (raised automatically above room noise). |
| `--max-seconds` | `120` | Hard limit per recording. |

**Personal defaults:** put arguments in `config.args` (one per line, git-ignored). `run.sh` picks it up:

```
--device
Rapoo Camera: USB Audio
--languages
en,fa
```

### Model choice

| Model | Size | Speed on GPU | Notes |
|---|---|---|---|
| `turbo` | 1.6 GB | ~0.8 s for 5 s of speech | **Recommended.** large-v3 quality, good Persian |
| `small` | 0.5 GB | faster | OK English, weak Persian |
| `base` / `tiny` | <150 MB | fastest | English-centric, fine on CPU |

## Autostart

`./install.sh --autostart` writes `~/.config/autostart/whisper-x11.desktop`, so the daemon starts when you log
in and keeps the model warm on the GPU. It can't start before login, because it needs your X session.
The log is at `~/.cache/whisper-x11.log`.

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
<summary><b>Text appears in the wrong language</b></summary>

Restrict detection: `--languages fa` (or `en`). Short utterances (one or two words) are the hardest to detect.
</details>

<details>
<summary><b>Overlay has a black box instead of a glow</b></summary>

Enable your window manager's compositor.
</details>

<details>
<summary><b>Pasted into a terminal but nothing happened</b></summary>

Terminals are detected by window class and get `Ctrl+Shift+V`. If yours isn't recognised, add its
`WM_CLASS` (find it with `xprop WM_CLASS`) to `TERMINALS` in `dictate.py`.
</details>

## Project layout

```
dictate.py           the app: recorder, Whisper, overlay (PySide6), paster (XTest)
run.sh               launcher (ROCm override, config.args)
install.sh           venv + dependencies + model + autostart
download_model.sh    fetch a Whisper model into ~/.cache/whisper
tools/make_media.py  renders assets/demo.gif and assets/states.png from the real overlay
```

The demo GIF and the state image are rendered headlessly from the actual `Overlay` widget
(`QT_QPA_PLATFORM=offscreen .venv/bin/python tools/make_media.py`). The editor window in the GIF is a mock.

## Credits

- [OpenAI Whisper](https://github.com/openai/whisper) (MIT)
- [PySide6](https://doc.qt.io/qtforpython-6/), [pynput](https://github.com/moses-palmer/pynput),
  [python-sounddevice](https://github.com/spatialaudio/python-sounddevice), [python-xlib](https://github.com/python-xlib/python-xlib)

## License

[MIT](LICENSE)
