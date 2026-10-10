#!/bin/sh
set -eu

ROOT="${TUGBOAT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DATA="$ROOT/TugBoat"
STATE="$DATA/state"
CONF="$DATA/TugBoat.conf"

move() {
    if [ -e "$1" ] && [ ! -e "$2" ]; then
        mkdir -p "$(dirname "$2")"
        mv "$1" "$2"
        echo "moved $1 -> $2"
    fi
}

conf_get() {
    awk -v key="$1" '
        $0 ~ "^" key "[ \t]*:" {
            sub("^[^:]*:[ \t]*", ""); sub("[ \t]+#.*$", ""); sub("[ \t\r]+$", "")
            gsub("^[\"\047]|[\"\047]$", ""); print; exit
        }' "$WORK"
}

conf_first() {
    awk -v key="$1" '
        found && /^[ \t]*-/ { sub("^[ \t]*-[ \t]*", ""); sub("[ \t]+#.*$", ""); sub("[ \t\r]+$", ""); gsub("^[\"\047]|[\"\047]$", ""); print; exit }
        found && !/^[ \t]*(#|$)/ { exit }
        $0 ~ "^" key "[ \t]*:" {
            sub("^[^:]*:[ \t]*", ""); sub("[ \t]+#.*$", ""); sub("[ \t\r]+$", "")
            if ($0 != "") { split($0, parts, ","); v = parts[1]; gsub("^[ \t\"\047]+|[ \t\"\047]+$", "", v); print v; exit }
            found = 1
        }' "$WORK"
}

conf_set() {
    awk -v key="$1" -v value="$2" '
        $0 ~ "^" key "[ \t]*:" { print key ": " value; next }
        { print }' "$WORK" > "$WORK.tmp" && mv "$WORK.tmp" "$WORK"
}

conf_to_list() {
    awk -v key="$1" '
        $0 ~ "^" key "[ \t]*:[ \t]*[^ \t#\r]" {
            sub("^[^:]*:[ \t]*", ""); sub("[ \t]+#.*$", ""); sub("[ \t\r]+$", "")
            print key ":"
            n = split($0, parts, ",")
            for (i = 1; i <= n; i++) {
                v = parts[i]; gsub("^[ \t\"\047]+|[ \t\"\047]+$", "", v)
                if (v != "") print "  - \"" v "\""
            }
            next
        }
        { print }' "$WORK" > "$WORK.tmp" && mv "$WORK.tmp" "$WORK"
}

absolute() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        ./*) printf '%s\n' "$ROOT/${1#./}" ;;
        .) printf '%s\n' "$ROOT" ;;
        *) printf '%s\n' "$ROOT/$1" ;;
    esac
}

mkdir -p "$STATE"

move "$ROOT/TugBoat.conf" "$CONF"
move "$ROOT/.TugBoat.conf.bak" "$STATE/config.bak"
move "$ROOT/.TugBoat.conf.defaults" "$STATE/config.defaults"
move "$ROOT/.TugBoat.crontab.bak" "$STATE/crontab.bak"
move "$ROOT/.TugBoat.deps.failed" "$STATE/deps.failed"
move "$ROOT/.TugBoat.py.bak" "$STATE/script-previous.bak"
for old in "$ROOT"/.TugBoat.py.*.bak; do
    [ -e "$old" ] || continue
    version="${old##*/.TugBoat.py.}"
    move "$old" "$STATE/script-${version%.bak}.bak"
done

[ -f "$CONF" ] || exit 0
WORK="$STATE/config.fixing"
cp -p "$CONF" "$WORK"

if grep -q '^container_path[ \t]*:' "$WORK"; then
    if grep -q '^stacks_directory[ \t]*:' "$WORK"; then
        sed -i '/^container_path[ \t]*:/d' "$WORK"
    else
        sed -i 's/^container_path[ \t]*:/stacks_directory:/' "$WORK"
    fi
fi

sed -i -e '/^docker_stack_up_cmd[ \t]*:/d' -e '/^docker_stack_down_cmd[ \t]*:/d' \
    -e '/^docker_stack_start_cmd[ \t]*:/d' -e '/^docker_stack_restore_cmd[ \t]*:/d' "$WORK"

conf_to_list stacks_directory
conf_to_list ignore_folders

STACKS="$(conf_first stacks_directory)"
STACKS="${STACKS:-.}"

STATUS="$(conf_get status_file)"
if [ "$STATUS" = "$STACKS/tugboat.json" ] || [ "$STATUS" = "./tugboat.json" ]; then
    move "$(absolute "$STATUS")" "$DATA/tugboat.json"
    conf_set status_file "./TugBoat/tugboat.json"
fi

BACKUPS="$(conf_get backup_path)"
if [ "$BACKUPS" = "$STACKS/.backup/\$STACK-NAME" ]; then
    OLD="$(absolute "$STACKS")/.backup"
    if [ -d "$OLD" ]; then
        for stack in "$OLD"/*/; do
            [ -d "$stack" ] || continue
            name="$(basename "$stack")"
            for item in "$stack"*; do
                [ -e "$item" ] || continue
                move "$item" "$DATA/backups/$name/$(basename "$item")"
            done
            rmdir "$stack" 2>/dev/null || true
        done
        rmdir "$OLD" 2>/dev/null || true
    fi
    conf_set backup_path './TugBoat/backups/$STACK-NAME'
fi

if cmp -s "$WORK" "$CONF"; then
    rm -f "$WORK"
else
    cp -p "$CONF" "$STATE/config.bak"
    mv "$WORK" "$CONF"
    echo "updated $CONF (old one kept as $STATE/config.bak)"
fi
