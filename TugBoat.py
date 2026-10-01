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
import pwd
import re
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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

__title__ = "TugBoat"
__version__ = "0.2.1"
__author__ = "Henrik Isefjær Olsen"
__git__ = "https://github.com/hen-io/TugBoat"

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
            say(yellow(f"  {SYM['warn']} New config settings (defaults used until you add them): "
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


def startup_update_check(cfg: "Config", dry_run: bool) -> None:
    if not (cfg.update_check or cfg.auto_update) or os.environ.get(UPDATED_ENV):
        if os.environ.get(UPDATED_ENV):
            say(green(f"{SYM['ok']} Updated to {__title__} {__version__}"))
        return
    _, newer, _ = check_update(timeout=3)
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
    up_cmd: str
    down_cmd: str
    start_cmd: str
    restore_cmd: str
    ignore_folders: set[str]
    require_root: bool
    status_file: Path
    health_wait: int
    docker_user: str
    update_check: bool
    auto_update: bool
    image_check: bool
    command_timeout: int

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


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"{path.name} line {lineno}: expected 'key: value'")
        key, value = stripped.split(":", 1)
        value = _strip_comment(value).strip().strip('"').strip("'")
        raw[key.strip().lower()] = value

    required = ("container_path", "docker_stack_up_cmd", "docker_stack_down_cmd")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"Missing required config value(s): {', '.join(missing)}")

    def resolve(value: str | Path) -> Path:
        return path.parent / Path(value).expanduser()

    container_path = resolve(raw["container_path"])
    return Config(
        container_path=container_path,
        backup=_parse_bool(raw.get("backup", "true"), "backup"),
        backup_path=str(resolve(raw.get("backup_path") or container_path / ".backup" / STACK_PLACEHOLDER)),
        backup_retention=_parse_int(raw.get("backup_retention", "10"), "backup_retention"),
        up_cmd=raw["docker_stack_up_cmd"],
        down_cmd=raw["docker_stack_down_cmd"],
        start_cmd=raw.get("docker_stack_start_cmd") or "docker compose up -d",
        restore_cmd=raw.get("docker_stack_restore_cmd") or raw.get("docker_stack_start_cmd") or "docker compose up -d",
        ignore_folders={n.strip().strip("/") for n in raw.get("ignore_folders", "").split(",")
                        if n.strip()},
        require_root=_parse_bool(raw.get("require_root", "true"), "require_root"),
        docker_user=raw.get("docker_user", "").strip(),
        update_check=_parse_bool(raw.get("update_check", "true"), "update_check"),
        auto_update=_parse_bool(raw.get("auto_update", "true"), "auto_update"),
        status_file=resolve(raw.get("status_file") or container_path / "tugboat.json"),
        health_wait=_parse_int(raw.get("health_wait", "60"), "health_wait"),
        image_check=_parse_bool(raw.get("image_check", "true"), "image_check"),
        command_timeout=_parse_int(raw.get("command_timeout", "0"), "command_timeout"),
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
        if action == "stop" and len(idx) == len(stacks) and len(stacks) > 1:
            confirm = _prompt(f"Stop ALL {len(stacks)} stacks? [y/N] ")
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


def prune_work(stack: Path, cfg: Config) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
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
        old = backups[cfg.backup_retention:]
        failed: list[str] = []
        for _, d in old:
            emit(f"removing {d.name}")
            try:
                shutil.rmtree(d)
            except OSError as e:
                failed.append(f"{d}: {e}")
        kept = min(len(backups), cfg.backup_retention)
        if failed:
            return False, f"{len(failed)} old backup(s) could not be removed", failed
        removed = f"removed {len(old)}, " if old else ""
        return True, f"{removed}keeping {kept}/{cfg.backup_retention}", []
    return work


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _http_open(url: str, timeout: float, method: str = "GET", headers: dict | None = None):
    req = Request(url, method=method, headers={"User-Agent": f"{__title__}/{__version__}", **(headers or {})})
    return urlopen(req, timeout=timeout)


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
            return _manifest_digest(f"{scheme}://{host}/v2/{ref.repo}/manifests/{ref.tag}", ref, timeout)
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


def local_image(image: str) -> tuple[str, set[str]] | None:
    rc, out, err = docker(["image", "inspect", "--format", "{{.Id}}\t{{json .RepoDigests}}", image])
    if rc != 0:
        if "no such" in err.lower():
            return None
        raise RegistryError(_last_line(err, f"docker image inspect exit code {rc}"))
    image_id, _, digests = out.strip().partition("\t")
    try:
        repo_digests = json.loads(digests) or []
    except ValueError:
        repo_digests = []
    return image_id, {d.rpartition("@")[2] for d in repo_digests if "@" in d}


def inspect_image(image: str, built: bool) -> dict:
    info = {"status": "unknown", "local_digest": "", "remote_digest": "", "detail": "", "id": ""}
    try:
        local = local_image(image)
        ref = parse_image_ref(image)
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
        elif not local[1]:
            info.update(status="local", detail="not from a registry (built or loaded locally)")
        else:
            remote = remote_digest(ref)
            info["remote_digest"] = remote
            info["local_digest"] = remote if remote in local[1] else sorted(local[1])[0]
            info["status"] = "up_to_date" if remote in local[1] else "update_available"
        if local:
            info["id"] = local[0]
    except RegistryError as e:
        info["detail"] = str(e)
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


def check_images(stacks: list[Path], snaps: dict[str, dict], quiet: bool = False) -> dict[str, dict]:
    busy = IS_TTY and not quiet
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
        checked = dict(zip(wanted, pool.map(lambda image: inspect_image(image, wanted[image]), wanted)))
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
            images.append({k: info[k] for k in ("image", "services", "status", "local_digest",
                                                "remote_digest", "detail")})
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
            note = i["detail"] or f"{i['local_digest'][7:19]} {SYM['arrow']} {i['remote_digest'][7:19]}"
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
        self._changes: dict[str, dict] = {}
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


def fmt_ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        seconds = (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return ""
    if seconds < 90:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return ""


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


def run_healthcheck(selected: list[Path], db: StatusDB | None, image_check: bool) -> int:
    say("\n" + dim(" · ").join([bold("HEALTH CHECK"),
                                f"{len(selected)} stack{'s' if len(selected) > 1 else ''}",
                                datetime.now().strftime("%Y-%m-%d %H:%M")]))
    rule("Stacks")
    snaps = check_all(selected)
    images = check_images(selected, snaps) if image_check else None
    print_health_table(selected, snaps, db, images)
    bad = [(s.name, snaps[s.name]) for s in selected if snaps[s.name]["problems"]]
    if db:
        for name, snap in snaps.items():
            db.set_health(name, snap)
        for name, found in (images or {}).items():
            db.set_images(name, found)

    if bad:
        rule(f"Problems ({len(bad)})")
        for name, snap in bad:
            say(f"\n  {red(SYM['fail'])} {bold(name)}")
            for p in snap["problems"]:
                say(f"      {p}")
    if images is not None:
        print_image_report(selected, images)
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


def start_stack(stack: Path, cfg: Config, ui: Runner) -> StackResult:
    res = StackResult(stack.name)
    if ui.dry_run:
        ui.dry("Start", f"would run: {cfg.start_cmd}")
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would start"
        return res
    out = ui.step("Start", command_work(cfg.start_cmd, stack), timeout=cfg.command_timeout)
    if not out.ok:
        return _fail(res, "Start", out, "start failed")
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
        ui.dry("Stop", f"would run: {cfg.down_cmd}")
        res.status = "would stop"
        return res
    out = ui.step("Stop", command_work(cfg.down_cmd, stack), timeout=cfg.command_timeout)
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed")
    res.status = "stopped"
    return res


def _restart_old(stack: Path, cfg: Config, ui: Runner, res: StackResult, what: str) -> StackResult:
    restart = ui.step("Restart old version", command_work(cfg.restore_cmd, stack), timeout=cfg.command_timeout)
    if restart.ok:
        res.status = f"{what} failed, old version restarted"
    else:
        _fail(res, "Restart old version", restart, f"{what} AND restart failed - stack is DOWN")
    return res


def update_stack(stack: Path, cfg: Config, do_backup: bool, ui: Runner) -> StackResult:
    res = StackResult(stack.name)

    if do_backup and backup_inside_stack(stack, cfg):
        note = f"backup_path {cfg.backup_root(stack.name)} is inside the stack folder"
        ui._line("fail", "Backup", None, red(note))
        res.ok, res.status = False, "not updated"
        res.errors.append(Issue(res.name, "Backup", note, ["Change backup_path in TugBoat.conf."]))
        return res

    if ui.dry_run:
        ui.dry("Stop", f"would run: {cfg.down_cmd}")
        if do_backup:
            ui.dry("Backup", f"would copy to {cfg.backup_root(stack.name)}/<timestamp>")
            ui.dry("Prune backups", f"would keep newest {cfg.backup_retention}")
        else:
            ui.skip("Backup", "skipped")
        ui.dry("Pull & start", f"would run: {cfg.up_cmd}")
        ui.dry("Health check", f"would wait up to {cfg.health_wait}s for healthy containers")
        res.status = "would update"
        return res

    out = ui.step("Stop", command_work(cfg.down_cmd, stack), timeout=cfg.command_timeout)
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
        if cfg.backup_retention > 0:
            out = ui.step("Prune backups", prune_work(stack, cfg), warn_only=True)
            if not out.ok:
                res.warnings.append(Issue(res.name, "Prune backups", out.note, out.details, out.output))
        else:
            ui.skip("Prune backups", "retention 0, keeping all")
    else:
        ui.skip("Backup", "skipped")

    out = ui.step("Pull & start", command_work(cfg.up_cmd, stack), timeout=cfg.command_timeout)
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
    mode.add_argument("--start", nargs="*", metavar="STACK", help="start (docker_stack_start_cmd)")
    mode.add_argument("--stop", nargs="*", metavar="STACK", help="stop (docker_stack_down_cmd)")
    mode.add_argument("--healthcheck", nargs="*", metavar="STACK",
                      help="check status/health and new image versions, write the status file "
                           "(all stacks if no names)")
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
    parser.add_argument("--skip-backup", action="store_true", help="skip the backup step (update only)")
    parser.add_argument("--no-image-check", action="store_true",
                        help="do not ask the registries for new image versions")
    parser.add_argument("-v", "--verbose", action="store_true", help="show full command output live")
    parser.add_argument("--dry-run", action="store_true", help="show actions without running them")
    parser.add_argument("--version", action="version",
                        version=f"{__title__} {__version__} - {__author__} - {__git__}")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _on_sigterm)

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
            ensure_root(non_interactive=args.auto)
        banner()
        user_warning = setup_docker_user(cfg.docker_user)
        if user_warning:
            say(yellow(f"{SYM['warn']} {user_warning}"))
        startup_update_check(cfg, args.dry_run)
        stacks = find_stacks(cfg.container_path, cfg.ignore_folders)
    except (OSError, ValueError) as e:
        say_error(str(e))
        return 2

    if not stacks:
        say(yellow(f"No stacks found in {cfg.container_path}"))
        return 0

    action: str | None = None
    raw_names: list[str] = list(args.stack)
    for a in ("update", "start", "stop", "healthcheck"):
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

    db: StatusDB | None = None
    if not args.dry_run:
        db = StatusDB(cfg.status_file)
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

    image_check = cfg.image_check and not args.no_image_check
    if action == "healthcheck":
        return run_healthcheck(selected, db, image_check)

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
    if cfg.docker_user:
        plan.append(f"docker as {cfg.docker_user}")
    plan.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    if args.auto:
        plan.append("auto")
    if args.dry_run:
        plan.append(cyan("DRY RUN"))
    say("\n" + dim(" · ").join(plan))

    if action == "update" and args.only_outdated:
        found = check_images(selected, check_all(selected))
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
            else:
                res = update_stack(stack, cfg, do_backup, ui)
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
                    db.set_images(stack.name, check_images([stack], {stack.name: snap}, quiet=True)[stack.name])
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
