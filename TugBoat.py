#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import fcntl
import functools
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

__title__ = "TugBoat"
__version__ = "0.5.3"
__author__ = "Henrik Isefjær Olsen"
__git__ = "https://github.com/hen-io/TugBoat"

CRON_EVERY_MINUTES = 1

CONFIG_DEFAULTS = {
    "container_path": "/container-data",
    "require_root": "true",
    "status_file": "{container_path}/tugboat.json",
    "ignore_folders": "",
    "backup": "true",
    "backup_path": "{container_path}/.backup/$STACK-NAME",
    "backup_retention": "10",
    "backup_large_mb": "0",
    "backup_large_retention": "2",
    "docker_user": "",
    "health_wait": "60",
    "image_check": "true",
    "image_check_interval": "60",
    "manage_cron": "true",
    "install_dependencies": "true",
    "command_timeout": "0",
    "update_check": "true",
    "auto_update": "true",
}
OBSOLETE_CONFIG_KEYS = ("docker_stack_up_cmd", "docker_stack_down_cmd", "docker_stack_start_cmd",
                        "docker_stack_restore_cmd")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "TugBoat.conf"

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
TIMESTAMP_FORMAT = "%d_%m_%y_%H_%M_%S"
STACK_PLACEHOLDER = "$STACK-NAME"
ERROR_TAIL_LINES = 20


IS_TTY = sys.stdout.isatty()
UNICODE = "utf" in (sys.stdout.encoding or "").lower()

SYM = {
    "ok":   "✓" if UNICODE else "+",
    "fail": "✗" if UNICODE else "x",
    "warn": "!",
    "skip": "–" if UNICODE else "-",
    "dry":  "○" if UNICODE else "o",
    "bar":  "│" if UNICODE else "|",
    "arrow": "›" if UNICODE else ">",
    "up":   "↑" if UNICODE else "^",
    "rule": "─" if UNICODE else "-",
}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if UNICODE else "|/-\\"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if IS_TTY else text


def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)
def red(t: str) -> str:    return _c("31", t)
def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str) -> str:   return _c("36", t)


STATUS_STYLE = {"ok": green, "fail": red, "warn": yellow, "skip": dim, "dry": cyan}


def write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")


def say(msg: str = "") -> None:
    write(msg + "\n")


def say_error(msg: str) -> None:
    say(red(f"{SYM['fail']} {msg}"))


def term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def rule(title: str = "") -> None:
    width = min(term_width(), 80)
    if title:
        say("\n" + bold(title) + " " + dim(SYM["rule"] * max(0, width - len(title) - 1)))
    else:
        say(dim(SYM["rule"] * width))


def banner() -> None:
    lines = [f"{__title__} v{__version__}", "Docker Compose stack updater",
             f"by {__author__}", __git__]
    h, v, tl, tr, bl, br = ("─", "│", "╭", "╮", "╰", "╯") if UNICODE else ("-", "|", "+", "+", "+", "+")
    width = max(len(line) for line in lines) + 4
    say(cyan(tl + h * width + tr))
    for i, line in enumerate(lines):
        text = f"  {line}".ljust(width)
        say(cyan(v) + (bold(cyan(text)) if i == 0 else text) + cyan(v))
    say(cyan(bl + h * width + br))


@dataclass
class Issue:
    stack: str
    step: str
    message: str
    details: list[str] = field(default_factory=list)
    output: list[str] = field(default_factory=list)


@dataclass
class StackResult:
    name: str
    ok: bool = True
    status: str = ""
    seconds: float = 0.0
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    backup: str | None = None
    health: dict | None = None


StepWork = Callable[[Callable[[str], None]], "tuple[bool, str, list[str]]"]


@dataclass
class StepOutcome:
    ok: bool
    warn: bool
    note: str
    details: list[str]
    output: list[str]
    seconds: float


CANCEL = threading.Event()
ACTIVE_PROCS: set[subprocess.Popen] = set()


def stop_active(grace: float = 10) -> None:
    CANCEL.set()
    procs = list(ACTIVE_PROCS)
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    for proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


class Runner:
    def __init__(self, verbose: bool, dry_run: bool):
        self.verbose = verbose
        self.dry_run = dry_run

    def _line(self, status: str, label: str, seconds: float | None, note: str) -> None:
        sym = STATUS_STYLE[status](SYM[status])
        t = dim(f"{fmt_time(seconds):>7}") if seconds is not None else " " * 7
        say(f"  {sym} {label:<20} {t}  {note}".rstrip())

    def skip(self, label: str, note: str) -> None:
        self._line("skip", label, None, dim(note))

    def dry(self, label: str, note: str) -> None:
        self._line("dry", label, None, dim(note))

    def step(self, label: str, work: StepWork, warn_only: bool = False,
             timeout: float = 0) -> StepOutcome:
        output: list[str] = []
        result: dict = {}
        timed_out = False
        CANCEL.clear()

        def emit(line: str) -> None:
            output.append(line)
            if self.verbose:
                say(dim(f"      {SYM['bar']} {line}"))

        def worker() -> None:
            try:
                result["r"] = work(emit)
            except Exception as e:
                result["r"] = (False, f"{type(e).__name__}: {e}", [])

        start = time.monotonic()
        if self.verbose:
            say(f"  {cyan(SYM['arrow'])} {label}")
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        frame = 0
        try:
            while t.is_alive():
                if timeout and not timed_out and time.monotonic() - start > timeout:
                    timed_out = True
                    stop_active()
                if IS_TTY and not self.verbose:
                    elapsed = fmt_time(time.monotonic() - start)
                    last = output[-1].strip() if output else ""
                    room = max(0, term_width() - 36)
                    write(f"\r\033[K  {cyan(SPINNER[frame % len(SPINNER)])} "
                                     f"{label:<20} {dim(f'{elapsed:>7}')}  {dim(last[:room])}")
                    frame += 1
                t.join(0.1)
        except KeyboardInterrupt:
            stop_active()
            t.join(30)
            raise
        finally:
            if IS_TTY and not self.verbose:
                write("\r\033[K")

        ok, note, details = result.get("r", (False, "no result", []))
        if timed_out:
            ok, note = False, f"timed out after {fmt_time(timeout)}"
        seconds = time.monotonic() - start
        if ok is True:
            status = "ok"
        elif ok == "warn" or warn_only:
            status = "warn"
        else:
            status = "fail"
        style = {"ok": dim, "warn": yellow, "fail": red}[status]
        self._line(status, label, seconds, style(note) if note else "")
        return StepOutcome(ok is True, status == "warn", note, details, output, seconds)


def command_work(cmd: str, cwd: Path) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", bufsize=1,
                                start_new_session=True, **DOCKER_RUN_AS)
        assert proc.stdout is not None
        ACTIVE_PROCS.add(proc)
        try:
            for line in proc.stdout:
                emit(line.rstrip("\n").split("\r")[-1])
            rc = proc.wait()
        finally:
            ACTIVE_PROCS.discard(proc)
        if CANCEL.is_set():
            return False, "cancelled", []
        return rc == 0, "" if rc == 0 else f"exit code {rc}", []
    return work


GITHUB_REPO = __git__.rstrip("/").split("github.com/")[-1]
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
UPDATED_ENV = "TUGBOAT_JUST_UPDATED"
RELEASE_CHECK_INTERVAL = 3600


def parse_version(v: str) -> tuple[int, ...]:
    core = v.strip().lstrip("vV").split("-")[0].split("+")[0]
    nums = re.findall(r"\d+", core)
    return tuple(int(n) for n in nums) if nums else (0,)


def http_get(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"User-Agent": f"{__title__}/{__version__}",
                                "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_release(timeout: float = 5) -> dict:
    try:
        data = json.loads(http_get(f"{GITHUB_API}/repos/{GITHUB_REPO}/releases/latest", timeout))
    except HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"no releases published on github.com/{GITHUB_REPO} yet")
        if e.code == 403:
            raise RuntimeError("GitHub refused the request (rate limit?) - try again later")
        raise RuntimeError(f"GitHub returned HTTP {e.code}")
    except (URLError, OSError, HTTPException) as e:
        raise RuntimeError(f"could not reach GitHub: {getattr(e, 'reason', e)}")
    except ValueError:
        raise RuntimeError("unexpected answer from GitHub")
    if not isinstance(data, dict):
        raise RuntimeError("unexpected answer from GitHub")
    tag = data.get("tag_name") or ""
    return {
        "tag": tag,
        "version": parse_version(tag),
        "name": data.get("name") or tag,
        "notes": data.get("body") or "",
        "url": data.get("html_url") or f"{__git__}/releases",
        "published": (data.get("published_at") or "")[:10],
        "assets": {a.get("name", ""): a.get("browser_download_url", "") for a in data.get("assets", [])},
    }


def check_update(timeout: float = 5) -> tuple[dict | None, dict | None, str]:
    try:
        rel = latest_release(timeout)
    except RuntimeError as e:
        return None, None, str(e)
    newer = rel if rel["version"] > parse_version(__version__) else None
    return rel, newer, ""


def _conf_keys(text: str) -> list[str]:
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            keys.append(line.split(":", 1)[0].strip().lower())
    return keys


def install_release(rel: dict, dry_run: bool) -> bool:
    script = Path(__file__).resolve()
    assets = {k.lower(): v for k, v in rel["assets"].items()}
    url = (assets.get(script.name.lower()) or assets.get("tugboat.py")
           or f"{GITHUB_RAW}/{GITHUB_REPO}/{rel['tag']}/TugBoat.py")

    say(f"  Downloading {dim(url)}")
    try:
        raw = http_get(url, 30)
        text = raw.decode("utf-8-sig")
    except (URLError, OSError, HTTPException, UnicodeDecodeError) as e:
        say_error(f"Download failed: {getattr(e, 'reason', e)}")
        return False

    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        say_error("Downloaded file has no __version__ - not a TugBoat script, aborting")
        return False
    if parse_version(m.group(1)) != rel["version"]:
        say_error(f"Release is {rel['tag']} but the file says {m.group(1)} - aborting")
        return False
    try:
        compile(text, str(script), "exec")
    except SyntaxError as e:
        say_error(f"Downloaded file has a syntax error (line {e.lineno}) - aborting")
        return False
    say(f"  {green(SYM['ok'])} Verified {m.group(1)}")

    if dry_run:
        say(cyan(f"  {SYM['dry']} Dry run - would replace {script}"))
        return True

    st = script.stat()
    backup = script.with_name(f".{script.name}.{__version__}.bak")
    tmp = script.with_name(f".{script.name}.new")
    try:
        shutil.copy2(script, backup)
        tmp.write_bytes(raw)
        os.chmod(tmp, st.st_mode)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, script)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        say_error(f"Could not replace {script}: {e}")
        return False
    say(f"  {green(SYM['ok'])} Installed {bold(rel['tag'])}  {dim(f'(old version kept as {backup.name})')}")

    try:
        example = http_get(f"{GITHUB_RAW}/{GITHUB_REPO}/{rel['tag']}/TugBoat.conf", 10).decode("utf-8")
        local = (set(_conf_keys(CONFIG_FILE.read_text(encoding="utf-8")))
                 if CONFIG_FILE.is_file() else set())
        new_keys = [k for k in _conf_keys(example) if k not in local]
        if new_keys:
            say(yellow(f"  {SYM['warn']} New config settings (added to your config on the next run): "
                       f"{', '.join(new_keys)}"))
            say(dim(f"    see {__git__}/blob/{rel['tag']}/TugBoat.conf"))
    except (URLError, OSError, HTTPException, UnicodeDecodeError):
        pass
    return True


def print_release_notes(rel: dict, max_lines: int = 15) -> None:
    lines = [l.rstrip() for l in rel["notes"].strip().splitlines()]
    if not lines:
        return
    say(dim("  Release notes:"))
    for line in lines[:max_lines]:
        say(dim(f"    {line}"))
    if len(lines) > max_lines:
        say(dim(f"    ... more at {rel['url']}"))


def run_check_update() -> int:
    rel, newer, err = check_update(timeout=10)
    if err:
        say_error(f"Update check failed: {err}")
        return 2
    if newer:
        title = f"{__title__} {rel['tag']}"
        say(f"\n{yellow(SYM['warn'])} {bold(title)} is available "
            f"(you have {__version__}, released {rel['published']})")
        print_release_notes(rel)
        say(f"\n  Install with: {bold('sudo python3 ' + Path(__file__).name + ' --self-update')}\n")
        return 0
    say(f"\n{green(SYM['ok'])} {__title__} {__version__} is up to date (latest release: {rel['tag']})\n")
    return 0


def run_self_update(dry_run: bool) -> int:
    rel, newer, err = check_update(timeout=10)
    if err:
        say_error(f"Update check failed: {err}")
        return 2
    if not newer:
        say(f"\n{green(SYM['ok'])} {__title__} {__version__} is up to date (latest release: {rel['tag']})\n")
        return 0
    say(f"\n{bold(f'Updating {__title__}')} {__version__} {SYM['arrow']} {rel['tag']}")
    ok = install_release(rel, dry_run)
    if ok:
        print_release_notes(rel)
    say()
    return 0 if ok else 1


def startup_update_check(cfg: "Config", dry_run: bool, db: "StatusDB | None") -> None:
    if not (cfg.update_check or cfg.auto_update) or os.environ.get(UPDATED_ENV):
        if os.environ.get(UPDATED_ENV):
            say(green(f"{SYM['ok']} Updated to {__title__} {__version__}"))
        return
    cached = db.data.get("release_check") if db else None
    if isinstance(cached, dict) and is_fresh(cached.get("checked_at"), RELEASE_CHECK_INTERVAL):
        latest = str(cached.get("latest") or "")
        if parse_version(latest) > parse_version(__version__):
            say(yellow(f"{SYM['warn']} {__title__} {latest} is available (you have {__version__}) - "
                       f"run with --self-update to install"))
        return
    rel, newer, _ = check_update(timeout=3)
    if db:
        db.set_top("release_check", {"checked_at": now_iso(), "latest": rel["tag"] if rel else ""})
        try:
            db.save()
        except OSError:
            pass
    if not newer:
        return
    if cfg.auto_update and not dry_run:
        say(yellow(f"{SYM['warn']} {__title__} {newer['tag']} is available - auto_update is on, installing"))
        if install_release(newer, dry_run=False):
            os.environ[UPDATED_ENV] = "1"
            script = str(Path(__file__).resolve())
            os.execv(sys.executable, [sys.executable, script, *sys.argv[1:]])
        say(yellow("  Continuing with the current version"))
        return
    say(yellow(f"{SYM['warn']} {__title__} {newer['tag']} is available (you have {__version__}) - "
               f"run with --self-update to install"))


@dataclass
class Config:
    container_path: Path
    backup: bool
    backup_path: str
    backup_retention: int
    backup_large_mb: int
    backup_large_retention: int
    ignore_folders: set[str]
    require_root: bool
    status_file: Path
    health_wait: int
    docker_user: str
    update_check: bool
    auto_update: bool
    image_check: bool
    image_check_interval: int
    command_timeout: int
    manage_cron: bool
    install_dependencies: bool

    def backup_root(self, stack: str) -> Path:
        return Path(self.backup_path.replace(STACK_PLACEHOLDER, stack))


def _parse_bool(value: str, key: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "yes", "1", "on"):
        return True
    if v in ("false", "no", "0", "off"):
        return False
    raise ValueError(f"'{key}' must be true/false, got '{value}'")


def _parse_int(value: str, key: str) -> int:
    digits = ""
    for ch in value.strip():
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        raise ValueError(f"'{key}' must be a whole number, got '{value}'")
    return int(digits)


def _strip_comment(value: str) -> str:
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i]
    return value


CONFIG_KEY_RE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*:")


def parse_config_text(text: str, name: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    for lineno, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"{name} line {lineno}: expected 'key: value'")
        key, value = stripped.split(":", 1)
        value = _strip_comment(value).strip().strip('"').strip("'")
        raw[key.strip().lower()] = value
    return raw


def config_default(key: str, raw: dict[str, str]) -> str:
    container = raw.get("container_path") or CONFIG_DEFAULTS["container_path"]
    return CONFIG_DEFAULTS[key].replace("{container_path}", container)


def sync_config(path: Path, write: bool = True) -> tuple[list[str], list[str]]:
    exists = path.is_file()
    text = path.read_bytes().decode("utf-8") if exists else ""
    raw = parse_config_text(text, path.name)
    newline = "\r\n" if "\r\n" in text else "\n"
    present: set[str] = set()
    removed: list[str] = []
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        match = None if line.lstrip("\ufeff").lstrip().startswith("#") else CONFIG_KEY_RE.match(line.lstrip("\ufeff"))
        key = match.group(1).lower() if match else ""
        if key in OBSOLETE_CONFIG_KEYS:
            removed.append(key)
            continue
        if key:
            present.add(key)
        kept.append(line)
    missing = [key for key in CONFIG_DEFAULTS if key not in present]
    if not missing and not removed:
        return [], []
    out = "".join(kept)
    if missing and out and not out.endswith(("\n", "\r")):
        out += newline
    out += "".join(f"{key}: {config_default(key, raw)}".rstrip() + newline for key in missing)
    if write:
        tmp = path.with_name(f".{path.name}.new")
        try:
            if exists:
                path.with_name(f".{path.name}.bak").write_bytes(text.encode("utf-8"))
            tmp.write_bytes(out.encode("utf-8"))
            if exists:
                st = path.stat()
                os.chmod(tmp, st.st_mode)
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except PermissionError:
                    pass
            else:
                os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    return missing, removed


def load_config(path: Path) -> Config:
    raw = parse_config_text(path.read_bytes().decode("utf-8"), path.name) if path.is_file() else {}

    def get(key: str) -> str:
        value = raw.get(key)
        if value is None or (not value and key == "container_path"):
            return config_default(key, raw)
        return value

    def resolve(value: str | Path) -> Path:
        return path.parent / Path(value).expanduser()

    return Config(
        container_path=resolve(get("container_path")),
        backup=_parse_bool(get("backup"), "backup"),
        backup_path=str(resolve(get("backup_path") or config_default("backup_path", raw))),
        backup_retention=_parse_int(get("backup_retention"), "backup_retention"),
        backup_large_mb=_parse_int(get("backup_large_mb"), "backup_large_mb"),
        backup_large_retention=_parse_int(get("backup_large_retention"), "backup_large_retention"),
        ignore_folders={n.strip().strip("/") for n in get("ignore_folders").split(",") if n.strip()},
        require_root=_parse_bool(get("require_root"), "require_root"),
        docker_user=get("docker_user").strip(),
        update_check=_parse_bool(get("update_check"), "update_check"),
        auto_update=_parse_bool(get("auto_update"), "auto_update"),
        status_file=resolve(get("status_file") or config_default("status_file", raw)),
        health_wait=_parse_int(get("health_wait"), "health_wait"),
        image_check=_parse_bool(get("image_check"), "image_check"),
        image_check_interval=_parse_int(get("image_check_interval"), "image_check_interval"),
        command_timeout=_parse_int(get("command_timeout"), "command_timeout"),
        manage_cron=_parse_bool(get("manage_cron"), "manage_cron"),
        install_dependencies=_parse_bool(get("install_dependencies"), "install_dependencies"),
    )


DOCKER_RUN_AS: dict = {}


def setup_docker_user(user: str) -> str | None:
    DOCKER_RUN_AS.clear()
    if not user:
        return None
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        raise ValueError(f"docker_user '{user}' does not exist on this system")

    if os.geteuid() != 0:
        if os.geteuid() == pw.pw_uid:
            return None
        raise PermissionError(f"Running docker as '{user}' needs root - run TugBoat with sudo")
    if pw.pw_uid == 0:
        return None

    groups = os.getgrouplist(user, pw.pw_gid)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUDO_")}
    env.update(HOME=pw.pw_dir, USER=user, LOGNAME=user)
    DOCKER_RUN_AS.update(user=pw.pw_uid, group=pw.pw_gid, extra_groups=groups, env=env)

    try:
        docker_gid = grp.getgrnam("docker").gr_gid
        if docker_gid not in groups:
            return (f"'{user}' is not in the docker group - docker commands will probably fail "
                    f"(fix: sudo usermod -aG docker {user})")
    except KeyError:
        pass
    return None


def ensure_root(non_interactive: bool = False) -> None:
    if os.geteuid() == 0:
        return
    if shutil.which("sudo") is None:
        raise PermissionError("TugBoat needs root to back up container data, and sudo was not found.")
    say(yellow("Not running as root - restarting with sudo"))
    sudo = ["sudo", "-n"] if non_interactive else ["sudo"]
    os.execvp("sudo", [*sudo, sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])


def acquire_run_lock():
    handle = open(CONFIG_FILE, "rb")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Another TugBoat run is changing stacks right now - try again when it is done")
    return handle


PACKAGES = {
    "docker": {"apt-get": [["docker.io"]], "dnf": [["moby-engine"], ["docker-ce"]], "pacman": [["docker"]]},
    "compose": {"apt-get": [["docker-compose-v2"], ["docker-compose-plugin"], ["docker-compose"]],
                "dnf": [["docker-compose"], ["docker-compose-plugin"]], "pacman": [["docker-compose"]]},
    "cron": {"apt-get": [["cron"]], "dnf": [["cronie"]], "pacman": [["cronie"]]},
}
SERVICES = {
    "docker": {"apt-get": "docker", "dnf": "docker", "pacman": "docker"},
    "cron": {"apt-get": "cron", "dnf": "crond", "pacman": "cronie"},
}
DEPS_FAILED = SCRIPT_DIR / ".TugBoat.deps.failed"
DEPS_RETRY_SECONDS = 3600
DEPS_TIMEOUT = 900


def package_manager() -> str | None:
    return next((tool for tool in ("apt-get", "dnf", "pacman") if shutil.which(tool)), None)


def dependency_present(name: str) -> bool:
    if name == "docker":
        return shutil.which("docker") is not None
    if name == "cron":
        return shutil.which("crontab") is not None
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def missing_dependencies(need_docker: bool, need_cron: bool) -> list[str]:
    missing: list[str] = []
    if need_docker:
        if not dependency_present("docker"):
            missing += ["docker", "compose"]
        elif not dependency_present("compose"):
            missing.append("compose")
    if need_cron and not dependency_present("cron"):
        missing.append("cron")
    return missing


def install_command(tool: str, packages: list[str]) -> list[str]:
    names = " ".join(shlex.quote(name) for name in packages)
    if tool == "apt-get":
        return [f"DEBIAN_FRONTEND=noninteractive apt-get install -y {names}"]
    if tool == "dnf":
        return [f"dnf install -y {names}"]
    return [f"pacman -S --noconfirm --needed {names}", f"pacman -Sy --noconfirm --needed {names}"]


def install_dependencies(missing: list[str], tool: str, verbose: bool, dry_run: bool) -> list[str]:
    ui = Runner(verbose=verbose, dry_run=dry_run)
    if dry_run:
        for name in missing:
            ui.dry(f"Install {name}", f"would install {' '.join(PACKAGES[name][tool][0])} with {tool}")
        return []
    if tool == "apt-get":
        ui.step("Update package list", command_work("apt-get update -qq", SCRIPT_DIR), warn_only=True,
                timeout=DEPS_TIMEOUT)
    remaining: list[str] = []
    for name in missing:
        if dependency_present(name):
            continue
        for packages in PACKAGES[name][tool]:
            for command in install_command(tool, packages):
                if ui.step(f"Install {name}", command_work(command, SCRIPT_DIR), timeout=DEPS_TIMEOUT).ok:
                    break
            if dependency_present(name):
                break
        if not dependency_present(name):
            remaining.append(name)
            continue
        service = SERVICES.get(name, {}).get(tool)
        if service and shutil.which("systemctl"):
            ui.step(f"Start {service}", command_work(f"systemctl enable --now {service}", SCRIPT_DIR),
                    warn_only=True, timeout=120)
    return remaining


def deps_recently_failed() -> bool:
    try:
        return time.time() - DEPS_FAILED.stat().st_mtime < DEPS_RETRY_SECONDS
    except OSError:
        return False


def handle_dependencies(missing: list[str], cfg: Config, args: argparse.Namespace) -> list[str]:
    names = ", ".join(missing)
    tool = package_manager()
    reason = ""
    if not cfg.install_dependencies:
        reason = "install_dependencies is off"
    elif not args.dry_run and os.geteuid() != 0:
        reason = "installing needs root"
    elif tool is None:
        reason = "no supported package manager (apt, dnf or pacman)"
    elif not args.dry_run and deps_recently_failed():
        reason = "the last install attempt failed less than an hour ago"
    if reason:
        say(yellow(f"{SYM['warn']} Missing: {names} ({reason})"))
        return missing
    say(f"\n{bold('Installing missing dependencies')}: {names}")
    remaining = install_dependencies(missing, tool, args.verbose, args.dry_run)
    if not args.dry_run:
        try:
            if remaining:
                DEPS_FAILED.write_text(", ".join(remaining) + "\n", encoding="utf-8")
            else:
                DEPS_FAILED.unlink(missing_ok=True)
        except OSError:
            pass
    return remaining


def action_running() -> bool:
    try:
        handle = open(CONFIG_FILE, "rb")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        handle.close()
    return False


def acquire_healthcheck_lock():
    handle = open(Path(__file__).resolve(), "rb")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


SCRIPT_NAME = Path(__file__).resolve().name
CRON_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
CRON_SCHEDULE_RE = re.compile(r"^\s*(@\w+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(\S.*)$")


def cron_schedule(minutes: int) -> str:
    if minutes == 1:
        return "* * * * *"
    if 2 <= minutes <= 59:
        return f"*/{minutes} * * * *"
    if 60 <= minutes <= 1440 and minutes % 60 == 0:
        hours = minutes // 60
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    raise ValueError("CRON_EVERY_MINUTES must be 1-59 minutes or a whole number of hours "
                     "(60, 120 ... 1440)")


def fmt_interval(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    return "every minute" if minutes == 1 else f"every {minutes} minutes"


def cron_command() -> str:
    script = Path(__file__).resolve()
    return (f"PATH={CRON_PATH} {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            "--healthcheck >/dev/null 2>&1")


CRON_ENV_RE = re.compile(r"^[A-Za-z_]\w*=[\w.,:/@%+=-]*$")
CRON_WORD_RE = re.compile(r"^[\w.,:/@%+=-]+$")
CRON_REDIRECT_RE = re.compile(r"^(?:\d*>>?|&>>?)(?:&\d+|[\w./-]*)$")
CRON_INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|env)$")
CRON_BACKUP = SCRIPT_DIR / ".TugBoat.crontab.bak"


def is_tugboat_cron(line: str) -> bool:
    text = line.strip()
    if not text or text.startswith("#"):
        return False
    match = CRON_SCHEDULE_RE.match(text)
    if not match:
        return False
    try:
        tokens = shlex.split(match.group(2))
    except ValueError:
        return False
    found = next((i for i, token in enumerate(tokens) if os.path.basename(token) == SCRIPT_NAME), None)
    if found is None or found + 1 >= len(tokens) or tokens[found + 1] != "--healthcheck":
        return False
    for token in tokens[:found]:
        if not (CRON_ENV_RE.match(token) or CRON_INTERPRETER_RE.match(os.path.basename(token))):
            return False
    if not all(CRON_WORD_RE.match(t) or CRON_REDIRECT_RE.match(t) for t in tokens[found + 2:]):
        return False
    target = Path(tokens[found])
    return not target.exists() or target.resolve() == Path(__file__).resolve()


def read_crontab() -> str:
    try:
        p = subprocess.run(["crontab", "-l"], capture_output=True, encoding="utf-8", errors="surrogateescape",
                           stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"could not run crontab: {e}")
    if p.returncode == 0:
        return p.stdout
    if "no crontab" in p.stderr.lower():
        return ""
    raise RuntimeError(_last_line(p.stderr, f"crontab -l exit code {p.returncode}"))


def write_crontab(text: str) -> None:
    args = ["crontab", "-"] if text.strip() else ["crontab", "-r"]
    try:
        p = subprocess.run(args, input=text, capture_output=True, encoding="utf-8", errors="surrogateescape",
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"could not run crontab: {e}")
    if p.returncode != 0:
        raise RuntimeError(_last_line(p.stderr, f"{' '.join(args)} exit code {p.returncode}"))


def apply_crontab(current: str, updated: str) -> None:
    if current.strip():
        try:
            CRON_BACKUP.write_bytes(current.encode("utf-8", "surrogateescape"))
        except OSError as e:
            raise RuntimeError(f"could not back up the crontab to {CRON_BACKUP} ({e.strerror or e}) - "
                               "nothing was changed")
    if read_crontab() != current:
        raise RuntimeError("the crontab changed while TugBoat was reading it - nothing was changed")
    write_crontab(updated)


def cron_lines(text: str) -> list[str]:
    if not text:
        return []
    return (text[:-1] if text.endswith("\n") else text).split("\n")


def plan_cron(current: str, line: str | None) -> tuple[str, str]:
    if line is not None and not is_tugboat_cron(line):
        raise RuntimeError("the cron line TugBoat would write is not recognised as its own - "
                           "nothing was changed")
    lines = cron_lines(current)
    others = [text for text in lines if not is_tugboat_cron(text)]
    hits = [i for i, text in enumerate(lines) if is_tugboat_cron(text)]
    old = lines[hits[0]] if hits else ""
    if line is None:
        result = others
    elif not hits:
        result = lines + [line]
    else:
        result = [line if i == hits[0] else text for i, text in enumerate(lines) if i not in hits[1:]]
    if [text for text in result if not is_tugboat_cron(text)] != others:
        raise RuntimeError("safety check failed: the change would touch lines that are not TugBoat's - "
                           "nothing was changed")
    return ("\n".join(result) + "\n" if result else ""), old


def ensure_cron(cfg: Config, dry_run: bool) -> tuple[str, str]:
    try:
        schedule = cron_schedule(CRON_EVERY_MINUTES)
    except ValueError as e:
        return "error", str(e)
    if shutil.which("crontab") is None:
        return "error", "crontab not found - install cron first (Debian/Ubuntu: apt install cron)"
    try:
        current = read_crontab()
        updated, old = plan_cron(current, f"{schedule} {cron_command()}")
        if updated.rstrip("\n") == current.rstrip("\n"):
            return "same", ""
        if not dry_run:
            apply_crontab(current, updated)
    except RuntimeError as e:
        return "error", f"could not update the crontab: {e}"
    return ("updated" if old else "added"), old


def describe_cron(cfg: Config, state: str, old: str, dry_run: bool = False) -> str:
    every = fmt_interval(CRON_EVERY_MINUTES)
    if state == "error":
        return yellow(f"{SYM['warn']} Cron job not checked: {old} (manage_cron: false stops this check)")
    if state == "same":
        return f"{green(SYM['ok'])} Health check already runs {every}"
    verb = {"added": "add", "updated": "update"}[state]
    if dry_run:
        return cyan(f"{SYM['dry']} Dry run - would {verb} the cron job: health check {every}")
    if state == "added":
        return f"{green(SYM['ok'])} Cron job added: health check {every}"
    match = CRON_SCHEDULE_RE.match(old)
    before = " ".join(match.group(1).split()) if match else ""
    now = cron_schedule(CRON_EVERY_MINUTES)
    if before and before != now:
        return f"{green(SYM['ok'])} Cron job updated: {dim(before)} {SYM['arrow']} {now}  ({every})"
    return f"{green(SYM['ok'])} Cron job updated to the current command  ({every})"


def run_install(cfg: Config, dry_run: bool) -> int:
    if not cfg.container_path.is_dir():
        say_error(f"container_path does not exist: {cfg.container_path} - "
                  f"edit {CONFIG_FILE} and run --install again")
        return 2
    state, old = ensure_cron(cfg, dry_run)
    say()
    if state == "error":
        say_error(f"Could not set up the cron job: {old}")
        return 2
    say(describe_cron(cfg, state, old, dry_run))
    say(dim("  Change CRON_EVERY_MINUTES at the top of TugBoat.py; TugBoat keeps the cron job in line on every run"))
    return 0


def run_uninstall(cfg: Config, dry_run: bool) -> int:
    if shutil.which("crontab") is None:
        say_error("crontab not found")
        return 2
    try:
        current = read_crontab()
        updated, old = plan_cron(current, None)
        say()
        if not old:
            say(f"{green(SYM['ok'])} No TugBoat cron job found")
            return 0
        if dry_run:
            say(cyan(f"{SYM['dry']} Dry run - would remove the TugBoat cron job"))
            return 0
        apply_crontab(current, updated)
        say(f"{green(SYM['ok'])} Cron job removed")
    except RuntimeError as e:
        say_error(f"Could not update the crontab: {e}")
        return 2
    if cfg.manage_cron:
        say(yellow(f"{SYM['warn']} manage_cron is on, so the next run adds it again - "
                   f"set manage_cron: false in {CONFIG_FILE.name} to keep it off"))
    return 0


def find_stacks(root: Path, ignore: set[str] = frozenset()) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"container_path does not exist: {root}")
    return sorted(
        (d for d in root.iterdir()
         if d.is_dir() and not d.name.startswith(".") and d.name not in ignore
         and any((d / f).is_file() for f in COMPOSE_FILES)),
        key=lambda d: d.name.lower(),
    )


def parse_selection(text: str, count: int) -> list[int] | None:
    text = text.strip().lower()
    if text in ("a", "all", "*"):
        return list(range(count))
    picked: set[int] = set()
    for part in text.replace(",", " ").split():
        try:
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
                picked.update(range(lo, hi + 1))
            else:
                picked.add(int(part))
        except ValueError:
            return None
    if not picked or any(n < 1 or n > count for n in picked):
        return None
    return sorted(n - 1 for n in picked)


def pick_stacks_by_name(names: list[str], stacks: list[Path], cfg: Config) -> list[Path]:
    by_name = {s.name: s for s in stacks}
    by_lower = {s.name.lower(): s for s in stacks}
    picked: list[Path] = []
    for raw in names:
        name = raw.strip().strip("/")
        stack = by_name.get(name) or by_lower.get(name.lower())
        if stack is None:
            if name in cfg.ignore_folders:
                raise ValueError(f"Stack '{name}' is in ignore_folders in TugBoat.conf")
            folder = cfg.container_path / name
            if folder.is_dir():
                raise ValueError(f"'{name}' has no compose file ({', '.join(COMPOSE_FILES)})")
            raise ValueError(f"No stack named '{name}'. Available: {', '.join(by_name)}")
        if stack not in picked:
            picked.append(stack)
    return picked


def _prompt(text: str = "> ") -> str | None:
    try:
        return input(cyan(text)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        say()
        return None


def _menu_item(key: str, text: str) -> None:
    say(f"  {cyan(f'{key:>2}')}  {text}")


def ask_for_action() -> str | None:
    say("\n" + bold("What do you want to do?"))
    _menu_item("1", f"Update  {dim('stop → backup → pull → start' if UNICODE else 'stop, backup, pull, start')}")
    _menu_item("2", "Start")
    _menu_item("3", "Stop")
    _menu_item("4", "Health check")
    _menu_item("5", f"Restart  {dim('stop → start, no pull' if UNICODE else 'stop, start, no pull')}")
    _menu_item("6", f"Rollback  {dim('restore a backup of one stack')}")
    _menu_item("q", dim("Quit"))
    while True:
        answer = _prompt()
        if answer is None or answer in ("q", "quit", "exit"):
            return None
        if answer in ("1", "u", "update"):
            return "update"
        if answer in ("2", "start"):
            return "start"
        if answer in ("3", "stop"):
            return "stop"
        if answer in ("4", "h", "health", "healthcheck"):
            return "healthcheck"
        if answer in ("5", "r", "restart"):
            return "restart"
        if answer in ("6", "rollback"):
            return "rollback"
        say(yellow("  Invalid choice, try again."))


def ask_for_stacks(stacks: list[Path], action: str, snaps: dict[str, dict] | None = None,
                   db: "StatusDB | None" = None) -> list[Path]:
    title = "check" if action == "healthcheck" else action
    say("\n" + bold(f"Stacks to {title.upper()}"))
    width = max(len(s.name) for s in stacks)
    for i, s in enumerate(stacks, 1):
        snap = (snaps or {}).get(s.name)
        updates = db.stack(s.name).get("updates_available") if db else 0
        mark = yellow(f"  {SYM['up']} {fmt_updates(updates)}") if updates else ""
        if snap:
            health = HEALTH_STYLE[snap["health"]][1](f"{snap['health']:<9}")
            _menu_item(str(i), f"{s.name:<{width}}  {health}{mark}".rstrip())
        else:
            _menu_item(str(i), f"{s.name:<{width}}{mark}".rstrip())
    _menu_item("a", bold("ALL stacks"))
    _menu_item("q", dim("Quit"))
    say(dim("  e.g. 1,3 or 2-4"))
    while True:
        answer = _prompt()
        if answer is None or answer in ("q", "quit", "exit"):
            return []
        idx = parse_selection(answer, len(stacks))
        if idx is None:
            say(yellow("  Invalid selection, try again."))
            continue
        if action in ("stop", "restart") and len(idx) == len(stacks) and len(stacks) > 1:
            confirm = _prompt(f"{action.capitalize()} ALL {len(stacks)} stacks? [y/N] ")
            if confirm not in ("y", "yes"):
                say(yellow("  Cancelled - select again or 'q' to quit."))
                continue
        return [stacks[i] for i in idx]


def new_backup_dest(stack: Path, cfg: Config) -> Path:
    dest = cfg.backup_root(stack.name) / datetime.now().strftime(TIMESTAMP_FORMAT)
    while dest.exists():
        time.sleep(0.25)
        dest = cfg.backup_root(stack.name) / datetime.now().strftime(TIMESTAMP_FORMAT)
    return dest


def backup_inside_stack(stack: Path, cfg: Config) -> bool:
    try:
        cfg.backup_root(stack.name).resolve().relative_to(stack.resolve())
        return True
    except ValueError:
        return False


def _is_special(path: str) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def _copy_file(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    if CANCEL.is_set():
        raise InterruptedError("cancelled")
    shutil.copyfile(src, dst, follow_symlinks=follow_symlinks)
    st = os.lstat(src)
    try:
        os.chown(dst, st.st_uid, st.st_gid, follow_symlinks=False)
    except PermissionError:
        pass
    shutil.copystat(src, dst, follow_symlinks=follow_symlinks)
    return dst


def _copy_dir_owners(src: Path, dest: Path) -> None:
    for root, dirs, files in os.walk(dest):
        origin = os.path.normpath(os.path.join(src, os.path.relpath(root, dest)))
        names = ["."] + dirs + [f for f in files if os.path.islink(os.path.join(root, f))]
        for name in names:
            try:
                st = os.lstat(os.path.join(origin, name))
                os.chown(os.path.join(root, name), st.st_uid, st.st_gid, follow_symlinks=False)
            except OSError:
                pass


def backup_work(stack: Path, dest: Path) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        emit(f"copying {stack} -> {dest}")
        skipped: list[str] = []

        def ignore(folder: str, names: list[str]) -> list[str]:
            special = [n for n in names if _is_special(os.path.join(folder, n))]
            for n in special:
                skipped.append(os.path.join(folder, n))
                emit(f"skipping socket/pipe/device {os.path.join(folder, n)}")
            return special

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stack, dest, symlinks=True, ignore=ignore, copy_function=_copy_file)
            _copy_dir_owners(stack, dest)
            try:
                shown = str(dest.relative_to(stack.parent))
            except ValueError:
                shown = str(dest)
            extra = f" ({len(skipped)} socket/pipe file(s) skipped)" if skipped else ""
            return True, f"{SYM['arrow']} {shown}{extra}", []
        except shutil.Error as e:
            failures = e.args[0] if e.args and isinstance(e.args[0], list) else []
            details = [f"{src}: {str(reason).split(']')[-1].split(':')[0].strip()}"
                       for src, _, reason in failures[:10]]
            if len(failures) > 10:
                details.append(f"... and {len(failures) - 10} more")
            note = "cancelled" if CANCEL.is_set() else f"{len(failures)} file(s) could not be copied"
        except OSError as e:
            details, note = [], str(e)
        shutil.rmtree(dest, ignore_errors=True)
        return False, note, details
    return work


def list_backups(stack: Path, cfg: Config) -> list[tuple[datetime, Path]]:
    root = cfg.backup_root(stack.name)
    backups: list[tuple[datetime, Path]] = []
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.is_dir():
                    backups.append((datetime.strptime(d.name, TIMESTAMP_FORMAT), d))
            except ValueError:
                continue
    backups.sort(reverse=True)
    return backups


def tree_size_over(root: Path, limit: int) -> bool:
    total = 0
    for folder, _, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            total += st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
            if total > limit:
                return True
    return False


def prune_work(stack: Path, cfg: Config) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        backups = list_backups(stack, cfg)
        keep, why = cfg.backup_retention, ""
        if cfg.backup_large_mb > 0 and backups and tree_size_over(backups[0][1], cfg.backup_large_mb * 1024 * 1024):
            keep, why = max(1, cfg.backup_large_retention), f", backup is over {cfg.backup_large_mb} MB"
        old = backups[keep:] if keep > 0 else []
        failed: list[str] = []
        for _, d in old:
            emit(f"removing {d.name}")
            try:
                shutil.rmtree(d)
            except OSError as e:
                failed.append(f"{d}: {e}")
        kept = len(backups) - len(old)
        if failed:
            return False, f"{len(failed)} old backup(s) could not be removed", failed
        removed = f"removed {len(old)}, " if old else ""
        return True, f"{removed}keeping {kept}/{keep if keep > 0 else 'all'}{why}", []
    return work


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def age_seconds(iso: object) -> float | None:
    try:
        then = datetime.fromisoformat(str(iso))
        return (datetime.now().astimezone() - then).total_seconds()
    except (ValueError, TypeError):
        return None


def is_fresh(iso: object, max_age: float) -> bool:
    age = age_seconds(iso)
    return age is not None and 0 <= age < max_age


def docker(args: list[str], cwd: Path | None = None, timeout: float = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["docker", *args], cwd=cwd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, **DOCKER_RUN_AS)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def _last_line(text: str, fallback: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1].strip() if lines else fallback


def compose_ps(stack: Path) -> tuple[list[dict] | None, str]:
    rc, out, err = docker(["compose", "ps", "--all", "--format", "json"], cwd=stack)
    if rc != 0:
        return None, _last_line(err or out, f"docker compose ps exit code {rc}")
    text = out.strip()
    if not text:
        return [], ""
    try:
        raw = json.loads(text) if text.startswith("[") else [json.loads(l) for l in text.splitlines() if l.strip()]
    except json.JSONDecodeError as e:
        return None, f"could not parse docker compose ps output: {e}"
    return [{
        "name": c.get("Name", ""),
        "service": c.get("Service", ""),
        "state": (c.get("State") or "").lower(),
        "health": (c.get("Health") or "").lower(),
        "exit_code": c.get("ExitCode"),
        "image": c.get("Image", ""),
        "status": c.get("Status", ""),
    } for c in raw], ""


def summarize_health(containers: list[dict]) -> tuple[str, str, list[str]]:
    if not containers:
        return "stopped", "no containers", []
    problems: list[str] = []
    for c in containers:
        name, state, health = c["name"] or c["service"], c["state"], c["health"]
        if state in ("restarting", "dead", "paused"):
            problems.append(f"{name}: {state}")
        elif health == "unhealthy":
            problems.append(f"{name}: unhealthy")
        elif state == "exited" and c["exit_code"] not in (0, None):
            problems.append(f"{name}: exited with code {c['exit_code']}")
        elif state == "created":
            problems.append(f"{name}: created but not started")
    running = [c for c in containers if c["state"] == "running"]
    healthy = [c for c in containers if c["health"] == "healthy"]
    starting = [c for c in containers if c["health"] == "starting"]

    summary = f"{len(running)}/{len(containers)} running"
    if healthy:
        summary += f", {len(healthy)} healthy"
    if starting:
        summary += f", {len(starting)} starting"

    if problems:
        return "unhealthy", summary, problems
    if not running:
        return "stopped", summary, []
    if starting:
        return "starting", summary, []
    return "healthy", summary, []


def check_health(stack: Path) -> dict:
    containers, err = compose_ps(stack)
    if containers is None:
        return {"health": "unknown", "summary": "could not read status", "problems": [err],
                "containers": [], "checked_at": now_iso()}
    health, summary, problems = summarize_health(containers)
    return {"health": health, "summary": summary, "problems": problems,
            "containers": containers, "checked_at": now_iso()}


def problem_logs(stack: Path, snapshot: dict, tail: int = 15) -> list[str]:
    bad = {c["service"] for c in snapshot["containers"]
           if c["service"] and any(p.startswith((c["name"] or c["service"]) + ":") for p in snapshot["problems"])}
    lines: list[str] = []
    for service in sorted(bad):
        rc, out, err = docker(["compose", "logs", "--no-color", "--tail", str(tail), service],
                              cwd=stack, timeout=30)
        if rc == -1:
            lines.append(f"-- logs: {service}: {err}")
            continue
        lines.append(f"-- logs: {service} (last {tail} lines) --")
        lines += [l for l in (out + err).splitlines() if l.strip()]
    return lines


def health_work(stack: Path, cfg: Config, res: StackResult) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool | str, str, list[str]]:
        deadline = time.monotonic() + cfg.health_wait
        last = ""
        while True:
            snap = check_health(stack)
            if snap["summary"] != last:
                emit(snap["summary"])
                last = snap["summary"]
            if snap["health"] != "starting" or time.monotonic() >= deadline:
                break
            if CANCEL.wait(3):
                break
        res.health = snap
        h = snap["health"]
        if h == "healthy":
            return True, snap["summary"], []
        if h == "starting":
            return "warn", f"still starting after {cfg.health_wait}s ({snap['summary']})", []
        if h == "stopped":
            return False, f"not running ({snap['summary']})", []
        if h == "unknown":
            return False, "could not read status", snap["problems"]
        for line in problem_logs(stack, snap):
            emit(line)
        return False, f"unhealthy ({snap['summary']})", snap["problems"]
    return work


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REPO_PART = r"[a-z0-9]+(?:(?:\.|_{1,2}|-+)[a-z0-9]+)*"
REPO_RE = re.compile(rf"{_REPO_PART}(?:/{_REPO_PART})*")
TAG_RE = re.compile(r"\w[\w.-]{0,127}")
DOCKER_HUB = "docker.io"
DOCKER_HUB_ALIASES = (DOCKER_HUB, "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com")
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
REGISTRY_TIMEOUT = 10
IMAGE_OUTDATED = ("update_available", "not_pulled")


REGISTRY_MAX_BYTES = 8 * 1024 * 1024
VERSION_LABEL = "org.opencontainers.image.version"
SOURCE_LABELS = ("org.opencontainers.image.source", "org.opencontainers.image.url")


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repo: str
    tag: str
    digest: str


def parse_image_ref(image: str) -> ImageRef | None:
    name, _, digest = image.strip().partition("@")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first.lower(), rest
    else:
        registry, path = DOCKER_HUB, name
    path, colon, tag = path.partition(":")
    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB
        if "/" not in path:
            path = "library/" + path
    if not REPO_RE.fullmatch(path) or (colon and not TAG_RE.fullmatch(tag)):
        return None
    if digest and not DIGEST_RE.fullmatch(digest):
        return None
    return ImageRef(registry, path, tag or ("" if digest else "latest"), digest)


def _registry_of(key: str) -> str:
    host = re.sub(r"^[a-z]+://", "", key.strip().lower()).split("/")[0]
    return DOCKER_HUB if host in DOCKER_HUB_ALIASES else host


@functools.lru_cache(maxsize=None)
def docker_cli_config() -> dict:
    home = DOCKER_RUN_AS.get("env", {}).get("HOME") or Path.home()
    folder = os.environ.get("DOCKER_CONFIG") or Path(home) / ".docker"
    try:
        data = json.loads((Path(folder) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _credential_helper(helper: str, server: str) -> tuple[str, str] | None:
    if not re.fullmatch(r"[\w.-]+", helper):
        return None
    try:
        p = subprocess.run([f"docker-credential-{helper}", "get"], input=server, capture_output=True,
                           text=True, timeout=10, **DOCKER_RUN_AS)
        data = json.loads(p.stdout)
        user, secret = data.get("Username"), data.get("Secret")
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None
    return (user, secret) if user and secret and user != "<token>" else None


def registry_credentials(registry: str) -> tuple[str, str] | None:
    conf = docker_cli_config()
    auths = conf.get("auths") if isinstance(conf.get("auths"), dict) else {}
    helpers = conf.get("credHelpers") if isinstance(conf.get("credHelpers"), dict) else {}
    helper = next((h for k, h in helpers.items() if _registry_of(k) == registry), None) or conf.get("credsStore")
    servers = [k for k in auths if _registry_of(k) == registry]
    for key in servers:
        entry = auths[key]
        if isinstance(entry, dict) and entry.get("auth"):
            try:
                user, _, password = base64.b64decode(entry["auth"]).decode("utf-8").partition(":")
            except ValueError:
                continue
            if user and password:
                return user, password
    if isinstance(helper, str):
        for server in servers or ["https://index.docker.io/v1/" if registry == DOCKER_HUB else registry]:
            creds = _credential_helper(helper, server)
            if creds:
                return creds
    return None


class _DropAuthOnNewHost(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
        return new


_OPENER = build_opener(_DropAuthOnNewHost)


def _http_open(url: str, timeout: float, method: str = "GET", headers: dict | None = None):
    req = Request(url, method=method, headers={"User-Agent": f"{__title__}/{__version__}", **(headers or {})})
    return _OPENER.open(req, timeout=timeout)


def _registry_auth(challenge: str, ref: ImageRef, timeout: float) -> str:
    scheme, _, rest = challenge.strip().partition(" ")
    params = dict(re.findall(r'(\w+)="([^"]*)"', rest))
    creds = registry_credentials(ref.registry)
    basic = "Basic " + base64.b64encode(":".join(creds).encode()).decode() if creds else ""
    if scheme.lower() == "basic":
        if not basic:
            raise RegistryError("login required (docker login)")
        return basic
    realm = params.get("realm", "")
    if scheme.lower() != "bearer" or not realm.startswith(("https://", "http://")):
        raise RegistryError("registry uses an authentication method TugBoat does not know")
    query = {"scope": params.get("scope") or f"repository:{ref.repo}:pull"}
    if params.get("service"):
        query["service"] = params["service"]
    url = realm + ("&" if "?" in realm else "?") + urlencode(query)
    attempts = [{"Authorization": basic}, {}] if basic and realm.startswith("https://") else [{}]
    for i, headers in enumerate(attempts):
        try:
            with _http_open(url, timeout, headers=headers) as r:
                data = json.loads(r.read())
            token = data.get("token") or data.get("access_token") if isinstance(data, dict) else None
            if token:
                return f"Bearer {token}"
            raise RegistryError("registry gave no access token")
        except HTTPError as e:
            if e.code not in (401, 403) or i == len(attempts) - 1:
                raise
    raise RegistryError("registry gave no access token")


_REGISTRY_TOKENS: dict[tuple[str, str], str] = {}
_REGISTRY_BASE: dict[str, str] = {}


def _manifest_digest(url: str, ref: ImageRef, timeout: float) -> str:
    key = (ref.registry, ref.repo)
    headers = {"Accept": MANIFEST_TYPES}
    if key in _REGISTRY_TOKENS:
        headers["Authorization"] = _REGISTRY_TOKENS[key]
    authed = False
    methods = ["HEAD", "GET"]
    while methods:
        try:
            with _http_open(url, timeout, methods[0], headers) as r:
                digest = r.headers.get("Docker-Content-Digest") or ""
                if DIGEST_RE.fullmatch(digest):
                    return digest
                if methods[0] == "GET":
                    return "sha256:" + hashlib.sha256(r.read()).hexdigest()
        except HTTPError as e:
            if e.code == 401 and not authed:
                authed = True
                headers["Authorization"] = _registry_auth(e.headers.get("WWW-Authenticate") or "", ref, timeout)
                _REGISTRY_TOKENS[key] = headers["Authorization"]
                continue
            if methods[0] == "GET" or e.code not in (400, 405, 501):
                raise
        methods.pop(0)
    raise RegistryError("registry did not return a digest")


def remote_digest(ref: ImageRef, timeout: float = REGISTRY_TIMEOUT) -> str:
    host = "registry-1.docker.io" if ref.registry == DOCKER_HUB else ref.registry
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    problem = "no answer"
    for scheme in ("https", "http") if local else ("https",):
        try:
            digest = _manifest_digest(f"{scheme}://{host}/v2/{ref.repo}/manifests/{ref.tag}", ref, timeout)
            _REGISTRY_BASE[ref.registry] = f"{scheme}://{host}"
            return digest
        except HTTPError as e:
            raise RegistryError({
                401: "access denied - private image? (docker login)",
                403: "access denied - private image? (docker login)",
                404: "image or tag not found in the registry",
                429: "rate limited by the registry",
            }.get(e.code, f"registry returned HTTP {e.code}"))
        except (URLError, OSError, HTTPException, ValueError) as e:
            problem = f"could not reach {host}: {getattr(e, 'reason', e)}"
    raise RegistryError(problem)


def _registry_get(ref: ImageRef, path: str, accept: str, timeout: float = REGISTRY_TIMEOUT) -> bytes:
    base = _REGISTRY_BASE.get(ref.registry)
    if not base:
        raise RegistryError("registry not reached yet")
    key = (ref.registry, ref.repo)
    headers = {"Accept": accept}
    if key in _REGISTRY_TOKENS:
        headers["Authorization"] = _REGISTRY_TOKENS[key]
    for attempt in (0, 1):
        try:
            with _http_open(f"{base}/v2/{ref.repo}/{path}", timeout, "GET", headers) as r:
                return r.read(REGISTRY_MAX_BYTES)
        except HTTPError as e:
            if e.code != 401 or attempt:
                raise
            headers["Authorization"] = _registry_auth(e.headers.get("WWW-Authenticate") or "", ref, timeout)
            _REGISTRY_TOKENS[key] = headers["Authorization"]
    raise RegistryError("registry did not answer")


def host_platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    return "linux", {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm", "armv6l": "arm"}.get(machine, machine)


def remote_config(ref: ImageRef, digest: str, wanted: tuple[str, str]) -> tuple[dict, list]:
    try:
        manifest = json.loads(_registry_get(ref, f"manifests/{digest}", MANIFEST_TYPES))
        if isinstance(manifest.get("manifests"), list):
            entries = [m for m in manifest["manifests"] if isinstance(m, dict) and m.get("digest")]
            match = [m for m in entries
                     if ((m.get("platform") or {}).get("os"), (m.get("platform") or {}).get("architecture")) == wanted]
            if not match:
                return {}, []
            manifest = json.loads(_registry_get(ref, f"manifests/{match[0]['digest']}", MANIFEST_TYPES))
        config = json.loads(_registry_get(ref, f"blobs/{manifest['config']['digest']}", "application/json"))
        labels = (config.get("config") or {}).get("Labels") or {}
        env = (config.get("config") or {}).get("Env") or []
        return labels if isinstance(labels, dict) else {}, env if isinstance(env, list) else []
    except (URLError, OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError, RegistryError):
        return {}, []


def image_version(labels: dict, env: list, ref: ImageRef) -> str:
    version = str(labels.get(VERSION_LABEL) or "").strip()
    if not version:
        name = re.sub(r"[^A-Z0-9]", "_", ref.repo.rsplit("/", 1)[-1].upper()) + "_VERSION="
        version = next((str(e)[len(name):].strip() for e in env if str(e).startswith(name)), "")
    return version or (ref.tag if re.search(r"\d", ref.tag) else "")


def image_page(ref: ImageRef) -> str:
    if ref.registry == DOCKER_HUB:
        name = ref.repo.split("/", 1)[1] if ref.repo.startswith("library/") else ""
        return f"https://hub.docker.com/_/{name}" if name else f"https://hub.docker.com/r/{ref.repo}"
    return f"https://{ref.registry}/{ref.repo}"


def image_details(info: dict, ref: ImageRef, local: LocalImage | None, prior: dict) -> None:
    labels = local.labels if local else {}
    registry_image = info["status"] not in ("local", "unknown")
    source = next((str(labels[k]).strip() for k in SOURCE_LABELS if labels.get(k)), "")
    if local:
        info["local_version"] = image_version(labels, local.env, ref)
    if info["status"] == "up_to_date":
        info["remote_version"] = info["local_version"]
    elif info["status"] in IMAGE_OUTDATED and info["remote_digest"]:
        if prior.get("remote_digest") == info["remote_digest"] and "remote_version" in prior:
            info["remote_version"] = str(prior["remote_version"])
            source = source or str(prior.get("source_url") or "")
        else:
            found, env = remote_config(ref, info["remote_digest"], local.platform if local and all(local.platform) else host_platform())
            info["remote_version"] = image_version(found, env, ref)
            source = source or next((str(found[k]).strip() for k in SOURCE_LABELS if found.get(k)), "")
    info["source_url"] = source or (image_page(ref) if registry_image else "")


@dataclass
class LocalImage:
    id: str
    digests: set[str]
    labels: dict
    env: list
    platform: tuple[str, str]


def local_image(image: str) -> LocalImage | None:
    rc, out, err = docker(["image", "inspect", image])
    if rc != 0:
        if "no such" in err.lower():
            return None
        raise RegistryError(_last_line(err, f"docker image inspect exit code {rc}"))
    try:
        data = json.loads(out)
        data = data[0] if isinstance(data, list) else data
    except (ValueError, IndexError):
        data = None
    if not isinstance(data, dict):
        raise RegistryError("could not read docker image inspect output")
    config = data.get("Config") if isinstance(data.get("Config"), dict) else {}
    repo_digests = data.get("RepoDigests") if isinstance(data.get("RepoDigests"), list) else []
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    env = config.get("Env") if isinstance(config.get("Env"), list) else []
    return LocalImage(str(data.get("Id") or ""), {str(d).rpartition("@")[2] for d in repo_digests if "@" in str(d)},
                      labels, env, (str(data.get("Os") or ""), str(data.get("Architecture") or "")))


def inspect_image(image: str, built: bool, prior: dict | None = None) -> dict:
    info = {"status": "unknown", "local_digest": "", "remote_digest": "", "detail": "", "id": "",
            "local_version": "", "remote_version": "", "source_url": ""}
    ref = parse_image_ref(image)
    local: LocalImage | None = None
    try:
        local = local_image(image)
        if built:
            info.update(status="local", detail="built from a Dockerfile")
        elif ref is None:
            info["detail"] = "could not understand the image name"
        elif ref.digest and local is None:
            info.update(status="not_pulled", local_digest="", remote_digest=ref.digest, detail="not pulled yet")
        elif local is None:
            info.update(remote_digest=remote_digest(ref), status="not_pulled", detail="not pulled yet")
        elif ref.digest:
            info.update(status="pinned", local_digest=ref.digest, detail="pinned to a digest")
        elif not local.digests:
            info.update(status="local", detail="not from a registry (built or loaded locally)")
        else:
            remote = remote_digest(ref)
            info["remote_digest"] = remote
            info["local_digest"] = remote if remote in local.digests else sorted(local.digests)[0]
            info["status"] = "up_to_date" if remote in local.digests else "update_available"
        if local:
            info["id"] = local.id
    except RegistryError as e:
        info["detail"] = str(e)
    if ref is not None:
        image_details(info, ref, local, prior or {})
    return info


def declared_images(stack: Path, containers: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}

    def add(image: str, service: str, built: bool) -> None:
        entry = found.setdefault(image, {"services": [], "built": False})
        if service and service not in entry["services"]:
            entry["services"].append(service)
        entry["built"] = entry["built"] or built

    rc, out, _ = docker(["compose", "config", "--format", "json"], cwd=stack)
    try:
        services = json.loads(out).get("services") if rc == 0 else None
    except (ValueError, AttributeError):
        services = None
    if isinstance(services, dict):
        for name, spec in services.items():
            if isinstance(spec, dict) and spec.get("image"):
                add(str(spec["image"]), name, "build" in spec)
        return found
    for c in containers:
        if c["image"]:
            add(c["image"], c["service"], False)
    return found


def running_image_ids(containers: list[dict]) -> dict[str, set[str]]:
    names = [c["name"] for c in containers if c["name"]]
    ids: dict[str, set[str]] = {}
    if not names:
        return ids
    _, out, _ = docker(["inspect", "--type", "container", "--format", "{{.Config.Image}}\t{{.Image}}", *names])
    for line in out.splitlines():
        image, _, image_id = line.strip().partition("\t")
        if image and image_id:
            ids.setdefault(image, set()).add(image_id)
    return ids


def check_images(stacks: list[Path], snaps: dict[str, dict], quiet: bool = False,
                 db: "StatusDB | None" = None) -> dict[str, dict]:
    busy = IS_TTY and not quiet
    prior = db.known_images() if db else {}
    if busy:
        write(f"  {cyan(SPINNER[0])} {dim('Checking registries for new images...')}")

    def gather(stack: Path) -> tuple[dict[str, dict], dict[str, set[str]]]:
        containers = snaps.get(stack.name, {}).get("containers") or []
        return declared_images(stack, containers), running_image_ids(containers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        gathered = dict(zip((s.name for s in stacks), pool.map(gather, stacks)))
        wanted: dict[str, bool] = {}
        for declared, _ in gathered.values():
            for image, entry in declared.items():
                wanted[image] = wanted.get(image, False) or entry["built"]
        checked = dict(zip(wanted, pool.map(
            lambda image: inspect_image(image, wanted[image], prior.get(image)), wanted)))
    if busy:
        write("\r\033[K")

    result: dict[str, dict] = {}
    for name, (declared, in_use) in gathered.items():
        images = []
        for image, entry in sorted(declared.items()):
            info = dict(checked[image], image=image, services=sorted(entry["services"]))
            image_id = info.pop("id")
            if info["status"] == "up_to_date" and in_use.get(image) and image_id not in in_use[image]:
                info.update(status="update_available", detail="new image is pulled, containers still run the old one")
            images.append({k: info[k] for k in ("image", "services", "status", "local_version", "remote_version",
                                                "local_digest", "remote_digest", "source_url", "detail")})
        result[name] = {
            "images": images,
            "updates_available": sum(i["status"] in IMAGE_OUTDATED for i in images),
            "images_checked_at": now_iso(),
        }
    return result


def fmt_updates(count: int) -> str:
    return f"{count} image update{'s' if count != 1 else ''}"


def print_image_report(stacks: list[Path], images: dict[str, dict]) -> None:
    rows = [(s.name, i) for s in stacks for i in images.get(s.name, {}).get("images", [])]
    outdated = [(n, i) for n, i in rows if i["status"] in IMAGE_OUTDATED]
    unknown = [(n, i) for n, i in rows if i["status"] == "unknown"]
    width = max((len(n) for n, _ in outdated + unknown), default=0)
    iwidth = max((len(i["image"]) for _, i in outdated + unknown), default=0)
    if outdated:
        rule(f"Image updates ({len(outdated)})")
        for name, i in outdated:
            old, new = i.get("local_version"), i.get("remote_version")
            if not (old and new and old != new):
                old, new = i["local_digest"][7:19], i["remote_digest"][7:19]
            note = i["detail"] or f"{old} {SYM['arrow']} {new}"
            say(f"  {yellow(SYM['up'])} {name:<{width}}  {i['image']:<{iwidth}}  {dim(note)}")
    if unknown:
        rule(f"Images not checked ({len(unknown)})")
        for name, i in unknown:
            say(f"  {yellow(SYM['warn'])} {name:<{width}}  {i['image']:<{iwidth}}  {dim(i['detail'])}")
    if rows and not outdated and not unknown:
        say("\n  " + green(f"{SYM['ok']} All {len(rows)} image(s) are up to date"))


class StatusDB:

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load(warn=True)
        self.written_by = self.data.get("tugboat_version")
        self._changes: dict[str, dict] = {}
        self._top: dict = {}
        self._known: set[str] | None = None

    def _load(self, warn: bool) -> dict:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            loaded = None
        except ValueError:
            loaded = None
            broken = self.path.with_name(self.path.name + ".broken")
            if warn:
                say(yellow(f"{self.path.name} is not valid JSON - starting fresh (old file kept as {broken.name})"))
            try:
                self.path.replace(broken)
            except OSError:
                pass
        except OSError as e:
            loaded = None
            if warn:
                say(yellow(f"{self.path.name} could not be read ({e.strerror or e}) - starting fresh"))
        if isinstance(loaded, dict) and isinstance(loaded.get("stacks"), dict):
            return loaded
        return {"stacks": {}}

    def stack(self, name: str) -> dict:
        entry = self.data["stacks"].get(name)
        return entry if isinstance(entry, dict) else {}

    def _set(self, name: str, values: dict) -> None:
        if not isinstance(self.data["stacks"].get(name), dict):
            self.data["stacks"][name] = {}
        self.data["stacks"][name].update(values)
        self._changes.setdefault(name, {}).update(values)

    def known_images(self) -> dict[str, dict]:
        known: dict[str, dict] = {}
        for entry in self.data["stacks"].values():
            images = entry.get("images") if isinstance(entry, dict) else None
            for image in images if isinstance(images, list) else []:
                if isinstance(image, dict) and image.get("image"):
                    known[str(image["image"])] = image
        return known

    def set_top(self, key: str, value: object) -> None:
        self.data[key] = value
        self._top[key] = value

    def set_health(self, name: str, snapshot: dict) -> None:
        self._set(name, {k: snapshot[k] for k in ("health", "summary", "problems", "checked_at", "containers")})

    def set_images(self, name: str, images: dict) -> None:
        self._set(name, {k: images[k] for k in ("images", "updates_available", "images_checked_at")})

    def set_action(self, res: StackResult, action: str) -> None:
        values: dict = {"last_action": {
            "action": action,
            "ok": res.ok,
            "result": res.status,
            "at": now_iso(),
            "duration_s": round(res.seconds, 1),
            "errors": [f"{e.step}: {e.message}" for e in res.errors],
            "warnings": [f"{w.step}: {w.message}" for w in res.warnings],
        }}
        if res.backup:
            values["last_backup"] = res.backup
        if action == "update" and res.ok:
            values["last_update"] = now_iso()
        self._set(res.name, values)

    def forget_missing(self, existing: list[Path]) -> None:
        self._known = {s.name for s in existing}
        for name in list(self.data["stacks"]):
            if name not in self._known:
                del self.data["stacks"][name]

    def save(self) -> None:
        data = self._load(warn=False)
        for name, values in self._changes.items():
            if not isinstance(data["stacks"].get(name), dict):
                data["stacks"][name] = {}
            data["stacks"][name].update(values)
        stacks = {n: e for n, e in data["stacks"].items()
                  if isinstance(e, dict) and (self._known is None or n in self._known)}
        data["stacks"] = dict(sorted(stacks.items(), key=lambda kv: kv[0].lower()))
        summary: dict[str, int] = {"stacks": len(stacks)}
        for entry in stacks.values():
            if entry.get("health"):
                summary[entry["health"]] = summary.get(entry["health"], 0) + 1
        summary["updates_available"] = sum(e.get("updates_available") or 0 for e in stacks.values())
        data["summary"] = summary
        data.update(self._top)
        data["tugboat_version"] = __version__
        data["written_at"] = now_iso()
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        self.data = data
        self._changes = {}
        self._top = {}


HEALTH_STYLE = {
    "healthy":   ("ok", green),
    "starting":  ("warn", yellow),
    "stopped":   ("skip", dim),
    "unhealthy": ("fail", red),
    "unknown":   ("fail", red),
}


def check_all(stacks: list[Path]) -> dict[str, dict]:
    if IS_TTY:
        write(f"  {cyan(SPINNER[0])} {dim(f'Checking {len(stacks)} stack(s)...')}")
    with ThreadPoolExecutor(max_workers=min(8, len(stacks) or 1)) as pool:
        snaps = dict(zip((s.name for s in stacks), pool.map(check_health, stacks)))
    if IS_TTY:
        write("\r\033[K")
    return snaps


def fmt_age(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return ""


def fmt_ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        seconds = (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return ""
    return fmt_age(seconds)


def print_health_table(stacks: list[Path], snaps: dict[str, dict], db: StatusDB | None,
                       images: dict[str, dict] | None = None) -> None:
    width = max(len(s.name) for s in stacks)
    swidth = max(len(snaps[s.name]["summary"]) for s in stacks)
    for stack in stacks:
        snap = snaps[stack.name]
        sym, style = HEALTH_STYLE[snap["health"]]
        health = f"{snap['health']:<10}"
        summary = f"{snap['summary']:<{swidth}}"
        extra = []
        last = db.stack(stack.name).get("last_update") if db else None
        if last:
            extra.append(dim(f"updated {fmt_ago(last)}"))
        known = images.get(stack.name, {}) if images is not None else db.stack(stack.name) if db else {}
        if known.get("updates_available"):
            extra.append(yellow(f"{SYM['up']} {fmt_updates(known['updates_available'])}"))
        if snap["problems"]:
            more = f" (+{len(snap['problems']) - 1} more)" if len(snap["problems"]) > 1 else ""
            extra.append(style(snap["problems"][0] + more))
        tail = dim("  ·  ").join(extra)
        say(f"  {style(SYM[sym])} {stack.name:<{width}}  {style(health)} {dim(summary)}  {tail}".rstrip())


def show_overview(stacks: list[Path], db: StatusDB | None) -> dict[str, dict]:
    snaps = check_all(stacks)
    counts: dict[str, int] = {}
    for snap in snaps.values():
        counts[snap["health"]] = counts.get(snap["health"], 0) + 1
    order = ("healthy", "starting", "unhealthy", "unknown", "stopped")
    parts = [HEALTH_STYLE[h][1](f"{counts[h]} {h}") for h in order if counts.get(h)]
    rule("Stacks")
    print_health_table(stacks, snaps, db)
    say("\n  " + dim(" · ").join(parts))
    if db:
        for name, snap in snaps.items():
            db.set_health(name, snap)
        try:
            db.save()
        except OSError:
            pass
    return snaps


def run_healthcheck(selected: list[Path], db: StatusDB | None, image_check: bool,
                    image_max_age: float = 0) -> int:
    say("\n" + dim(" · ").join([bold("HEALTH CHECK"),
                                f"{len(selected)} stack{'s' if len(selected) > 1 else ''}",
                                datetime.now().strftime("%Y-%m-%d %H:%M")]))
    rule("Stacks")
    snaps = check_all(selected)
    images: dict[str, dict] | None = None
    checked: dict[str, dict] = {}
    if image_check:
        same_version = bool(db) and db.written_by == __version__
        images = {s.name: db.stack(s.name) for s in selected
                  if same_version and is_fresh(db.stack(s.name).get("images_checked_at"), image_max_age)}
        due = [s for s in selected if s.name not in images]
        if due:
            checked = check_images(due, snaps, db=db)
            images.update(checked)
    print_health_table(selected, snaps, db, images)
    bad = [(s.name, snaps[s.name]) for s in selected if snaps[s.name]["problems"]]
    if db:
        for name, snap in snaps.items():
            db.set_health(name, snap)
        for name, found in checked.items():
            db.set_images(name, found)

    if bad:
        rule(f"Problems ({len(bad)})")
        for name, snap in bad:
            say(f"\n  {red(SYM['fail'])} {bold(name)}")
            for p in snap["problems"]:
                say(f"      {p}")
    if images is not None:
        print_image_report(selected, images)
        ages = [age_seconds(images[s.name].get("images_checked_at")) or 0 for s in selected]
        if not checked and ages:
            say("\n" + dim(f"  Image versions from {fmt_time(max(ages))} ago - checked again when older than "
                           f"{fmt_time(image_max_age)} (--check-images to check now)"))
    code = 1 if bad else 0
    if db:
        try:
            db.save()
            say("\n" + dim(f"Status written to {db.path}"))
        except OSError as e:
            say()
            say_error(f"Could not write {db.path}: {e.strerror or e}")
            code = code or 2
    say()
    return code


def _fail(res: StackResult, step: str, out: StepOutcome, status: str) -> StackResult:
    res.ok = False
    res.status = status
    res.errors.append(Issue(res.name, step, out.note, out.details, out.output))
    return res


COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_CREATE = "docker compose up -d --pull missing"
UPDATE_CMD = "docker compose pull && docker compose up -d --remove-orphans"
STOP_DRY = "would stop each running container with: docker container stop <name>"
OLD_CONTAINERS_DRY = "would remove containers from outside the stack that use the same names"
START_DRY = ("would start each stopped container with: docker container start <name>, and create "
             f"missing ones with: {COMPOSE_CREATE} --force-recreate --no-deps <service>")


def name_conflicts(stack: Path) -> tuple[list[dict], list[str]]:
    rc, out, _ = docker(["compose", "config", "--format", "json"], cwd=stack)
    try:
        config = json.loads(out) if rc == 0 else None
    except ValueError:
        config = None
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        return [], []
    project = str(config.get("name") or "")
    wanted = sorted({str(spec["container_name"]) for spec in config["services"].values()
                     if isinstance(spec, dict) and spec.get("container_name")})
    if not wanted:
        return [], []
    _, out, _ = docker(["inspect", "--type", "container", *wanted])
    try:
        found = json.loads(out)
    except ValueError:
        found = []
    remove: list[dict] = []
    blocked: list[str] = []
    for c in found if isinstance(found, list) else []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("Name") or "").lstrip("/")
        if name not in wanted:
            continue
        labels = (c.get("Config") or {}).get("Labels") or {}
        owner = str(labels.get(COMPOSE_PROJECT_LABEL) or "")
        running = bool((c.get("State") or {}).get("Running"))
        if owner and owner != project:
            blocked.append(f"{name} belongs to the compose project '{owner}'")
        elif not owner:
            remove.append({"name": name, "running": running})
    return remove, blocked


def clear_work(remove: list[dict], blocked: list[str]) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        if blocked:
            return False, "container name used by another stack", blocked
        for c in remove:
            if CANCEL.is_set():
                return False, "cancelled", []
            if c["running"]:
                emit(f"stopping {c['name']}")
                rc, _, err = docker(["stop", c["name"]], timeout=120)
                if rc != 0:
                    return False, f"could not stop {c['name']}", [_last_line(err, "no error message")]
            emit(f"removing {c['name']}")
            rc, _, err = docker(["rm", c["name"]], timeout=60)
            if rc != 0:
                return False, f"could not remove {c['name']}", [_last_line(err, "no error message")]
        return True, "removed " + ", ".join(c["name"] for c in remove), []
    return work


def clear_old_containers(stack: Path, ui: Runner) -> StepOutcome | None:
    remove, blocked = name_conflicts(stack)
    if not (remove or blocked):
        return None
    return ui.step("Old containers", clear_work(remove, blocked))


def compose_order(stack: Path) -> list[str] | None:
    rc, out, _ = docker(["compose", "config", "--format", "json"], cwd=stack)
    try:
        services = json.loads(out).get("services") if rc == 0 else None
    except (ValueError, AttributeError):
        services = None
    if not isinstance(services, dict):
        return None
    needs = {}
    for name, spec in services.items():
        wanted = spec.get("depends_on") if isinstance(spec, dict) else None
        needs[name] = sorted(wanted) if isinstance(wanted, (list, dict)) else []
    order: list[str] = []
    pending = sorted(needs)
    while pending:
        ready = [name for name in pending if all(d in order or d not in needs for d in needs[name])]
        chosen = (ready or pending)[0]
        order.append(chosen)
        pending.remove(chosen)
    return order


def in_order(containers: list[dict], order: list[str] | None, reverse: bool = False) -> list[dict]:
    rank = {name: i for i, name in enumerate(order or [])}
    return sorted(containers, key=lambda c: (rank.get(c["service"], len(rank)), c["name"]), reverse=reverse)


def container_cmd(verb: str, names: list[str]) -> str:
    steps = "; ".join(f"docker container {verb} {shlex.quote(name)} || rc=1" for name in names)
    return f"rc=0; {steps}; exit $rc"


def unreadable(ui: Runner, label: str, err: str) -> StepOutcome:
    note = f"could not read the stack: {err}"
    ui._line("fail", label, None, red(note))
    return StepOutcome(False, False, note, [], [], 0.0)


def stop_containers(stack: Path, cfg: Config, ui: Runner) -> StepOutcome:
    containers, err = compose_ps(stack)
    if containers is None:
        return unreadable(ui, "Stop", err)
    active = [c for c in containers if c["name"] and c["state"] in ("running", "restarting", "paused")]
    if not active:
        ui.skip("Stop", "no running containers")
        return StepOutcome(True, False, "", [], [], 0.0)
    names = [c["name"] for c in in_order(active, compose_order(stack), reverse=True)]
    return ui.step("Stop", command_work(container_cmd("stop", names), stack), timeout=cfg.command_timeout)


def create_cmd(services: list[str]) -> str:
    if not services:
        return COMPOSE_CREATE
    return f"{COMPOSE_CREATE} --force-recreate --no-deps " + " ".join(shlex.quote(name) for name in services)


def start_containers(stack: Path, cfg: Config, ui: Runner) -> tuple[str, StepOutcome] | None:
    old = clear_old_containers(stack, ui)
    if old and not old.ok:
        return "Old containers", old
    containers, err = compose_ps(stack)
    if containers is None:
        return "Start", unreadable(ui, "Start", err)
    declared = compose_order(stack)
    if not containers and declared is None:
        out = ui.step("Create", command_work(create_cmd([]), stack), timeout=cfg.command_timeout)
        return None if out.ok else ("Create", out)
    existing = {c["service"] for c in containers}
    redo = {name for name in declared or [] if name not in existing}
    redo |= {c["service"] for c in containers if c["state"] == "dead" and c["service"]}
    to_start = [c for c in in_order(containers, declared) if c["name"] and c["state"] in ("exited", "created")]
    started: StepOutcome | None = None
    if to_start:
        command = container_cmd("start", [c["name"] for c in to_start])
        started = ui.step("Start", command_work(command, stack), timeout=cfg.command_timeout)
        if not started.ok:
            after = compose_ps(stack)[0] or []
            redo |= {c["service"] for c in after if c["service"] and c["state"] != "running"}
    if redo:
        names = [name for name in declared or sorted(redo) if name in redo]
        out = ui.step("Create", command_work(create_cmd(names), stack), timeout=cfg.command_timeout)
        if not out.ok:
            return "Create", out
    elif started is not None and not started.ok:
        return "Start", started
    return None


def start_stack(stack: Path, cfg: Config, ui: Runner) -> StackResult:
    res = StackResult(stack.name)
    if ui.dry_run:
        ui.dry("Old containers", OLD_CONTAINERS_DRY)
        ui.dry("Start", START_DRY)
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would start"
        return res
    failed = start_containers(stack, cfg, ui)
    if failed:
        return _fail(res, failed[0], failed[1], "start failed")
    out = ui.step("Health check", health_work(stack, cfg, res))
    if out.warn:
        res.warnings.append(Issue(res.name, "Health check", out.note, out.details, out.output))
    elif not out.ok:
        return _fail(res, "Health check", out, "started but unhealthy")
    res.status = "started"
    return res


def stop_stack(stack: Path, cfg: Config, ui: Runner) -> StackResult:
    res = StackResult(stack.name)
    if ui.dry_run:
        ui.dry("Stop", STOP_DRY)
        res.status = "would stop"
        return res
    out = stop_containers(stack, cfg, ui)
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed")
    res.status = "stopped"
    return res


def restart_stack(stack: Path, cfg: Config, ui: Runner) -> StackResult:
    res = StackResult(stack.name)
    if ui.dry_run:
        ui.dry("Stop", STOP_DRY)
        ui.dry("Old containers", OLD_CONTAINERS_DRY)
        ui.dry("Start", START_DRY)
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would restart"
        return res
    out = stop_containers(stack, cfg, ui)
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed, left as is")
    failed = start_containers(stack, cfg, ui)
    if failed:
        return _fail(res, failed[0], failed[1], "start failed - stack is stopped")
    out = ui.step("Health check", health_work(stack, cfg, res))
    if out.warn:
        res.warnings.append(Issue(res.name, "Health check", out.note, out.details, out.output))
    elif not out.ok:
        return _fail(res, "Health check", out, "restarted but unhealthy")
    res.status = "restarted"
    return res


def _restart_old(stack: Path, cfg: Config, ui: Runner, res: StackResult, what: str) -> StackResult:
    failed = start_containers(stack, cfg, ui)
    if failed is None:
        res.status = f"{what} failed, old version restarted"
    else:
        _fail(res, "Restart old version", failed[1], f"{what} AND restart failed - stack is DOWN")
    return res


def ask_for_backup(stack: Path, backups: list[tuple[datetime, Path]]) -> Path | None:
    say("\n" + bold(f"Backups of {stack.name}"))
    for i, (when, folder) in enumerate(backups, 1):
        age = fmt_age((datetime.now() - when).total_seconds())
        _menu_item(str(i), f"{folder.name}  {dim(when.strftime('%Y-%m-%d %H:%M') + ', ' + age)}")
    _menu_item("q", dim("Quit"))
    say(dim("  Enter = newest"))
    while True:
        answer = _prompt()
        if answer is None or answer in ("q", "quit", "exit"):
            return None
        if answer == "":
            return backups[0][1]
        if answer.isdigit() and 1 <= int(answer) <= len(backups):
            return backups[int(answer) - 1][1]
        say(yellow("  Invalid choice, try again."))


def restore_work(stack: Path, backup: Path) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        try:
            emit(f"clearing {stack}")
            for entry in stack.iterdir():
                if CANCEL.is_set():
                    return False, "cancelled", []
                if entry.is_symlink() or not entry.is_dir():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
            emit(f"copying {backup} -> {stack}")
            shutil.copytree(backup, stack, symlinks=True, copy_function=_copy_file, dirs_exist_ok=True)
            _copy_dir_owners(backup, stack)
            return True, f"{SYM['arrow']} {backup.name}", []
        except shutil.Error as e:
            failures = e.args[0] if e.args and isinstance(e.args[0], list) else []
            details = [f"{src}: {str(reason).split(']')[-1].split(':')[0].strip()}"
                       for src, _, reason in failures[:10]]
            if len(failures) > 10:
                details.append(f"... and {len(failures) - 10} more")
            note = "cancelled" if CANCEL.is_set() else f"{len(failures)} file(s) could not be restored"
            return False, note, details
        except OSError as e:
            return False, str(e), []
    return work


def rollback_stack(stack: Path, cfg: Config, backup: Path, safety_backup: bool, ui: Runner) -> StackResult:
    res = StackResult(stack.name)
    if backup_inside_stack(stack, cfg):
        note = f"backup_path {cfg.backup_root(stack.name)} is inside the stack folder"
        ui._line("fail", "Rollback", None, red(note))
        res.ok, res.status = False, "not rolled back"
        res.errors.append(Issue(res.name, "Rollback", note, ["Change backup_path in TugBoat.conf."]))
        return res
    if ui.dry_run:
        ui.dry("Stop", STOP_DRY)
        if safety_backup:
            ui.dry("Safety backup", f"would copy the current state to {cfg.backup_root(stack.name)}/<timestamp>")
        else:
            ui.skip("Safety backup", "skipped")
        ui.dry("Restore", f"would replace the stack folder with {backup}")
        ui.dry("Old containers", OLD_CONTAINERS_DRY)
        ui.dry("Recreate", f"would run: {COMPOSE_CREATE} --force-recreate --remove-orphans")
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would roll back"
        return res

    out = stop_containers(stack, cfg, ui)
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed, left as is")

    if safety_backup:
        dest = new_backup_dest(stack, cfg)
        out = ui.step("Safety backup", backup_work(stack, dest))
        if not out.ok:
            _fail(res, "Safety backup", out, "backup failed")
            return _restart_old(stack, cfg, ui, res, "rollback")
        res.backup = str(dest)
    else:
        ui.skip("Safety backup", "skipped")

    kept = f" - the state before the rollback is in {res.backup}" if res.backup else ""
    out = ui.step("Restore", restore_work(stack, backup))
    if not out.ok:
        return _fail(res, "Restore", out, f"restore failed, stack is stopped{kept}")

    old = clear_old_containers(stack, ui)
    if old and not old.ok:
        return _fail(res, "Old containers", old, f"rollback failed, stack is stopped{kept}")

    services = compose_order(stack)
    command = create_cmd(services) if services else f"{COMPOSE_CREATE} --force-recreate"
    out = ui.step("Recreate", command_work(f"{command} --remove-orphans", stack), timeout=cfg.command_timeout)
    if not out.ok:
        return _fail(res, "Recreate", out, f"restored, but the containers could not be created{kept}")

    out = ui.step("Health check", health_work(stack, cfg, res))
    if out.warn:
        res.warnings.append(Issue(res.name, "Health check", out.note, out.details, out.output))
    elif not out.ok:
        return _fail(res, "Health check", out, "rolled back but unhealthy")

    res.status = "rolled back"
    return res


def run_list_backups(stacks: list[Path], cfg: Config) -> int:
    for stack in stacks:
        backups = list_backups(stack, cfg)
        rule(f"{stack.name} ({len(backups)})")
        if not backups:
            say(dim("  no backups"))
        for when, folder in backups:
            age = fmt_age((datetime.now() - when).total_seconds())
            say(f"  {folder.name}  {dim(when.strftime('%Y-%m-%d %H:%M') + ', ' + age)}")
    say()
    return 0


def update_stack(stack: Path, cfg: Config, do_backup: bool, ui: Runner) -> StackResult:
    res = StackResult(stack.name)

    if do_backup and backup_inside_stack(stack, cfg):
        note = f"backup_path {cfg.backup_root(stack.name)} is inside the stack folder"
        ui._line("fail", "Backup", None, red(note))
        res.ok, res.status = False, "not updated"
        res.errors.append(Issue(res.name, "Backup", note, ["Change backup_path in TugBoat.conf."]))
        return res

    if ui.dry_run:
        ui.dry("Stop", STOP_DRY)
        if do_backup:
            ui.dry("Backup", f"would copy to {cfg.backup_root(stack.name)}/<timestamp>")
            large = (f", or {max(1, cfg.backup_large_retention)} when a backup is over {cfg.backup_large_mb} MB"
                     if cfg.backup_large_mb > 0 else "")
            ui.dry("Prune backups", f"would keep newest {cfg.backup_retention or 'all'}{large}")
        else:
            ui.skip("Backup", "skipped")
        ui.dry("Old containers", OLD_CONTAINERS_DRY)
        ui.dry("Pull & start", f"would run: {UPDATE_CMD}")
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would update"
        return res

    out = stop_containers(stack, cfg, ui)
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed, left as is")

    if do_backup:
        dest = new_backup_dest(stack, cfg)
        out = ui.step("Backup", backup_work(stack, dest))
        if out.ok:
            res.backup = str(dest)
        if not out.ok:
            _fail(res, "Backup", out, "backup failed")
            return _restart_old(stack, cfg, ui, res, "backup")
        if cfg.backup_retention > 0 or cfg.backup_large_mb > 0:
            out = ui.step("Prune backups", prune_work(stack, cfg), warn_only=True)
            if not out.ok:
                res.warnings.append(Issue(res.name, "Prune backups", out.note, out.details, out.output))
        else:
            ui.skip("Prune backups", "retention 0, keeping all")
    else:
        ui.skip("Backup", "skipped")

    old = clear_old_containers(stack, ui)
    if old and not old.ok:
        _fail(res, "Old containers", old, "update failed")
        return _restart_old(stack, cfg, ui, res, "update")

    out = ui.step("Pull & start", command_work(UPDATE_CMD, stack), timeout=cfg.command_timeout)
    if not out.ok:
        _fail(res, "Pull & start", out, "update failed")
        return _restart_old(stack, cfg, ui, res, "update")

    out = ui.step("Health check", health_work(stack, cfg, res))
    if out.warn:
        res.warnings.append(Issue(res.name, "Health check", out.note, out.details, out.output))
    elif not out.ok:
        return _fail(res, "Health check", out, "updated but unhealthy")

    res.status = "updated"
    return res


def print_issue(issue: Issue, style: Callable[[str], str], sym: str) -> None:
    say(f"\n  {style(sym)} {bold(issue.stack)} {dim(SYM['arrow'])} {issue.step}: {style(issue.message)}")
    for d in issue.details:
        say(f"      {d}")
    lines = [line for line in issue.output if line.strip()]
    if lines:
        hidden = len(lines) - ERROR_TAIL_LINES
        if hidden > 0:
            say(dim(f"      ... {hidden} earlier line(s) hidden, use --verbose to see everything"))
        for line in lines[-ERROR_TAIL_LINES:]:
            say(dim(f"      {SYM['bar']} ") + line)


def print_report(results: list[StackResult], pending: list[str], action: str,
                 total_seconds: float, interrupted: bool) -> None:
    rule("Summary")
    width = max([len(r.name) for r in results] + [len(p) for p in pending] + [5])
    swidth = max(len(r.status) for r in results) if results else 0
    for r in results:
        sym = green(SYM["ok"]) if r.ok else red(SYM["fail"])
        padded = f"{r.status:<{swidth}}"
        status = padded if r.ok else red(padded)
        warn = yellow(f"  {len(r.warnings)} warning{'s' if len(r.warnings) > 1 else ''}") if r.warnings else ""
        say(f"  {sym} {r.name:<{width}}  {status}  {dim(f'{fmt_time(r.seconds):>7}')}{warn}")
    for name in pending:
        say(f"  {dim(SYM['skip'])} {dim(f'{name:<{width}}  not run (interrupted)')}")

    n_ok = sum(r.ok for r in results)
    n_fail = len(results) - n_ok
    parts = [green(f"{n_ok} ok")]
    if n_fail:
        parts.append(red(f"{n_fail} failed"))
    if pending:
        parts.append(yellow(f"{len(pending)} not run"))
    say("\n  " + dim(" · ").join(parts) + dim(f"  ·  {action}  ·  {fmt_time(total_seconds)}"))

    errors = [e for r in results for e in r.errors]
    warnings = [w for r in results for w in r.warnings]
    if warnings:
        rule(f"Warnings ({len(warnings)})")
        for w in warnings:
            print_issue(w, yellow, SYM["warn"])
    if errors:
        rule(f"Errors ({len(errors)})")
        for e in errors:
            print_issue(e, red, SYM["fail"])
    if interrupted:
        say("\n" + red("Interrupted - the stack that was running may be stopped."))
    say()


def _on_sigterm(signum, frame) -> None:
    raise KeyboardInterrupt


def main() -> int:
    if sys.version_info < (3, 9):
        say_error(f"{__title__} needs Python 3.9 or newer (this is {sys.version.split()[0]})")
        return 2
    parser = argparse.ArgumentParser(
        description="Manage Docker Compose stacks.",
        epilog="Stack names are folder names. Give them after the action or with --stack. "
               "With no names, use --all, --auto or pick from a list.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", nargs="*", metavar="STACK",
                      help="stop -> backup -> pull -> start -> health check")
    mode.add_argument("--start", nargs="*", metavar="STACK", help="start the stack's containers; a missing container is created, and its image pulled only if the host does not have it")
    mode.add_argument("--stop", nargs="*", metavar="STACK", help="stop the stack's containers (they are kept)")
    mode.add_argument("--restart", nargs="*", metavar="STACK",
                      help="stop -> start -> health check, without pulling new images")
    mode.add_argument("--rollback", nargs="*", metavar="STACK",
                      help="restore a backup of one stack (the current state is backed up first)")
    mode.add_argument("--list-backups", nargs="*", metavar="STACK",
                      help="list the backups of the stacks (all stacks if no names)")
    mode.add_argument("--healthcheck", nargs="*", metavar="STACK",
                      help="check status/health and new image versions, write the status file "
                           "(all stacks if no names)")
    mode.add_argument("--install", action="store_true",
                      help="set up the cron job that runs --healthcheck every CRON_EVERY_MINUTES "
                           "minutes (TugBoat also keeps it in line on every run, see manage_cron)")
    mode.add_argument("--uninstall", action="store_true", help="remove the TugBoat cron job")
    mode.add_argument("--check-update", action="store_true",
                      help="check GitHub for a newer TugBoat release")
    mode.add_argument("--self-update", action="store_true",
                      help="install the newest TugBoat release from GitHub")
    parser.add_argument("--stack", nargs="+", action="extend", metavar="STACK", default=[],
                        help="stack(s) to act on, for any action (no action given: ask, or update with --auto)")
    parser.add_argument("--all", action="store_true",
                        help="run the action on all stacks (on its own: update all)")
    parser.add_argument("--auto", action="store_true",
                        help="no questions: default action update, all stacks unless named, confirm everything")
    parser.add_argument("--only-outdated", action="store_true",
                        help="update only stacks that have a new image version (update only)")
    parser.add_argument("--skip-backup", action="store_true",
                        help="skip the backup step (update and rollback)")
    parser.add_argument("--to", metavar="BACKUP",
                        help="the backup to roll back to, by folder name or its start (rollback only)")
    images_opt = parser.add_mutually_exclusive_group()
    images_opt.add_argument("--no-image-check", action="store_true",
                            help="do not ask the registries for new image versions")
    images_opt.add_argument("--check-images", action="store_true",
                            help="ask the registries now, even if image_check_interval has not passed")
    parser.add_argument("-v", "--verbose", action="store_true", help="show full command output live")
    parser.add_argument("--dry-run", action="store_true", help="show actions without running them")
    parser.add_argument("--version", action="version",
                        version=f"{__title__} {__version__} - {__author__} - {__git__}")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _on_sigterm)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        if args.check_update:
            banner()
            return run_check_update()
        if args.self_update:
            script = Path(__file__).resolve()
            if not args.dry_run and not (os.access(script, os.W_OK) and os.access(script.parent, os.W_OK)):
                ensure_root()
            banner()
            return run_self_update(args.dry_run)
        cfg = load_config(CONFIG_FILE)
        if cfg.require_root and not args.dry_run:
            ensure_root(non_interactive=args.auto or not sys.stdin.isatty())
        banner()
        needed = missing_dependencies(not args.uninstall, cfg.manage_cron or args.install or args.uninstall)
        if needed:
            needed = handle_dependencies(needed, cfg, args)
        if "docker" in needed or "compose" in needed:
            say_error("Docker with the Compose plugin is required - install it and run TugBoat again")
            return 2
        user_warning = setup_docker_user(cfg.docker_user)
        if user_warning:
            say(yellow(f"{SYM['warn']} {user_warning}"))
        try:
            created = not CONFIG_FILE.is_file()
            added, removed = sync_config(CONFIG_FILE, write=not args.dry_run)
        except OSError as e:
            say(yellow(f"{SYM['warn']} Config not updated: {e.strerror or e}"))
        else:
            verb = "would be " if args.dry_run else ""
            if created and added:
                say(green(f"{SYM['ok']} Default config {verb}created: {CONFIG_FILE}"))
            elif added:
                say(green(f"{SYM['ok']} Config: setting(s) {verb}added with defaults: {', '.join(added)}"))
            if removed:
                say(green(f"{SYM['ok']} Config: obsolete setting(s) {verb}removed: {', '.join(removed)}"))
        if args.uninstall:
            return run_uninstall(cfg, args.dry_run)
        if args.install:
            code = run_install(cfg, args.dry_run)
            if code or args.dry_run:
                return code
            found = find_stacks(cfg.container_path, cfg.ignore_folders)
            if found:
                run_healthcheck(found, StatusDB(cfg.status_file), cfg.image_check,
                                cfg.image_check_interval * 60)
            return 0
        db: StatusDB | None = None if args.dry_run else StatusDB(cfg.status_file)
        startup_update_check(cfg, args.dry_run, db)
        stacks = find_stacks(cfg.container_path, cfg.ignore_folders)
        if cfg.manage_cron and not args.dry_run:
            state, old = ensure_cron(cfg, False)
            if state != "same":
                say(describe_cron(cfg, state, old))
    except (OSError, ValueError) as e:
        say_error(str(e))
        return 2

    if not stacks:
        say(yellow(f"No stacks found in {cfg.container_path}"))
        return 0

    action: str | None = None
    raw_names: list[str] = list(args.stack)
    if args.list_backups is not None:
        wanted = [n.strip() for v in list(args.list_backups) + list(args.stack) for n in v.split(",") if n.strip()]
        try:
            chosen = pick_stacks_by_name(wanted, stacks, cfg) if wanted else stacks
        except ValueError as e:
            say_error(str(e))
            return 2
        return run_list_backups(chosen, cfg)

    for a in ("update", "start", "stop", "restart", "rollback", "healthcheck"):
        value = getattr(args, a)
        if value is not None:
            action = a
            raw_names = list(value) + raw_names
    names = [n.strip() for v in raw_names for n in v.split(",") if n.strip()]
    if action is None and (args.all or args.auto):
        action = "update"

    if names and args.all:
        say_error("Give stack names or --all, not both.")
        return 2
    if args.only_outdated and action not in (None, "update"):
        say_error("--only-outdated only works with --update.")
        return 2
    if args.to and action not in (None, "rollback"):
        say_error("--to only works with --rollback.")
        return 2

    if db:
        db.forget_missing(stacks)

    snaps: dict[str, dict] | None = None
    if action is None:
        snaps = show_overview(stacks, db)
        action = ask_for_action()
        if action is None:
            say(dim("Nothing selected, exiting."))
            return 0

    if names:
        try:
            selected = pick_stacks_by_name(names, stacks, cfg)
        except ValueError as e:
            say_error(str(e))
            return 2
    elif args.all or args.auto or action == "healthcheck":
        selected = stacks
    else:
        selected = ask_for_stacks(stacks, action, snaps, db)
    if not selected:
        say(dim("Nothing selected, exiting."))
        return 0

    rollback_to: Path | None = None
    if action == "rollback":
        if len(selected) != 1:
            say_error("Roll back one stack at a time - name it, for example: --rollback web")
            return 2
        backups = list_backups(selected[0], cfg)
        if not backups:
            say_error(f"No backups of '{selected[0].name}' in {cfg.backup_root(selected[0].name)}")
            return 2
        if args.to:
            matches = ([d for _, d in backups if d.name == args.to]
                       or [d for _, d in backups if d.name.startswith(args.to)])
            if len(matches) != 1:
                say_error(f"{'No backup' if not matches else 'Several backups'} match '{args.to}'. "
                          f"Available: {', '.join(d.name for _, d in backups)}")
                return 2
            rollback_to = matches[0]
        elif args.auto:
            rollback_to = backups[0][1]
        elif sys.stdin.isatty():
            rollback_to = ask_for_backup(selected[0], backups)
        else:
            say_error("Say which backup with --to NAME (see --list-backups), or use --auto for the newest")
            return 2
        if rollback_to is None:
            say(dim("Nothing selected, exiting."))
            return 0
        if not (args.auto or args.dry_run):
            if not sys.stdin.isatty():
                say_error("Add --auto to confirm a rollback without a terminal")
                return 2
            answer = _prompt(f"Roll back {selected[0].name} to {rollback_to.name}? "
                             f"The current state is backed up first. [y/N] ")
            if answer not in ("y", "yes"):
                say(dim("Cancelled."))
                return 0

    image_check = (cfg.image_check or args.check_images) and not args.no_image_check
    if action == "healthcheck":
        max_age = 0 if args.check_images else cfg.image_check_interval * 60
        check_lock = None
        if not args.dry_run:
            if action_running():
                say(yellow(f"{SYM['warn']} A start/stop/update is running - health check skipped"))
                return 0
            check_lock = acquire_healthcheck_lock()
            if check_lock is None:
                say(yellow(f"{SYM['warn']} Another health check is still running - skipped"))
                return 0
        return run_healthcheck(selected, db, image_check, max_age)

    lock = None
    if not args.dry_run:
        try:
            lock = acquire_run_lock()
        except (OSError, RuntimeError) as e:
            say_error(str(e))
            return 2

    do_backup = cfg.backup and not args.skip_backup
    ui = Runner(verbose=args.verbose, dry_run=args.dry_run)

    plan = [bold(action.upper()), f"{len(selected)} stack{'s' if len(selected) > 1 else ''}"]
    if action == "update":
        plan.append(f"backup {'on' if do_backup else yellow('off')}")
        if args.only_outdated:
            plan.append("only outdated")
    if action == "rollback":
        plan.append(f"to {rollback_to.name}")
        plan.append(f"safety backup {'off' if args.skip_backup else 'on'}")
    if cfg.docker_user:
        plan.append(f"docker as {cfg.docker_user}")
    plan.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    if args.auto:
        plan.append("auto")
    if args.dry_run:
        plan.append(cyan("DRY RUN"))
    say("\n" + dim(" · ").join(plan))

    if action == "update" and args.only_outdated:
        found = check_images(selected, check_all(selected), db=db)
        if db:
            for name, images in found.items():
                db.set_images(name, images)
        rule("New image versions")
        width = max(len(s.name) for s in selected)
        outdated: list[Path] = []
        for stack in selected:
            states = [i["status"] for i in found[stack.name]["images"]]
            n = sum(s in IMAGE_OUTDATED for s in states)
            if n:
                outdated.append(stack)
                say(f"  {yellow(SYM['up'])} {stack.name:<{width}}  {yellow(fmt_updates(n))}")
            elif "unknown" in states:
                say(f"  {yellow(SYM['warn'])} {stack.name:<{width}}  "
                    f"{yellow('could not be checked - skipped (run --healthcheck for details)')}")
            else:
                say(f"  {dim(SYM['skip'])} {stack.name:<{width}}  {dim('up to date - skipped')}")
        selected = outdated
        if not selected:
            if db:
                try:
                    db.save()
                except OSError:
                    pass
            say("\n" + green(f"{SYM['ok']} Nothing to update.") + "\n")
            return 0

    results: list[StackResult] = []
    interrupted = False
    run_start = time.monotonic()
    total = len(selected)
    for i, stack in enumerate(selected, 1):
        rule(f"[{i}/{total}] {stack.name}")
        start = time.monotonic()
        try:
            if action == "stop":
                res = stop_stack(stack, cfg, ui)
            elif action == "start":
                res = start_stack(stack, cfg, ui)
            elif action == "restart":
                res = restart_stack(stack, cfg, ui)
            elif action == "rollback":
                res = rollback_stack(stack, cfg, rollback_to, not args.skip_backup, ui)
            elif action == "update":
                res = update_stack(stack, cfg, do_backup, ui)
            else:
                raise ValueError(f"unknown action '{action}'")
        except KeyboardInterrupt:
            res = StackResult(stack.name, ok=False, status="interrupted")
            res.errors.append(Issue(stack.name, "-", "interrupted (Ctrl+C or terminated)"))
            interrupted = True
        res.seconds = time.monotonic() - start
        results.append(res)
        if db:
            try:
                snap = res.health or check_health(stack)
                db.set_health(stack.name, snap)
                db.set_action(res, action)
                if action == "update" and res.ok and (image_check or args.only_outdated) and not interrupted:
                    db.set_images(stack.name, check_images([stack], {stack.name: snap}, quiet=True, db=db)[stack.name])
                db.save()
            except OSError as e:
                res.warnings.append(Issue(stack.name, "Status file", e.strerror or str(e)))
            except KeyboardInterrupt:
                interrupted = True
        if interrupted:
            break

    pending = [s.name for s in selected[len(results):]]
    print_report(results, pending, action, time.monotonic() - run_start, interrupted)
    if db:
        say(dim(f"Status written to {db.path}") + "\n")
    del lock

    if interrupted:
        return 130
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say_error("Interrupted")
        sys.exit(130)
