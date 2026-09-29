#!/bin/bash
# (Re)create the virtualenv. yt-dlp comes from pacman (updated by `omarchy update`);
# the venv only adds the UI and keyring libraries. Rerun after a Python version upgrade.
set -euo pipefail
cd "$(dirname "$0")"

if ! /usr/bin/python3 -c "import yt_dlp" 2>/dev/null; then
  echo "yt-dlp is not installed system-wide: omarchy pkg add yt-dlp yt-dlp-ejs" >&2
  exit 1
fi

rm -rf .venv
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q textual secretstorage
# compat mode puts the repo on sys.path via a .pth file, ahead of the system
# site-packages, so the repo's code wins over an installed yt-pplayer package.
.venv/bin/pip install -q --no-deps -e . --config-settings editable_mode=compat

.venv/bin/python -c "import yt_dlp, yt_dlp.version as v; print('yt-dlp', v.__version__, 'from', yt_dlp.__file__)"
