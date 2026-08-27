#!/usr/bin/env bash
# Run this ON COMPUTER A, while it still has internet access.
#
# It downloads every dependency as a wheel into vendor/wheels/ so the whole repo can
# be copied to the air-gapped machine and installed there with no network.
#
# All of these resolve to pure-Python `py3-none-any` wheels, which means the downloads
# are portable to any OS and CPU architecture — the check at the end enforces that.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
dest="$here/wheels"

mkdir -p "$dest"

python3 -m pip download \
    -d "$dest" \
    -r "$repo/requirements.txt" \
    -r "$repo/requirements-optional.txt"

echo
echo "Checking every wheel is platform-independent..."
bad=0
for wheel in "$dest"/*.whl; do
    case "$(basename "$wheel")" in
        *-py3-none-any.whl|*-py2.py3-none-any.whl) ;;
        *)
            echo "  NOT PORTABLE: $(basename "$wheel")"
            bad=1
            ;;
    esac
done

if [ "$bad" -ne 0 ]; then
    echo
    echo "A platform-specific wheel was downloaded. It will only install on a machine"
    echo "matching this one's OS, architecture, and Python version. Either pin a"
    echo "different version or drop the dependency." >&2
    exit 1
fi

echo
echo "Done. $(ls -1 "$dest"/*.whl | wc -l | tr -d ' ') wheels in $dest"
echo "Copy the whole repository to Computer B and run vendor/install_offline.sh there."
