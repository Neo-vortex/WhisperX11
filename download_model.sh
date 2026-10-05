#!/usr/bin/env bash
# Download a Whisper model into ~/.cache/whisper (needs internet).
#   ./download_model.sh [turbo|medium|large-v3|small|...]   (default: turbo)
# Resumable: if the connection drops, run the same command again. The file only appears under its
# final name after its SHA-256 checksum is verified, so a partial download never counts as installed.
cd "$(dirname "$0")"
exec .venv/bin/python - "${1:-turbo}" <<'PY'
import hashlib, os, sys, time, urllib.request
import whisper

name = sys.argv[1]
if name not in whisper._MODELS:
    sys.exit(f"unknown model {name!r}; choose from: {', '.join(whisper._MODELS)}")
url = whisper._MODELS[name]
sha = url.split("/")[-2]
root = os.path.expanduser("~/.cache/whisper")
os.makedirs(root, exist_ok=True)
final = os.path.join(root, os.path.basename(url))
part = final + ".part"

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

if os.path.exists(final):
    if sha256(final) == sha:
        sys.exit(print(f"ok: {final} (already downloaded)"))
    os.rename(final, part)  # corrupt or incomplete: try to resume it

for attempt in range(1, 31):
    have = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            if have and r.status != 206:  # server ignored the range: start over
                have = 0
            total = have + int(r.headers.get("Content-Length", 0))
            with open(part, "ab" if have else "wb") as f:
                done, t0, last = have, time.monotonic(), 0.0
                while block := r.read(1 << 20):
                    f.write(block)
                    done += len(block)
                    now = time.monotonic()
                    if now - last > 0.5 or done == total:
                        speed = (done - have) / max(now - t0, 1e-6) / 1e6
                        print(f"\r{name}: {done / 1e6:7.1f} / {total / 1e6:.1f} MB  {speed:5.1f} MB/s", end="", flush=True)
                        last = now
        print()
        break
    except Exception as e:
        print(f"\nconnection problem ({e}); retrying in 5 s [{attempt}/30]", flush=True)
        time.sleep(5)
else:
    sys.exit(f"download failed; run the same command again to resume ({part})")

print("verifying checksum ...", flush=True)
if sha256(part) != sha:
    os.remove(part)
    sys.exit("checksum mismatch; the partial file was removed, run the command again")
os.replace(part, final)
print(f"ok: {final}")
PY
