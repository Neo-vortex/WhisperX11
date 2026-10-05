#!/usr/bin/env bash
# Set up WhisperX11: venv, dependencies, model download, optional autostart.
#   ./install.sh [--model turbo] [--autostart]
set -euo pipefail
cd "$(dirname "$0")"
MODEL=turbo
AUTOSTART=0
while [ $# -gt 0 ]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --autostart) AUTOSTART=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
    # system site-packages so an existing GPU build of PyTorch (ROCm/CUDA) is reused
    "$PY" -m venv --system-site-packages .venv
fi

if ! .venv/bin/python -c "import torch" 2>/dev/null; then
    cat >&2 <<'MSG'
PyTorch is not installed. Install the build for your GPU first (https://pytorch.org/get-started/locally/):
  AMD (ROCm):  .venv/bin/pip install torch --index-url https://download.pytorch.org/whl/rocm6.4
  NVIDIA:      .venv/bin/pip install torch
  CPU only:    .venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
then run ./install.sh again.
MSG
    exit 1
fi

# --no-deps keeps pip from replacing your GPU PyTorch with a default (CUDA) wheel
.venv/bin/pip install --no-deps openai-whisper
.venv/bin/pip install -r requirements.txt

./download_model.sh "$MODEL"

if [ "$AUTOSTART" = 1 ]; then
    mkdir -p ~/.config/autostart
    cat > ~/.config/autostart/whisper-x11.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=WhisperX11
Comment=Ctrl+Alt+Space to dictate into the focused window
Exec=sh -c 'sleep 3; exec $PWD/run.sh --model $MODEL >> \$HOME/.cache/whisper-x11.log 2>&1'
Terminal=false
X-GNOME-Autostart-enabled=true
DESKTOP
    echo "autostart installed: ~/.config/autostart/whisper-x11.desktop"
fi

.venv/bin/python -c "import torch; print('PyTorch', torch.__version__, '| GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none (CPU)')"
echo "done — start with ./run.sh, then press Ctrl+Alt+Space"
