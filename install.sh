#!/usr/bin/env bash
# Linux/macOS: same command installs or updates. No download worker is started.
set -euo pipefail
if ! command -v uv >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  if command -v curl >/dev/null 2>&1; then
    curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo 'curl or wget is required to bootstrap uv.' >&2; exit 1
  fi
fi
UV="$(command -v uv || true)"
UV="${UV:-$HOME/.local/bin/uv}"
if [[ "${1:-}" == '--local' ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  exec "$UV" run --no-project --python 3.12 python "$ROOT/scripts/install_update.py" --uv "$UV" --local "$ROOT"
fi
export PATH="$HOME/.local/bin:$PATH"
if ! command -v gh >/dev/null 2>&1; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "$UV" run --no-project --python 3.12 python "$ROOT/scripts/bootstrap_gh.py"
fi
if ! gh auth status >/dev/null 2>&1; then gh auth login --web --git-protocol https; fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# gh supplies authentication; no tokens are written to disk or command arguments.
gh api repos/onetrickpony1/cloudisle-dl/contents/scripts/install_update.py --jq .content > "$TMP/helper.b64"
"$UV" run --no-project --python 3.12 python -c 'import base64,pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(base64.b64decode(pathlib.Path(sys.argv[1]).read_bytes()))' "$TMP/helper.b64" "$TMP/install_update.py"
"$UV" run --no-project --python 3.12 python "$TMP/install_update.py" --uv "$UV"
