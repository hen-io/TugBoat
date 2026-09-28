#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__title__ = "TugBoat"
__version__ = "0.1.7"
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
    print(red(f"{SYM['fail']} {msg}"), flush=True)


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

    def step(self, label: str, work: StepWork, warn_only: bool = False) -> StepOutcome:
        output: list[str] = []
        result: dict = {}

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
                if IS_TTY and not self.verbose:
                    elapsed = fmt_time(time.monotonic() - start)
                    last = output[-1].strip() if output else ""
                    room = max(0, term_width() - 36)
                    write(f"\r\033[K  {cyan(SPINNER[frame % len(SPINNER)])} "
                                     f"{label:<20} {dim(f'{elapsed:>7}')}  {dim(last[:room])}")
                    frame += 1
                t.join(0.1)
        finally:
            if IS_TTY and not self.verbose:
                write("\r\033[K")

        ok, note, details = result.get("r", (False, "no result", []))
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
                                text=True, errors="replace", bufsize=1, **DOCKER_RUN_AS)
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(line.rstrip("\n").split("\r")[-1])
        rc = proc.wait()
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
    except (URLError, OSError, TimeoutError) as e:
        raise RuntimeError(f"could not reach GitHub: {getattr(e, 'reason', e)}")
    except json.JSONDecodeError:
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
        text = raw.decode("utf-8")
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError) as e:
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
        local = set(_conf_keys(CONFIG_FILE.read_text())) if CONFIG_FILE.is_file() else set()
        new_keys = [k for k in _conf_keys(example) if k not in local]
        if new_keys:
            say(yellow(f"  {SYM['warn']} New config settings (defaults used until you add them): "
                       f"{', '.join(new_keys)}"))
            say(dim(f"    see {__git__}/blob/{rel['tag']}/TugBoat.conf"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError):
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
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
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

    container_path = Path(raw["container_path"]).expanduser()
    return Config(
        container_path=container_path,
        backup=_parse_bool(raw.get("backup", "true"), "backup"),
        backup_path=raw.get("backup_path", str(container_path / ".backup" / STACK_PLACEHOLDER)),
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
        status_file=Path(raw.get("status_file") or container_path / "tugboat.json").expanduser(),
        health_wait=_parse_int(raw.get("health_wait", "60"), "health_wait"),
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


def ask_for_stacks(stacks: list[Path], action: str, snaps: dict[str, dict] | None = None) -> list[Path]:
    title = "check" if action == "healthcheck" else action
    say("\n" + bold(f"Stacks to {title.upper()}"))
    width = max(len(s.name) for s in stacks)
    for i, s in enumerate(stacks, 1):
        snap = (snaps or {}).get(s.name)
        if snap:
            _menu_item(str(i), f"{s.name:<{width}}  {HEALTH_STYLE[snap['health']][1](snap['health'])}")
        else:
            _menu_item(str(i), s.name)
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


def backup_work(stack: Path, dest: Path) -> StepWork:
    def work(emit: Callable[[str], None]) -> tuple[bool, str, list[str]]:
        emit(f"copying {stack} -> {dest}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stack, dest, symlinks=True)
            try:
                shown = str(dest.relative_to(stack.parent))
            except ValueError:
                shown = str(dest)
            return True, f"{SYM['arrow']} {shown}", []
        except shutil.Error as e:
            failures = e.args[0] if e.args and isinstance(e.args[0], list) else []
            details = [f"{src}: {str(reason).split(']')[-1].split(':')[0].strip()}"
                       for src, _, reason in failures[:10]]
            if len(failures) > 10:
                details.append(f"... and {len(failures) - 10} more")
            note = f"{len(failures)} file(s) could not be copied"
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


def compose_ps(stack: Path) -> tuple[list[dict] | None, str]:
    try:
        p = subprocess.run(["docker", "compose", "ps", "--all", "--format", "json"], cwd=stack,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                           **DOCKER_RUN_AS)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip().splitlines()
        return None, msg[-1] if msg else f"docker compose ps exit code {p.returncode}"
    text = p.stdout.strip()
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
        try:
            p = subprocess.run(["docker", "compose", "logs", "--no-color", "--tail", str(tail), service],
                               cwd=stack, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                               **DOCKER_RUN_AS)
            lines.append(f"-- logs: {service} (last {tail} lines) --")
            lines += [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
        except (OSError, subprocess.TimeoutExpired) as e:
            lines.append(f"-- logs: {service}: {e}")
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
            time.sleep(3)
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


class StatusDB:

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"stacks": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict) and isinstance(loaded.get("stacks"), dict):
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                broken = path.with_name(path.name + ".broken")
                say(yellow(f"{path.name} could not be read - starting fresh (old file kept as {broken.name})"))
                try:
                    path.replace(broken)
                except OSError:
                    pass

    def stack(self, name: str) -> dict:
        return self.data["stacks"].setdefault(name, {})

    def set_health(self, name: str, snapshot: dict) -> None:
        entry = self.stack(name)
        entry.update({k: snapshot[k] for k in ("health", "summary", "problems", "checked_at", "containers")})

    def set_action(self, res: StackResult, action: str) -> None:
        entry = self.stack(res.name)
        entry["last_action"] = {
            "action": action,
            "ok": res.ok,
            "result": res.status,
            "at": now_iso(),
            "duration_s": round(res.seconds, 1),
            "errors": [f"{e.step}: {e.message}" for e in res.errors],
            "warnings": [f"{w.step}: {w.message}" for w in res.warnings],
        }
        if res.backup:
            entry["last_backup"] = res.backup
        if action == "update" and res.ok:
            entry["last_update"] = now_iso()

    def forget_missing(self, existing: list[Path]) -> None:
        names = {s.name for s in existing}
        for name in list(self.data["stacks"]):
            if name not in names:
                del self.data["stacks"][name]

    def save(self) -> None:
        self.data["tugboat_version"] = __version__
        self.data["written_at"] = now_iso()
        self.data["stacks"] = dict(sorted(self.data["stacks"].items(), key=lambda kv: kv[0].lower()))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.path)


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


def print_health_table(stacks: list[Path], snaps: dict[str, dict], db: StatusDB | None) -> None:
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
        try:
            for name, snap in snaps.items():
                db.set_health(name, snap)
            db.save()
        except OSError:
            pass
    return snaps


def run_healthcheck(selected: list[Path], db: StatusDB | None) -> int:
    say("\n" + dim(" · ").join([bold("HEALTH CHECK"),
                                f"{len(selected)} stack{'s' if len(selected) > 1 else ''}",
                                datetime.now().strftime("%Y-%m-%d %H:%M")]))
    rule("Stacks")
    snaps = check_all(selected)
    print_health_table(selected, snaps, db)
    bad = [(s.name, snaps[s.name]) for s in selected if snaps[s.name]["problems"]]
    if db:
        for name, snap in snaps.items():
            db.set_health(name, snap)

    if bad:
        rule(f"Problems ({len(bad)})")
        for name, snap in bad:
            say(f"\n  {red(SYM['fail'])} {bold(name)}")
            for p in snap["problems"]:
                say(f"      {p}")
    if db:
        db.save()
        say("\n" + dim(f"Status written to {db.path}"))
    say()
    return 1 if bad else 0


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
    out = ui.step("Start", command_work(cfg.start_cmd, stack))
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
    out = ui.step("Stop", command_work(cfg.down_cmd, stack))
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed")
    res.status = "stopped"
    return res


def update_stack(stack: Path, cfg: Config, do_backup: bool, ui: Runner) -> StackResult:
    res = StackResult(stack.name)

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

    out = ui.step("Stop", command_work(cfg.down_cmd, stack))
    if not out.ok:
        return _fail(res, "Stop", out, "stop failed, left as is")

    if do_backup:
        dest = new_backup_dest(stack, cfg)
        out = ui.step("Backup", backup_work(stack, dest))
        if out.ok:
            res.backup = str(dest)
        if not out.ok:
            _fail(res, "Backup", out, "backup failed")
            restart = ui.step("Restart old version", command_work(cfg.restore_cmd, stack))
            if restart.ok:
                res.status = "backup failed, old version restarted"
            else:
                _fail(res, "Restart old version", restart, "backup AND restart failed - stack is DOWN")
            return res
        if cfg.backup_retention > 0:
            out = ui.step("Prune backups", prune_work(stack, cfg), warn_only=True)
            if not out.ok:
                res.warnings.append(Issue(res.name, "Prune backups", out.note, out.details, out.output))
        else:
            ui.skip("Prune backups", "retention 0, keeping all")
    else:
        ui.skip("Backup", "skipped")

    out = ui.step("Pull & start", command_work(cfg.up_cmd, stack))
    if not out.ok:
        return _fail(res, "Pull & start", out, "update/start failed - check the stack")

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


def main() -> int:
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
                      help="check status/health and write the status file (all stacks if no names)")
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
    parser.add_argument("--skip-backup", action="store_true", help="skip the backup step (update only)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show full command output live")
    parser.add_argument("--dry-run", action="store_true", help="show actions without running them")
    parser.add_argument("--version", action="version",
                        version=f"{__title__} {__version__} - {__author__} - {__git__}")
    args = parser.parse_args()

    try:
        cfg = load_config(CONFIG_FILE)
        if cfg.require_root and not args.dry_run:
            ensure_root(non_interactive=args.auto)
        banner()
        user_warning = setup_docker_user(cfg.docker_user)
        if user_warning:
            say(yellow(f"{SYM['warn']} {user_warning}"))
        if args.check_update:
            return run_check_update()
        if args.self_update:
            return run_self_update(args.dry_run)
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

    db: StatusDB | None = None
    if not args.dry_run:
        db = StatusDB(cfg.status_file)
        db.forget_missing(stacks)

    if names and args.all:
        say_error("Give stack names or --all, not both.")
        return 2

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
        selected = ask_for_stacks(stacks, action, snaps)
    if not selected:
        say(dim("Nothing selected, exiting."))
        return 0

    if action == "healthcheck":
        return run_healthcheck(selected, db)

    do_backup = cfg.backup and not args.skip_backup
    ui = Runner(verbose=args.verbose, dry_run=args.dry_run)

    plan = [bold(action.upper()), f"{len(selected)} stack{'s' if len(selected) > 1 else ''}"]
    if action == "update":
        plan.append(f"backup {'on' if do_backup else yellow('off')}")
    if cfg.docker_user:
        plan.append(f"docker as {cfg.docker_user}")
    plan.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    if args.auto:
        plan.append("auto")
    if args.dry_run:
        plan.append(cyan("DRY RUN"))
    say("\n" + dim(" · ").join(plan))

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
            res.errors.append(Issue(stack.name, "-", "interrupted by user (Ctrl+C)"))
            interrupted = True
        res.seconds = time.monotonic() - start
        results.append(res)
        if db:
            try:
                db.set_health(stack.name, res.health or check_health(stack))
                db.set_action(res, action)
                db.save()
            except OSError as e:
                res.warnings.append(Issue(stack.name, "Status file", str(e)))
        if interrupted:
            break

    pending = [s.name for s in selected[len(results):]]
    print_report(results, pending, action, time.monotonic() - run_start, interrupted)
    if db:
        say(dim(f"Status written to {db.path}") + "\n")

    if interrupted:
        return 130
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())