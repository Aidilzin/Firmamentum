#!/bin/sh
# Copy the read-only source to the writable /work (no .git, no host venv), then run the command given to `docker run`.
set -e
rsync -a --exclude .git --exclude .venv --exclude __pycache__ /src/ /work/
cd /work
exec "$@"
