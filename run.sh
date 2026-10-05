#!/usr/bin/env bash
# Start the dictation daemon. Extra args go to dictate.py (see --help).
# Personal defaults can live in config.args (one argument per line), e.g.:
#   --device
#   Rapoo Camera: USB Audio
cd "$(dirname "$0")"

# RDNA2 cards other than gfx1030 (e.g. RX 6700 XT = gfx1031) need the gfx1030 kernels on ROCm
if [ -z "${HSA_OVERRIDE_GFX_VERSION:-}" ] && command -v rocminfo >/dev/null \
    && rocminfo 2>/dev/null | grep -qE 'gfx103[1-6]'; then
    export HSA_OVERRIDE_GFX_VERSION=10.3.0
fi

extra=()
[ -f config.args ] && extra=(@config.args)
exec .venv/bin/python dictate.py "${extra[@]}" "$@"
