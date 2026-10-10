#!/bin/sh
set -eu

RAW="${TUGBOAT_RAW:-https://raw.githubusercontent.com/hen-io/TugBoat/main}"
DIR="${TUGBOAT_CONTAINER_PATH:-/container-data}"
LINK="${TUGBOAT_LINK:-/usr/local/bin/tugboat}"

fail() {
    printf 'x %s\n' "$*" >&2
    exit 1
}

say() {
    printf '%s\n' "$*"
}

[ "$(id -u)" -eq 0 ] || fail "run as root:  wget -qO install.sh $RAW/install.sh && sudo sh install.sh"

PM=""
for tool in apt-get dnf pacman; do
    if command -v "$tool" >/dev/null 2>&1; then
        PM="$tool"
        break
    fi
done

install_packages() {
    case "$PM" in
        apt-get) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
        dnf) dnf install -y "$@" ;;
        pacman) pacman -S --noconfirm --needed "$@" || pacman -Sy --noconfirm --needed "$@" ;;
        *) return 1 ;;
    esac
}

python_ok() {
    command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
}

if ! python_ok; then
    [ "${TUGBOAT_INSTALL_DEPS:-1}" = "0" ] && fail "Python 3.9 or newer is needed"
    [ -n "$PM" ] || fail "Python 3.9 or newer is needed, and no supported package manager (apt, dnf, pacman) was found"
    PYTHON_PACKAGE=python3
    [ "$PM" = "pacman" ] && PYTHON_PACKAGE=python
    say "Installing $PYTHON_PACKAGE with $PM"
    install_packages "$PYTHON_PACKAGE" || fail "could not install $PYTHON_PACKAGE"
    python_ok || fail "Python 3.9 or newer is needed"
fi

DOCKER_USER="${TUGBOAT_DOCKER_USER:-}"
if [ -n "$DOCKER_USER" ]; then
    id "$DOCKER_USER" >/dev/null 2>&1 || fail "TUGBOAT_DOCKER_USER: the user '$DOCKER_USER' does not exist"
fi

if command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
elif command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
else
    [ -n "$PM" ] || fail "wget or curl is needed"
    say "Installing wget with $PM"
    install_packages wget || fail "could not install wget"
    fetch() { wget -q -O "$2" "$1"; }
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
python3 "$TMP/TugBoat.py" --version >/dev/null 2>&1 || fail "the downloaded TugBoat.py does not run - nothing was installed"

mkdir -p "$DIR"
SCRIPT="$DIR/TugBoat.py"
CONF="$DIR/TugBoat.conf"

if [ -f "$SCRIPT" ]; then
    mkdir -p "$DIR/TugBoat/state"
    cp -p "$SCRIPT" "$DIR/TugBoat/state/script-previous.bak"
fi
cp "$TMP/TugBoat.py" "$SCRIPT.new"
chmod 755 "$SCRIPT.new"
mv "$SCRIPT.new" "$SCRIPT"

if [ -f "$CONF" ]; then
    say "Keeping your existing $CONF"
    if [ "${TUGBOAT_INSTALL_DEPS:-}" = "0" ]; then
        set_conf install_dependencies false
    fi
    if [ -n "${TUGBOAT_CONTAINER_PATH:-}" ]; then
        set_conf container_path "$TUGBOAT_CONTAINER_PATH"
        say "container_path set to $TUGBOAT_CONTAINER_PATH"
    fi
    if [ -n "$DOCKER_USER" ]; then
        set_conf docker_user "$DOCKER_USER"
        say "docker_user set to $DOCKER_USER"
    fi
else
    fetch "$RAW/TugBoat.conf" "$CONF" || fail "could not download TugBoat.conf"
    chmod 644 "$CONF"
    if [ -z "$DOCKER_USER" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        DOCKER_USER="$SUDO_USER"
    fi
    set_conf docker_user "$DOCKER_USER"
    if [ "${TUGBOAT_INSTALL_DEPS:-}" = "0" ]; then
        set_conf install_dependencies false
    fi
    if [ -n "$DOCKER_USER" ]; then
        say "Docker commands will run as $DOCKER_USER (docker_user in TugBoat.conf)"
    fi
    say "Created $CONF"
fi

if [ -d "$(dirname "$LINK")" ]; then
    ln -sf "$SCRIPT" "$LINK"
fi

say "Installed $SCRIPT"
say "Checking Docker and cron, then setting up the health check cron job"
if python3 "$SCRIPT" --install; then
    say ""
    say "Done. Run it with:  sudo tugboat      (settings: $CONF)"
else
    say ""
    say "TugBoat is installed, but the cron job was not added."
    say "Edit $CONF (container_path must exist), then run:  sudo python3 $SCRIPT --install"
    exit 1
fi
