#!/bin/sh
# Copy the read-only source to the writable /work (no .git, no host venv), then run the command given to `docker run`.
# Two exit codes are reserved so orch can tell "the sandbox is not ready" from "the tests failed":
#   96  the venv volume is read-only and was never prepared (no /venv/.orch-ready); orch warms it and runs again
#   97  copying /src to /work failed (for example the tmpfs is full)
set -e
if [ ! -w /venv ] && [ ! -f /venv/.orch-ready ]; then
    echo "orch sandbox: the venv volume is not prepared" >&2
    exit 96
fi
if ! rsync -a --exclude .git --exclude .venv --exclude __pycache__ /src/ /work/; then
    echo "orch sandbox: copying /src to /work failed" >&2
    exit 97
fi
cd /work
exec "$@"
