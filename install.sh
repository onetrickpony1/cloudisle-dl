#!/bin/sh
# cloudisle-dl installer for macOS and Linux. No admin rights needed.
#
#   curl -LsSf https://raw.githubusercontent.com/onetrickpony1/cloudisle-dl/main/install.sh | sh
#
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if missing: it manages Python for cloudisle-dl
#   2. installs cloudisle-dl in its own isolated environment, with its own Python if needed
#   3. puts cloudisle-dl on your PATH
# No browser is required. Existing workers and data are not changed.
# Run it again to update. Uninstall: uv tool uninstall cloudisle-dl
set -eu

SOURCE="${CLOUDISLE_DL_SOURCE:-cloudisle-dl @ https://github.com/onetrickpony1/cloudisle-dl/archive/refs/heads/main.zip}"
PYTHON="${CLOUDISLE_DL_PYTHON:-3.12}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\ncloudisle-dl installer: %s\n' "$*" >&2; exit 1; }
say "Installing cloudisle-dl"

# 1. uv
if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    say "Installing uv (Python manager, from astral.sh)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "needs curl or wget to download uv"
    fi
    for c in "$HOME/.local/bin/uv" "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.cargo/bin/uv"; do
        [ -x "$c" ] && UV="$c" && break
    done
    [ -n "${UV:-}" ] || fail "uv was installed but can't be found; open a new terminal and run this again"
fi

# 2. cloudisle-dl (+ Python if needed)
say "Installing cloudisle-dl and its packages (Python $PYTHON is downloaded if needed)"
"$UV" tool install --force --refresh --python "$PYTHON" "$SOURCE" || fail "installing cloudisle-dl failed (see above)"

# 3. PATH
"$UV" tool update-shell >/dev/null 2>&1 || true
BIN="$("$UV" tool dir --bin)"
CDL="$BIN/cloudisle-dl"
[ -x "$CDL" ] || fail "cloudisle-dl was installed but $CDL is missing"

say "Done: $("$CDL" --version)"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) printf 'Open a NEW terminal so the commands are found (they are in %s).\n' "$BIN" ;;
esac
cat <<'EOF'

Configure:   cloudisle-dl init --destination PATH
Background:  cloudisle-dl start
Status:      cloudisle-dl status
Stop/resume: cloudisle-dl stop / cloudisle-dl resume
Local disk:  add --allow-local to init.
No browser or GitHub login is required. Installation does not start or restart downloads.
After updating, stop and resume a packaged worker to load the new version.
EOF
