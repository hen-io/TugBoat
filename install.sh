#!/bin/sh
set -eu

RAW="${TUGBOAT_RAW:-https://raw.githubusercontent.com/hen-io/TugBoat/main}"
DIR="${TUGBOAT_DIR:-/opt/TugBoat}"
LINK="${TUGBOAT_LINK:-/usr/local/bin/tugboat}"

fail() {
    printf 'x %s\n' "$*" >&2
    exit 1
}

say() {
    printf '%s\n' "$*"
}

[ "$(id -u)" -eq 0 ] || fail "run as root:  wget -qO install.sh $RAW/install.sh && sudo sh install.sh"
command -v python3 >/dev/null 2>&1 || fail "python3 is needed (Debian/Ubuntu: apt install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "Python 3.9 or newer is needed"
command -v docker >/dev/null 2>&1 || fail "docker is not installed"
command -v crontab >/dev/null 2>&1 || fail "cron is not installed (Debian/Ubuntu: apt install cron)"

if command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
elif command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
else
    fail "wget or curl is needed"
fi

set_conf() {
    awk -v key="$1" -v value="$2" '
        $0 ~ "^" key ":" { print key ": " value; done = 1; next }
        { print }
        END { if (!done) print key ": " value }
    ' "$CONF" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say "Downloading TugBoat from $RAW"
fetch "$RAW/TugBoat.py" "$TMP/TugBoat.py" || fail "could not download TugBoat.py"
python3 -c 'import sys; compile(open(sys.argv[1], encoding="utf-8-sig").read(), "TugBoat.py", "exec")' "$TMP/TugBoat.py" \
    || fail "the downloaded TugBoat.py is not a valid script"

mkdir -p "$DIR"
SCRIPT="$DIR/TugBoat.py"
CONF="$DIR/TugBoat.conf"

if [ -f "$SCRIPT" ]; then
    cp -p "$SCRIPT" "$DIR/.TugBoat.py.bak"
fi
cp "$TMP/TugBoat.py" "$SCRIPT.new"
chmod 755 "$SCRIPT.new"
mv "$SCRIPT.new" "$SCRIPT"

if [ -f "$CONF" ]; then
    say "Keeping your existing $CONF"
else
    fetch "$RAW/TugBoat.conf" "$CONF" || fail "could not download TugBoat.conf"
    chmod 644 "$CONF"
    if [ -n "${TUGBOAT_CONTAINER_PATH:-}" ]; then
        set_conf container_path "$TUGBOAT_CONTAINER_PATH"
        set_conf backup_path "$TUGBOAT_CONTAINER_PATH/.backup/\$STACK-NAME"
    fi
    CONTAINERS="$(awk -F': *' '$1 == "container_path" { print $2 }' "$CONF" | tr -d '\r')"
    set_conf status_file "$CONTAINERS/tugboat.json"
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        set_conf docker_user "$SUDO_USER"
        say "Docker commands will run as $SUDO_USER (docker_user in TugBoat.conf)"
    else
        set_conf docker_user ""
    fi
    say "Created $CONF"
fi

if [ -d "$(dirname "$LINK")" ]; then
    ln -sf "$SCRIPT" "$LINK"
fi

say "Installed $SCRIPT"
say "Setting up the health check cron job"
if python3 "$SCRIPT" --install; then
    say ""
    say "Done. Run it with:  sudo tugboat      (settings: $CONF)"
else
    say ""
    say "TugBoat is installed, but the cron job was not added."
    say "Edit $CONF (container_path must exist), then run:  sudo python3 $SCRIPT --install"
    exit 1
fi
