#!/usr/bin/env bash
# Set up WhisperX11: venv, dependencies, model download, optional autostart.
#
#   ./install.sh [--cuda | --cpu] [--model turbo] [--autostart]
#
#   --cuda  (default) use your GPU. You must ALREADY have a working GPU build of PyTorch
#           (NVIDIA CUDA, or AMD ROCm, which PyTorch exposes through the same torch.cuda API)
#           in the Python that creates the venv. This script never installs drivers, CUDA,
#           ROCm or GPU PyTorch wheels.
#   --cpu   run on the CPU. Installs the CPU-only PyTorch wheel if PyTorch is missing.
#           Use a small model (e.g. --model base or small) for acceptable speed.
set -euo pipefail
cd "$(dirname "$0")"
MODE=cuda
MODEL=""
AUTOSTART=0
while [ $# -gt 0 ]; do
    case "$1" in
        --cuda) MODE=cuda; shift ;;
        --cpu) MODE=cpu; shift ;;
        --model) MODEL="$2"; shift 2 ;;
        --autostart) AUTOSTART=1; shift ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -z "$MODEL" ] && { [ "$MODE" = cpu ] && MODEL=small || MODEL=turbo; }

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
    # system site-packages so an existing GPU build of PyTorch is reused
    "$PY" -m venv --system-site-packages .venv
fi

if [ "$MODE" = cuda ]; then
    if ! .venv/bin/python -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
        cat >&2 <<'MSG'
No working GPU PyTorch found (import torch; torch.cuda.is_available() must be True).

--cuda needs a GPU that already works with PyTorch in the Python you run this script with
(set PYTHON=/path/to/python if it is not python3). Set that up first (NVIDIA driver + CUDA
PyTorch, or ROCm + ROCm PyTorch: https://pytorch.org/get-started/locally/), delete .venv,
and run ./install.sh again. Or use ./install.sh --cpu.
MSG
        exit 1
    fi
    RUN_ARGS=(--model "$MODEL")
else
    if ! .venv/bin/python -c "import torch" 2>/dev/null; then
        .venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
    fi
    RUN_ARGS=(--cpu --model "$MODEL")
fi

# --no-deps keeps pip from replacing your PyTorch with a different wheel
.venv/bin/pip install --no-deps openai-whisper silero-vad
.venv/bin/pip install -r requirements.txt
if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    # global hotkeys on Wayland read /dev/input (needs the 'input' group); typing uses wtype or ydotool
    .venv/bin/pip install evdev || echo "evdev failed to build (needs python3-dev + linux headers)" >&2
    command -v wtype >/dev/null || command -v ydotool >/dev/null || \
        echo "note: install wtype (Sway/Hyprland/labwc) or ydotool (GNOME/KDE) for typing on Wayland" >&2
    id -nG | grep -qw input || echo "note: add yourself to the 'input' group for hotkeys: sudo usermod -aG input \$USER" >&2
fi

./download_model.sh "$MODEL"

if [ "$AUTOSTART" = 1 ]; then
    mkdir -p ~/.config/autostart
    cat > ~/.config/autostart/whisper-x11.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=WhisperX11
Comment=Ctrl+Alt+Space to dictate into the focused window
Exec=sh -c 'sleep 3; exec $PWD/run.sh ${RUN_ARGS[*]} >> \$HOME/.cache/whisper-x11.log 2>&1'
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
    echo "autostart installed: ~/.config/autostart/whisper-x11.desktop"
fi

.venv/bin/python -c "import torch; print('PyTorch', torch.__version__, '| device:', torch.cuda.get_device_name(0) if torch.cuda.is_available() and '$MODE' == 'cuda' else 'CPU')"
echo "done — start with: ./run.sh ${RUN_ARGS[*]}   then press Ctrl+Alt+Space"
