#!/bin/sh
set -eu

ROOT="${TUGBOAT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DATA="$ROOT/.TugBoat"
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

merge() {
    mkdir -p "$2"
    for entry in "$1"/* "$1"/.[!.]*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        target="$2/$(basename "$entry")"
        if [ ! -e "$target" ]; then
            mv "$entry" "$target"
        elif [ -d "$entry" ] && [ -d "$target" ]; then
            merge "$entry" "$target"
        fi
    done
    rmdir "$1" 2>/dev/null || true
}

mkdir -p "$STATE"

OLD_DATA="$ROOT/TugBoat"
if [ -d "$OLD_DATA" ] && [ ! -f "$OLD_DATA/compose.yaml" ] && [ ! -f "$OLD_DATA/compose.yml" ] \
    && [ ! -f "$OLD_DATA/docker-compose.yaml" ] && [ ! -f "$OLD_DATA/docker-compose.yml" ]; then
    merge "$OLD_DATA" "$DATA"
    echo "moved $OLD_DATA -> $DATA"
    if [ -d "$OLD_DATA" ]; then
        rm -rf "$STATE/old-TugBoat-folder"
        mv "$OLD_DATA" "$STATE/old-TugBoat-folder"
    fi
fi

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
    conf_set status_file "./.TugBoat/tugboat.json"
elif [ "$STATUS" = "./TugBoat/tugboat.json" ]; then
    conf_set status_file "./.TugBoat/tugboat.json"
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
    conf_set backup_path './.TugBoat/backups/$STACK-NAME'
elif [ "$BACKUPS" = './TugBoat/backups/$STACK-NAME' ]; then
    conf_set backup_path './.TugBoat/backups/$STACK-NAME'
fi

MINUTES="$(conf_get update_check_interval)"
if grep -q '^update_check_interval[ \t]*:' "$WORK"; then
    if ! grep -q '^update_check_seconds[ \t]*:' "$WORK"; then
        case "$MINUTES" in
            ''|60) SECONDS_VALUE=1800 ;;
            *[!0-9]*) SECONDS_VALUE=1800 ;;
            *) SECONDS_VALUE=$((MINUTES * 60)) ;;
        esac
        sed -i "s/^update_check_interval[ \t]*:.*/update_check_seconds: $SECONDS_VALUE/" "$WORK"
        sed -i 's/^# Minutes between checks for a new TugBoat release$/# Seconds between checks for a new TugBoat release (checked when TugBoat starts)/' "$WORK"
    else
        sed -i '/^update_check_interval[ \t]*:/d' "$WORK"
    fi
fi

if cmp -s "$WORK" "$CONF"; then
    rm -f "$WORK"
else
    cp -p "$CONF" "$STATE/config.bak"
    mv "$WORK" "$CONF"
    echo "updated $CONF (old one kept as $STATE/config.bak)"
fi
