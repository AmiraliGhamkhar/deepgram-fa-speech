#!/usr/bin/env bash
# Regenerate the three dependency lockfiles in requirements/.
#
# The .txt files stay the human-edited inputs (version ranges); the .lock
# files are their compiled, hash-pinned resolutions and are what CI and
# the Docker image actually install (`pip install --require-hashes`).
# Edit a range -> run this script -> review the .lock diff like code ->
# commit both together. Never hand-edit a .lock file.
#
# Locks are UNIVERSAL (one file for Linux+Windows x Python 3.10-3.12,
# environment markers and all), which neither pip-compile nor pip can
# produce on a single machine -- hence `uv`, used ONLY as the generator
# here. It is not a runtime or CI dependency: installs stay plain pip.
# Get it via `pipx install uv` or `pip install uv` (any >= 0.5).
# dev.lock is compiled CONSTRAINED by the two runtime locks, so CI tests
# exactly the versions that ship, plus the dev tools.
set -euo pipefail
cd "$(dirname "$0")/.."

UV="uv"
command -v uv >/dev/null 2>&1 || UV="python3 -m uv"
$UV --version >/dev/null 2>&1 || {
    echo "uv not found: install it with 'pipx install uv' or 'pip install uv' (generator only)" >&2
    exit 1
}

$UV pip compile requirements.txt \
    --universal --generate-hashes --python-version 3.10 \
    --output-file requirements/client.lock
$UV pip compile host/requirements.txt \
    --universal --generate-hashes --python-version 3.10 \
    --output-file requirements/host.lock
$UV pip compile requirements-dev.txt host/requirements-dev.txt \
    --universal --generate-hashes --python-version 3.10 \
    -c requirements/client.lock -c requirements/host.lock \
    --output-file requirements/dev.lock

echo "Locks regenerated. Verify before committing:"
echo "  python3 -m venv /tmp/lockcheck && /tmp/lockcheck/bin/pip install --require-hashes -r requirements/dev.lock"
