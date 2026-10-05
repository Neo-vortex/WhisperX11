#!/usr/bin/env bash
# Download a whisper model into ~/.cache/whisper without loading it (needs internet).
cd "$(dirname "$0")"
exec .venv/bin/python - "${1:-turbo}" <<'PY'
import os, sys, whisper
name = sys.argv[1]
path = whisper._download(whisper._MODELS[name], os.path.expanduser("~/.cache/whisper"), False)
print("ok:", path)
PY
