#!/usr/bin/env bash
# Run this ON COMPUTER B (the air-gapped machine).
#
# Installs from the wheels committed in vendor/wheels/ with networking disabled
# entirely (--no-index), so it cannot silently reach out to PyPI.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
wheels="$here/wheels"

if [ ! -d "$wheels" ] || [ -z "$(ls -A "$wheels" 2>/dev/null)" ]; then
    echo "No wheels found in $wheels." >&2
    echo "Run vendor/fetch_wheels.sh on Computer A first, then copy this repo over." >&2
    exit 1
fi

python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Python $python_version is too old — click 8.3 needs 3.10 or newer." >&2
    exit 1
fi

echo "Installing required dependencies..."
python3 -m pip install --no-index --find-links "$wheels" -r "$repo/requirements.txt"

echo
echo "Installing optional interface dependencies..."
if ! python3 -m pip install --no-index --find-links "$wheels" \
        -r "$repo/requirements-optional.txt"; then
    echo
    echo "The optional packages could not be installed. That is not fatal —"
    echo "git-air-sync will run in plain-text mode."
fi

echo
echo "Installing git-air-sync itself..."
python3 -m pip install --no-index --no-build-isolation -e "$repo" || {
    echo
    echo "Editable install failed (it needs setuptools). You can still run the tool"
    echo "directly from this directory with:"
    echo "    python3 -m git_air_sync"
}

echo
echo "Checking the installation..."
cd "$repo" && python3 -m git_air_sync doctor
