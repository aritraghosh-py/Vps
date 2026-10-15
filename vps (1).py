"""
Cloud Runner Panel - aiogram 3.31+ edition.

Uses Telegram Rich Messages (Bot API 10.1+): headings 1-6, tables, quotes, footers,
checklists, collapsible blocks - and coloured inline buttons (Bot API 9.4+ `style`).
Colour rules: blue = navigation/primary, green = run/create/confirm-safe, red = stop/delete.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import sys
import time
import zipfile
from collections import OrderedDict

import aiogram  # noqa: E402
import psutil  # noqa: E402
from aiogram import Bot, Dispatcher, F, Router  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402
from aiogram.client.telegram import TelegramAPIServer  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter  # noqa: E402
from aiogram.filters import Command, CommandObject  # noqa: E402
from aiogram.filters.callback_data import CallbackData  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.types import (  # noqa: E402
    BotCommand, CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

import db  # noqa: E402
import richui as ui  # noqa: E402
from richui import Raw, Rich, code, esc, mention  # noqa: E402

log = logging.getLogger("runner")

# ============================================================================ #
# CONFIG - edit these values directly
# ============================================================================ #
BOT_TOKEN = "8722153642:AAHtNloo9mqdFJtb9hTIUSm9dJjg9-MgDXA"      # from @BotFather (revoke the old leaked one!)
ADMIN_IDS = {5716292610}                         # admins: /backup, /dashboard, /broadcast
ALLOWED_USERS: set[int] = set()                  # empty = everyone may use the bot
BACKUP_CHANNEL_ID = "@backup_channel_for_scriptcloud"
BACKUP_INTERVAL_H = 12                           # hours between auto-backups
RESTORE_MODE = "auto"                            # auto = only on a fresh VPS | always | never
LOCAL_API = ""                                   # e.g. "http://127.0.0.1:8081" to lift 20/50 MB limits
BASE_DIR = "projects"
STATE_FILE = "active_runs.json"
MB = 1024 * 1024
MAX_UPLOAD = (2000 if LOCAL_API else 50) * MB          # Bot API: 50 MB up
MAX_DOWNLOAD = (2000 if LOCAL_API else 20) * MB        # Bot API: 20 MB down
PAGE = 20
HIDDEN = {"venv", "__pycache__", ".git"}
KEEP_HIDDEN_EXT = (".session", ".session-journal", ".env")
LOG_MAX_BYTES = 2 * MB                           # a run.log above this is trimmed ...
LOG_KEEP = 256 * 1024                            # ... down to its last 256 KB (newest lines are kept)
PIP_CACHE = os.path.expanduser("~/.cache/pip")
PIP_ENV = {"PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_PROGRESS_BAR": "off"}
RESUME_GAP_S = 3                                 # boot: pause between auto-resumed scripts
RESUME_MAX_CPU = 80                              # boot: wait for CPU below this % before launching the next script
RESUME_MIN_FREE_MB = 200                         # boot: ...and for at least this much available RAM
MAX_SCRIPT_MB = 0                                # memory guard: stop a script above this RSS (0 = off, e.g. 512)

session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API, is_local=True)) if LOCAL_API else None
bot = Bot(
    BOT_TOKEN, session=session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
)
dp = Dispatcher()
router = Router(name="runner")
dp.include_router(router)

# ============================================================================ #
# STATE
# ============================================================================ #
BOT_START_TIME = time.time()
ACTIVE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}   # "uid:project:rel" -> process
RUNNING_SCRIPTS: dict[str, str] = {}                           # key -> "project/rel"
STARTING: set[str] = set()
START_TIMES: dict[str, float] = {}
TASKS: set[asyncio.Task] = set()                               # strong refs: tasks can't be GC'd mid-run
SHUTTING_DOWN = False


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return task


PENDING_RESUME: dict[str, str] = {}                            # scripts waiting for their turn after a reboot


def save_running_state() -> None:
    state = {**PENDING_RESUME, **RUNNING_SCRIPTS}       # pending ones stay saved if the bot dies mid-boot
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)           # atomic: never leaves a half-written file
    except Exception as exc:
        log.error("Error saving running state: %s", exc)


def load_running_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ============================================================================ #
# HELPERS
# ============================================================================ #
def user_dir(uid) -> str: return os.path.join(BASE_DIR, f"user_{uid}")
def project_dir(uid, proj) -> str: return os.path.join(user_dir(uid), proj)
def venv_python(pdir) -> str: return os.path.abspath(os.path.join(pdir, "venv", "bin", "python"))


def valid_project(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name or "")) and name not in (".", "..")


def safe_join(base: str, rel: str) -> str | None:
    """Join and make sure the result stays inside base (blocks ../ and symlink escapes)."""
    base_r = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_r, rel))
    return target if target == base_r or target.startswith(base_r + os.sep) else None


def visible(name: str) -> bool:
    if name in HIDDEN:
        return False
    return not name.startswith(".") or name.endswith(KEEP_HIDDEN_EXT)


def format_size(n: int) -> str:
    if n < 1024: return f"{n} B"
    if n < MB: return f"{n / 1024:.1f} KB"
    if n < 1024 * MB: return f"{n / MB:.1f} MB"
    return f"{n / (1024 * MB):.2f} GB"


def format_uptime(seconds: float) -> str:
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def bar(pct: float, length: int = 10) -> str:
    filled = int(max(0, min(100, pct)) / 100 * length)
    return "▰" * filled + "▱" * (length - filled)


def file_icon(name: str) -> str:
    low = name.lower()
    ext = low.rsplit(".", 1)[-1] if "." in low else ""
    if ext == "py": return "🐍"
    if ext == "log": return "📜"
    if ext == "env": return "🔑"
    if low.endswith((".session", ".session-journal")): return "📦"
    if ext in ("json", "yaml", "yml", "toml"): return "⚙️"
    if ext in ("zip", "rar", "tar", "gz"): return "🗜️"
    return "📄"


def list_dir(path: str) -> list[tuple[str, bool]]:
    out = []
    with contextlib.suppress(OSError):
        with os.scandir(path) as it:
            for e in it:
                if visible(e.name):
                    out.append((e.name, e.is_dir(follow_symlinks=False)))
    out.sort(key=lambda t: (not t[1], t[0].lower()))
    return out


def project_names(uid) -> list[str]:
    return sorted(n for n, is_dir in list_dir(user_dir(uid)) if is_dir)


def tail_text(path: str, lines: int = 25, max_chars: int = 3500) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 64 * 1024))
        data = f.read().decode("utf-8", "replace")
    text = "\n".join(data.splitlines()[-lines:])
    return text[-max_chars:] if text.strip() else ""


def is_running(key: str) -> bool:
    p = ACTIVE_PROCESSES.get(key)
    return p is not None and p.returncode is None


def running_keys(uid) -> list[str]:
    return [k for k in list(ACTIVE_PROCESSES) if k.startswith(f"{uid}:") and is_running(k)]


def get_running_scripts(uid) -> list[str]:
    return [RUNNING_SCRIPTS[k] for k in running_keys(uid) if k in RUNNING_SCRIPTS]


def paginate(items: list, pg: int, size: int = PAGE):
    pages = max(1, -(-len(items) // size))
    pg = max(0, min(pg, pages - 1))
    return items[pg * size:(pg + 1) * size], pg, pages


# ---- callback_data: 64-byte limit workaround (long values become short tokens) ----
_REFS: OrderedDict[str, str] = OrderedDict()


_PENDING: dict[str, str] = {}
_flusher: asyncio.Task | None = None


async def _flush_pending() -> None:
    await asyncio.sleep(0.3)                       # let a whole keyboard render, then write once, sequentially
    while _PENDING:
        token, value = _PENDING.popitem()
        with contextlib.suppress(Exception):
            await db.acache_set("cb:" + token, value, 7 * 86400)


def _persist(token: str, value: str) -> None:
    global _flusher
    _PENDING[token] = value
    if _flusher is None or _flusher.done():
        with contextlib.suppress(RuntimeError):
            _flusher = spawn(_flush_pending())


def R(value: str) -> str:
    if len(value.encode()) <= 24 and "|" not in value and not value.startswith("~"):
        return value
    token = "~" + hashlib.blake2s(value.encode(), digest_size=6).hexdigest()
    if token not in _REFS:
        _REFS[token] = value
        if len(_REFS) > 5000:
            _REFS.popitem(last=False)
        _persist(token, value)
    return token


async def D(token: str) -> str | None:
    if not token.startswith("~"):
        return token
    return _REFS.get(token) or await db.acache_get("cb:" + token)


# ---- callback data classes (separator "|" so ":" in names is safe) ----
class Menu(CallbackData, prefix="m", sep="|"):
    a: str
    pg: int = 0


class Proj(CallbackData, prefix="p", sep="|"):
    n: str


class Fx(CallbackData, prefix="x", sep="|"):      # explorer inside the active project
    a: str
    r: str = "."
    pg: int = 0


class Mf(CallbackData, prefix="f", sep="|"):      # /myfiles manager
    a: str
    p: str = ""
    f: str = ""
    pg: int = 0


class Kill(CallbackData, prefix="k", sep="|"):
    p: str
    r: str


class Log(CallbackData, prefix="l", sep="|"):
    a: str
    f: str = "run.log"


class Fa(CallbackData, prefix="a", sep="|"):      # file actions (works for explorer + /myfiles)
    a: str
    p: str
    r: str
    m: str = "x"          # x = explorer, m = /myfiles
    pg: int = 0


class Own(CallbackData, prefix="o", sep="|"):
    a: str
    k: str = ""
    pg: int = 0


class Dash(CallbackData, prefix="d", sep="|"):
    v: str
    ref: bool = False


# ---- coloured buttons ("smart" palette) ----
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


def btn(text: str, data: str, style: str | None = None, raw: bool = False) -> InlineKeyboardButton:
    """Buttons carry no emojis: labels are stripped (raw=True keeps real file/project names untouched)."""
    if not raw:
        text = _EMOJI.sub("", text).strip()
    kw = {"style": style} if style else {}
    return InlineKeyboardButton(text=text, callback_data=data, **kw)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def nav_row(pg: int, pages: int, make) -> list:
    if pages <= 1:
        return []
    row = []
    if pg > 0:
        row.append(btn("⬅️ Prev", make(pg - 1)))
    row.append(btn(f"{pg + 1}/{pages}", make(pg)))
    if pg < pages - 1:
        row.append(btn("Next ➡️", make(pg + 1)))
    return row


async def answer(cb: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    with contextlib.suppress(TelegramAPIError):
        await cb.answer(text, show_alert=alert)


async def present(event, view, embed: bool = False) -> None:
    """embed=True renders the buttons inside the rich message (Bot API 10.3) instead of under it."""
    rich, markup = view
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            await ui.edit(bot, event.message, rich, markup, embed)
    else:
        await ui.send(bot, event.chat.id, rich, markup, embed)


# ---- rich wrappers: every bot message goes through richui (native rich message, HTML fallback) ----
_PRE = re.compile(r"(<pre>.*?</pre>)", re.S)


def rich_text(text: str) -> str:
    """Turn a classic-HTML notice (inline tags + newlines) into rich-message blocks."""
    out: list[str] = []
    for part in _PRE.split(text):
        if part.startswith("<pre>"):
            out.append(part)
        else:
            out.extend(f"<p>{ln}</p>" for ln in part.split("\n") if ln.strip())
    return "".join(out)


async def say(chat_id, text: str, markup=None) -> Message | None:
    try:
        return await ui.send(bot, chat_id, rich_text(text), markup)
    except TelegramAPIError as exc:
        log.warning("say() failed: %s", exc)
        return None


async def reply(event: Message, text: str, markup=None) -> Message:
    return await ui.send(bot, event.chat.id, rich_text(text), markup)


async def edit_msg(msg: Message, text: str, markup=None) -> bool:
    return await ui.edit(bot, msg, rich_text(text), markup)


def user_message(event) -> Message | None:
    return event.message if isinstance(event, CallbackQuery) and isinstance(event.message, Message) else None


async def send_file(cb: CallbackQuery, path: str, chat_id) -> None:
    """Send a file as a document and always tell the user what happened (no silent failures)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return await answer(cb, "❌ File not found.", True)
    if size == 0:
        return await answer(cb, "⚠️ File is empty — Telegram can't send empty files.", True)
    if size > MAX_UPLOAD:
        return await answer(cb, f"⚠️ {format_size(size)} is over the {format_size(MAX_UPLOAD)} Bot API limit.", True)
    await answer(cb, "📥 Sending…")
    try:
        await bot.send_document(chat_id, FSInputFile(path), request_timeout=600)
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        with contextlib.suppress(TelegramAPIError):
            await bot.send_document(chat_id, FSInputFile(path), request_timeout=600)
    except Exception as exc:                                  # noqa: BLE001 - report every failure to the user
        log.warning("download failed for %s: %s", path, exc)
        await say(chat_id, f"❌ <b>Download failed:</b> <code>{esc(str(exc)[:300])}</code>")


# ---- zip helpers ----
def safe_extract(zip_path: str, dest: str) -> None:
    dest_r = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if sum(x.file_size for x in infos) > 2048 * MB:
            raise ValueError("archive too large when extracted (>2 GB)")
        for x in infos:
            t = os.path.realpath(os.path.join(dest_r, x.filename))
            if not (t == dest_r or t.startswith(dest_r + os.sep)):
                raise ValueError(f"unsafe path in archive: {x.filename}")
        z.extractall(dest_r)


def zip_dir(src: str, zip_path: str, extra_files: list[tuple[str, str]] | None = None, prefix_root: bool = False,
            lean: bool = False) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=3 if lean else 6) as z:
        for path, arc in extra_files or []:
            if os.path.exists(path):
                z.write(path, arc)
        if os.path.isdir(src):
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in HIDDEN]
                for fn in files:
                    if lean and (fn == "run.log" or fn.endswith(".pyc")):
                        continue                  # logs and bytecode are never worth backing up
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, "." if prefix_root else src)
                    with contextlib.suppress(OSError):
                        z.write(full, arc)


# ============================================================================ #
# SUBPROCESS ENGINE
# ============================================================================ #
def _signal_group(proc, sig) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)           # processes start in their own session/group


async def run_cmd(*args, cwd=None, timeout=900, env=None) -> tuple[int, str]:
    """Run a helper command with a hard timeout so nothing can hang the panel."""
    env = {**os.environ, **PIP_ENV, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")[-1500:]


async def stop_process(key: str) -> str:
    proc = ACTIVE_PROCESSES.pop(key, None)
    name = RUNNING_SCRIPTS.pop(key, key)
    START_TIMES.pop(key, None)
    PENDING_RESUME.pop(key, None)
    save_running_state()
    if proc and proc.returncode is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 3)
    return name


def _deprioritize(pid: int) -> None:
    """Scripts must never starve the panel: lower CPU priority + first pick for the OOM killer."""
    with contextlib.suppress(Exception):
        psutil.Process(pid).nice(10)
    with contextlib.suppress(OSError):
        with open(f"/proc/{pid}/oom_score_adj", "w") as f:
            f.write("500")


_ENV_SEM: asyncio.Semaphore | None = None


def env_lock() -> asyncio.Semaphore:
    """At most 2 venv/pip jobs at a time (created lazily so it binds to the running loop)."""
    global _ENV_SEM
    if _ENV_SEM is None:
        _ENV_SEM = asyncio.Semaphore(2)
    return _ENV_SEM


MODULE_TO_PIP = {
    "dotenv": "python-dotenv", "bs4": "beautifulsoup4", "cv2": "opencv-python", "yaml": "pyyaml",
    "PIL": "Pillow", "telebot": "pyTelegramBotAPI", "sklearn": "scikit-learn", "Crypto": "pycryptodome",
    "dateutil": "python-dateutil", "telethon": "telethon", "pyrogram": "pyrofork", "git": "GitPython",
}


def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.blake2s(f.read()).hexdigest()
    except OSError:
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_text(path: str, text: str | None) -> None:
    if text:
        with contextlib.suppress(OSError):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)


async def ensure_env(chat_id, pdir: str) -> None:
    """Create the venv if missing. Requirements are installed only when requirements.txt changed
    (its hash is cached inside the venv), so a normal restart skips pip entirely."""
    venv = os.path.join(pdir, "venv")
    pip = [venv_python(pdir), "-m", "pip"]
    if not os.path.exists(venv):
        m = await say(chat_id, "⚙️ Rebuilding environment…")
        await run_cmd(sys.executable, "-m", "venv", venv, timeout=300)
        if m:
            with contextlib.suppress(TelegramAPIError):
                await m.delete()
    req = os.path.join(pdir, "requirements.txt")
    stamp = os.path.join(venv, ".req_hash")
    m = None
    if os.path.exists(req):
        digest = await asyncio.to_thread(_file_digest, req)
        if digest and digest == _read_text(stamp):
            return                                           # unchanged -> fast start
        m = await say(chat_id, "📦 <code>requirements.txt</code> found — installing…")
        rc, out = await run_cmd(*pip, "install", "-r", req, cwd=pdir)
        if rc != 0:
            await say(chat_id, f"⚠️ Some requirements failed:\n<pre>{esc(out[-700:])}</pre>")
        else:
            _write_text(stamp, digest)
    else:
        m = await say(chat_id, "🔍 No <code>requirements.txt</code> — scanning imports…")
        pipreqs = os.path.abspath(os.path.join(pdir, "venv", "bin", "pipreqs"))
        if not os.path.exists(pipreqs):
            await run_cmd(*pip, "install", "pipreqs", cwd=pdir)
        await run_cmd(pipreqs, pdir, "--force", "--ignore", "venv", timeout=300)
        await run_cmd(*pip, "uninstall", "-y", "pipreqs", "yarg", "docopt", cwd=pdir)   # keep the venv small
        if os.path.exists(req):
            rc, _ = await run_cmd(*pip, "install", "-r", req, cwd=pdir)
            if rc == 0:
                _write_text(stamp, await asyncio.to_thread(_file_digest, req))
    if m:
        with contextlib.suppress(TelegramAPIError):
            await m.delete()


async def start_process(chat_id, uid, project: str, rel: str, attempted: set | None = None,
                        quiet: bool = False) -> None:
    attempted = attempted if attempted is not None else set()
    key = f"{uid}:{project}:{rel}"
    pdir = project_dir(uid, project)
    entry = safe_join(pdir, rel)
    if not entry or not os.path.isfile(entry):
        return void(await say(chat_id, f"❌ <code>{esc(rel)}</code> not found in <code>{esc(project)}</code>."))
    if is_running(key) or key in STARTING:
        return void(await say(chat_id, f"⚠️ <code>{esc(rel)}</code> is already running in this project!"))
    STARTING.add(key)
    try:
        async with env_lock():
            await ensure_env(chat_id, pdir)
        env = {k: v for k, v in os.environ.items() if k != "BOT_TOKEN"}   # scripts never see the panel token
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"      # no __pycache__ written next to your code
        env["MALLOC_ARENA_MAX"] = "2"             # glibc: far less RAM per Python process
        env.update(PIP_ENV)
        proc = await asyncio.create_subprocess_exec(
            venv_python(pdir), "-u", entry, cwd=os.path.abspath(pdir), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=1 << 18,
        )
        _deprioritize(proc.pid)
        ACTIVE_PROCESSES[key] = proc
        RUNNING_SCRIPTS[key] = f"{project}/{rel}"
        START_TIMES[key] = time.time()
        save_running_state()
        spawn(read_stdout(proc, chat_id, uid, project, rel, attempted))
    except Exception as exc:
        log.exception("start failed")
        return void(await say(chat_id, f"❌ Could not start <code>{esc(rel)}</code>:\n<pre>{esc(exc)}</pre>"))
    finally:
        STARTING.discard(key)

    if quiet:
        return
    r = (Rich().h(3, "▶️ Started").p(code(f"{project}/{rel}"))
         .footer(f"PID {proc.pid} · logs are recorded silently — tap Refresh to view"))
    await ui.send(bot, chat_id, r, kb(
        [btn("🔄 Refresh Live Logs", Log(a="r", f="run.log").pack(), PRIMARY)],
        [btn("🛑 Stop Process", Kill(p=R(project), r=R(rel)).pack(), DANGER)],
    ))


def void(_=None) -> None:
    return None


async def read_stdout(proc, chat_id, uid, project, rel, attempted: set) -> None:
    key = f"{uid}:{project}:{rel}"
    pdir = project_dir(uid, project)
    log_path = os.path.join(pdir, "run.log")
    failed_notified: set[str] = set()
    written = 0
    lf = open(log_path, "a", encoding="utf-8")        # block-buffered; flushed on a short timer below
    loop = asyncio.get_running_loop()
    flush_h = None

    def _flush() -> None:
        nonlocal flush_h
        flush_h = None
        with contextlib.suppress(Exception):
            lf.flush()

    try:
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:            # absurdly long line: skip it, keep going
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            lf.write(line + "\n")
            if flush_h is None:
                flush_h = loop.call_later(0.5, _flush)
            written += 1
            if written % 500 == 0 and os.path.getsize(log_path) > LOG_MAX_BYTES:
                lf.flush()
                await asyncio.to_thread(_trim_log, log_path, LOG_KEEP)   # keep the newest lines

            m = re.search(r"No module named '([^']+)'", line)
            if not m:
                continue
            module = m.group(1).split(".")[0]
            if module in attempted:
                if module not in failed_notified:
                    failed_notified.add(module)
                    await say(chat_id, f"❌ <b>Auto-heal failed</b>\nCould not resolve <code>{esc(module)}</code>. "
                                       "Please fix <code>requirements.txt</code> manually.")
                continue
            attempted.add(module)
            pkg = MODULE_TO_PIP.get(module, module)
            msg = await say(chat_id, f"🔧 <b>Auto-heal:</b> missing <code>{esc(module)}</code> — installing…")
            rc, out = await run_cmd(venv_python(pdir), "-m", "pip", "install", pkg, cwd=pdir, timeout=600)
            if msg:
                with contextlib.suppress(TelegramAPIError):
                    await edit_msg(msg, f"{'✅' if rc == 0 else '⚠️'} <code>{esc(pkg)}</code> "
                                        f"{'installed' if rc == 0 else 'failed'}!\n🔄 <b>Auto-restarting script…</b>")
            ACTIVE_PROCESSES.pop(key, None)
            RUNNING_SCRIPTS.pop(key, None)
            _signal_group(proc, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            spawn(start_process(chat_id, uid, project, rel, attempted))
            return
    finally:
        if flush_h is not None:
            flush_h.cancel()
        lf.close()

    code_ = await proc.wait()
    if SHUTTING_DOWN:
        return                              # keep state file so scripts resume on next boot
    if ACTIVE_PROCESSES.get(key) is proc:
        ACTIVE_PROCESSES.pop(key, None)
        RUNNING_SCRIPTS.pop(key, None)
        save_running_state()
        r = (Rich().h(3, "⚠️ Script terminated").p(code(f"{project}/{rel}"))
             .footer(f"Exit code {code_}"))
        with contextlib.suppress(TelegramAPIError):
            await ui.send(bot, chat_id, r, kb([
                btn("▶️ Restart", Mf(a="run", p=R(project), f=R(rel)).pack(), SUCCESS),
                btn("📜 Logs", Log(a="r", f="run.log").pack(), PRIMARY),
            ]))


def _headroom() -> tuple[float, float]:
    return psutil.cpu_percent(0.5), psutil.virtual_memory().available / MB


async def _wait_for_headroom(timeout: float = 60) -> None:
    """Hold the next launch until CPU/RAM calm down (capped, so a busy box can't stall the boot forever)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not SHUTTING_DOWN:
        cpu, free_mb = await asyncio.to_thread(_headroom)
        if cpu < RESUME_MAX_CPU and free_mb > RESUME_MIN_FREE_MB:
            return
        await asyncio.sleep(2)


async def resume_active_processes() -> None:
    saved = load_running_state()
    if not saved:
        return
    PENDING_RESUME.update(saved)            # keeps them in the state file until each one has had its turn
    log.info("Found %d previously active scripts. Resuming one by one…", len(saved))
    by_user: dict[int, list[str]] = {}
    for key, script in saved.items():
        with contextlib.suppress(ValueError):
            by_user.setdefault(int(key.split(":", 1)[0]), []).append(script)
    for uid, scripts in by_user.items():    # one message per user, not one per script
        await say(uid, "🔄 <b>System rebooted:</b> resuming your scripts one by one…\n"
                       + "\n".join(f"• <code>{esc(sc)}</code>" for sc in scripts))
    for key, script in saved.items():
        if SHUTTING_DOWN:
            break
        try:
            if key not in PENDING_RESUME:   # the user stopped it while it was waiting
                continue
            uid_s, proj, rel = key.split(":", 2)
            uid = int(uid_s)
            if not os.path.isdir(project_dir(uid, proj)):
                continue
            await _wait_for_headroom()
            if key not in PENDING_RESUME or SHUTTING_DOWN:
                continue
            await start_process(uid, uid, proj, rel, quiet=True)
            await asyncio.sleep(RESUME_GAP_S)
        except Exception as exc:
            log.warning("Failed to resume %s: %s", script, exc)
        finally:
            if not SHUTTING_DOWN:
                PENDING_RESUME.pop(key, None)


# ============================================================================ #
# CLOUD BACKUP / RESTORE
# ============================================================================ #
def _build_backup_zip(path: str) -> None:
    snap = "runner_panel.db.snapshot"
    db.backup_to(snap)
    try:
        extras = [(snap, os.path.basename(db.DB_PATH)), (STATE_FILE, STATE_FILE)]
        extras += [(f, f) for f in os.listdir(".") if f.endswith((".session", ".session-journal"))]
        zip_dir(BASE_DIR, path, extras, prefix_root=True, lean=True)
    finally:
        with contextlib.suppress(OSError):
            os.remove(snap)


async def perform_backup(status: Message | None = None) -> None:
    zip_path = "system_backup.zip"
    try:
        await asyncio.to_thread(_build_backup_zip, zip_path)
        size = os.path.getsize(zip_path)
        if size > MAX_UPLOAD:
            raise RuntimeError(f"backup is {format_size(size)} — over the {format_size(MAX_UPLOAD)} Bot API upload "
                               "limit. Run a local Bot API server (LOCAL_BOT_API) or trim projects.")
        old_id = None
        with contextlib.suppress(TelegramAPIError):
            chat = await bot.get_chat(BACKUP_CHANNEL_ID)
            old_id = chat.pinned_message.message_id if chat.pinned_message else None
        sent = await bot.send_document(
            BACKUP_CHANNEL_ID, FSInputFile(zip_path, filename="system_backup.zip"),
            caption=f"☁️ <b>Auto-Backup</b>\n📅 Last save: <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>",
        )
        await bot.pin_chat_message(BACKUP_CHANNEL_ID, sent.message_id, disable_notification=True)
        if old_id and old_id != sent.message_id:      # delete the old one only AFTER the new one is safe
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(BACKUP_CHANNEL_ID, old_id)
        if status:
            await ui.edit(bot, status, Rich().h(3, "✅ Backup saved").p(f"Pinned in the cloud channel · {format_size(size)}"))
    except Exception as exc:
        log.exception("backup failed")
        if status:
            with contextlib.suppress(TelegramAPIError):
                await ui.edit(bot, status, Rich().h(3, "❌ Backup failed").pre(str(exc)))
    finally:
        with contextlib.suppress(OSError):
            os.remove(zip_path)


async def auto_backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_H * 3600)
        try:
            await perform_backup()
        except Exception:
            log.exception("auto-backup crashed")


async def restore_system() -> None:
    fresh = not os.path.isdir(BASE_DIR) or not os.listdir(BASE_DIR)
    if RESTORE_MODE == "never" or (RESTORE_MODE == "auto" and not fresh):
        return
    admin = min(ADMIN_IDS) if ADMIN_IDS else None
    msg = await say(admin, "☁️ <b>Checking pinned backup…</b>") if admin else None
    try:
        chat = await bot.get_chat(BACKUP_CHANNEL_ID)
        doc = chat.pinned_message.document if chat.pinned_message else None
        if not doc or doc.file_name != "system_backup.zip":
            if msg:
                await edit_msg(msg, "ℹ️ No backup pinned in the channel. Starting fresh.")
            return
        if doc.file_size and doc.file_size > MAX_DOWNLOAD:
            raise RuntimeError(f"backup is {format_size(doc.file_size)} — over the {format_size(MAX_DOWNLOAD)} "
                               "Bot API download limit (use LOCAL_BOT_API).")
        if msg:
            await edit_msg(msg, "📥 <b>Backup found! Restoring files, sessions and database…</b>")
        tmp = "restore_tmp.zip"
        await bot.download(doc, destination=tmp)
        db.release()
        for ext in ("-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(db.DB_PATH + ext)
        await asyncio.to_thread(safe_extract, tmp, ".")
        os.remove(tmp)
        await asyncio.to_thread(db.setup_db)
        if msg:
            await edit_msg(msg, "✅ <b>Restore complete!</b> Projects, sessions and DB recovered.")
    except Exception as exc:
        log.exception("restore failed")
        if msg:
            with contextlib.suppress(TelegramAPIError):
                await edit_msg(msg, f"⚠️ <b>Restore failed:</b>\n<pre>{esc(exc)}</pre>")


# ============================================================================ #
# VIEWS  (each returns (Rich, keyboard))
# ============================================================================ #
async def view_main(uid):
    active = await db.aget_active_project(uid)
    running = get_running_scripts(uid)
    r = (Rich().h(1, "☁️ Cloud Code Runner")
         .p("Deploy, run and watch your scripts straight from Telegram.")
         .table(["Overview", "Status"], [
             ["📁 Active project", code(active) if active else "none selected"],
             ["▶️ Running", f"{len(running)} script(s)"],
         ]))
    if running:
        r.h(4, "Running now").ul([code(s) for s in running])
    r.footer("Pick an action below")
    return r, kb(
        [btn("📁 My Projects", Menu(a="projects").pack(), PRIMARY), btn("➕ New Project", Menu(a="new").pack(), SUCCESS)],
        [btn("📂 Manage Files (Run/Del)", Fx(a="d", r=".").pack(), PRIMARY)],
        [btn("🛑 Stop Process", Menu(a="stop").pack(), DANGER if running else None),
         btn("📜 View Logs", Menu(a="logs").pack())],
        [btn("📊 System Vitals", Dash(v="sys").pack()), btn("🆘 Help & Guide", Menu(a="help").pack())],
        [btn("👑 Owner Panel", Own(a="l").pack(), PRIMARY)] if uid in ADMIN_IDS else [],
    )


async def view_projects(uid, pg=0):
    names = await asyncio.to_thread(project_names, uid)
    if not names:
        return (Rich().h(3, "❌ No projects yet").p("Create one with ", code("/newproject name"), " or ", code("/clone url"), "."),
                kb([btn("➕ New Project", Menu(a="new").pack(), SUCCESS)], [btn("🔙 Back to Menu", Menu(a="main").pack())]))
    active = await db.aget_active_project(uid)
    running = get_running_scripts(uid)
    chunk, pg, pages = paginate(names, pg)
    rows, row = [], []
    for n in chunk:
        live = any(s.startswith(f"{n}/") for s in running)
        row.append(btn(f"{n}{' (running)' if live else ''}", Proj(n=R(n)).pack(),
                       SUCCESS if live else (PRIMARY if n == active else None), raw=True))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append(nav_row(pg, pages, lambda p: Menu(a="projects", pg=p).pack()))
    rows.append([btn("🔙 Back to Menu", Menu(a="main").pack())])
    table = [[code(n), "🟢 running" if any(s.startswith(f"{n}/") for s in running) else "⚪ idle",
              "⭐ active" if n == active else ""] for n in chunk]
    r = (Rich().h(2, "🗂 Your Projects").table(["Project", "State", ""], table)
         .footer(f"Active: {active or 'none'} · tap a project to switch"))
    return r, kb(*rows)


async def view_myfiles(uid):
    names = await asyncio.to_thread(project_names, uid)
    if not names:
        return Rich().h(3, "❌ No folders or files yet"), kb([btn("🔙 Back to Menu", Menu(a="main").pack())])

    def count():
        return sum(1 for n in names for fn, d in list_dir(project_dir(uid, n)) if not d)
    total = await asyncio.to_thread(count)
    rows, row = [], []
    live_projects = {k.split(":", 2)[1] for k in running_keys(uid)}
    for n in names[:60]:
        row.append(btn(n, Mf(a="fld", p=R(n)).pack(), SUCCESS if n in live_projects else PRIMARY, raw=True))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([btn("🔄 Refresh", Mf(a="main").pack())])
    r = (Rich().h(1, "📂 My File Manager")
         .table(["Stat", "Value"], [["👤 User", code(uid)], ["📁 Folders", len(names)], ["📄 Files", total]])
         .footer("Select a folder below"))
    return r, kb(*rows)


MSEL: dict[tuple[int, str], set[str]] = {}      # (user id, folder) -> selected file names
MF_PAGE = 10


async def view_mf_folder(uid, folder, pg=0):
    path = project_dir(uid, folder)
    if not os.path.isdir(path):
        return None
    files = [n for n, d in await asyncio.to_thread(list_dir, path) if not d]
    sel = MSEL.setdefault((uid, folder), set())
    sel &= set(files)                                   # forget files that no longer exist
    chunk, pg, pages = paginate(files, pg, MF_PAGE)

    def size(f):
        with contextlib.suppress(OSError):
            return format_size(os.path.getsize(os.path.join(path, f)))
        return "?"
    P = R(folder)
    r = Rich().h(2, "📂 Folder · ", code(folder))
    rows = []
    if chunk:
        r.quote("Select files to delete · 🟢 = running (locked)")
        r.checklist([
            (Raw(f"{file_icon(f)} {code(f)} · {size(f)}" + (" · 🟢 running" if is_running(f"{uid}:{folder}:{f}") else "")), f in sel)
            for f in chunk])
        for f in chunk:
            row = []
            if not is_running(f"{uid}:{folder}:{f}"):
                row.append(btn(f"{_mark(f in sel)} {f}", Mf(a="tg", p=P, f=R(f), pg=pg).pack(),
                               DANGER if f in sel else None, raw=True))
            row.append(btn("Open", Mf(a="fil", p=P, f=R(f)).pack(),
                           SUCCESS if is_running(f"{uid}:{folder}:{f}") else PRIMARY))
            rows.append(row)
        rows.append(nav_row(pg, pages, lambda p: Mf(a="fld", p=P, pg=p).pack()))
        rows.append([btn("Select all idle", Mf(a="sa", p=P, pg=pg).pack(), PRIMARY),
                     btn("Clear selection", Mf(a="sc", p=P, pg=pg).pack())])
        if sel:
            rows.append([btn(f"Delete selected ({len(sel)})", Mf(a="sd", p=P, pg=pg).pack(), DANGER)])
        r.xquote("How it works: tap a file's button to select it, tap it again to toggle it back. ",
                 "Open shows the file actions. Nothing is deleted until you confirm.")
    else:
        r.quote("Empty folder")
    rows.append([btn("⬅️ Back", Mf(a="main").pack())])
    r.footer(f"{len(sel)} selected · page {pg + 1}/{pages}")
    return r, kb(*rows)


def view_mf_confirm(uid: int, folder: str, sel: set[str], pg: int):
    names = sorted(sel, key=str.lower)
    shown = [(Raw(f"{file_icon(f)} {code(f)}"), True) for f in names[:30]]
    r = (Rich().h(2, "⚠️ Confirm deletion").p("These files will be permanently deleted.")
         .quote(f"📁 {folder} · 📄 {len(names)} file(s)").checklist(shown)
         .xquote("Safety rules: running scripts are skipped and nothing else in the project is touched."))
    r.footer(f"… and {len(names) - 30} more" if len(names) > 30 else "This cannot be undone")
    P = R(folder)
    return r, kb([btn("Yes, delete", Mf(a="sy", p=P, pg=pg).pack(), DANGER)],
                 [btn("Cancel", Mf(a="fld", p=P, pg=pg).pack())])


LANG = {"py": "python", "json": "json", "yml": "yaml", "yaml": "yaml", "js": "javascript", "sh": "bash",
        "md": "markdown", "log": "log", "toml": "toml", "html": "html", "css": "css", "sql": "sql", "txt": "text"}


def read_text_page(path: str, pg: int, size: int = 3000):
    if os.path.getsize(path) > 2 * MB:
        return None, "File is larger than 2 MB. Use Download."
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:4096]:
        return None, "Binary file. Use Download."
    text = data.decode("utf-8", "replace")
    pages = max(1, -(-len(text) // size))
    pg = max(0, min(pg, pages - 1))
    return (text[pg * size:(pg + 1) * size], pg, pages), None


async def view_file_panel(uid, proj, rel, mode="x"):
    path = safe_join(project_dir(uid, proj), rel)
    if not path or not os.path.isfile(path):
        return None
    running = is_running(f"{uid}:{proj}:{rel}")
    st = os.stat(path)
    r = (Rich().h(2, "File")
         .table(["Field", "Value"], [
             ["Project", code(proj)], ["Path", code(rel)], ["Size", format_size(st.st_size)],
             ["Modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))],
             ["Status", "Running" if running else "Idle"]])
         .footer("Choose an action below"))
    P, F_ = R(proj), R(rel)

    def A(a, **kw):
        return Fa(a=a, p=P, r=F_, m=mode, **kw).pack()
    rows = [[btn("View", A("v"), PRIMARY), btn("Download", A("dl"))],
            [btn("Rename", A("ren"))]]
    if rel.endswith(".py"):
        rows.append([btn("Stop", A("stp"), DANGER) if running else btn("Run", A("run"), SUCCESS),
                     btn("Logs", A("lg"), SUCCESS if running else None)])
    rows.append([btn("Delete", A("del"), DANGER)])
    back = Fx(a="d", r=R(os.path.dirname(rel) or ".")).pack() if mode == "x" else Mf(a="fld", p=P).pack()
    rows.append([btn("Back", back)])
    return r, kb(*rows)


async def view_mf_file(uid, folder, file):
    return await view_file_panel(uid, folder, file, "m")


def view_confirm(what: str, ok_cb: str, cancel_cb: str, icon="📄"):
    r = (Rich().h(2, "⚠️ Confirm deletion").p("This cannot be undone.")
         .quote(f"{icon} {what}"))
    return r, kb([btn("✅ Yes, delete", ok_cb, DANGER)], [btn("❌ Cancel", cancel_cb)])


async def view_explorer(uid, rel=".", pg=0):
    active = await db.aget_active_project(uid)
    if not active:
        return None
    pdir = project_dir(uid, active)
    target = safe_join(pdir, rel)
    if not target or not os.path.isdir(target):
        return None
    entries = await asyncio.to_thread(list_dir, target)
    chunk, pg, pages = paginate(entries, pg)
    running = get_running_scripts(uid)
    rows = []
    for name, is_dir in chunk:
        item = name if rel == "." else f"{rel}/{name}"
        full = f"{active}/{item}"
        live = any(s.startswith(full + "/") for s in running) if is_dir else full in running
        label = name + ("/" if is_dir else "") + (" (running)" if live else "")
        rows.append([btn(label, Fx(a="d" if is_dir else "f", r=R(item)).pack(), SUCCESS if live else None, raw=True)])
    rows.append(nav_row(pg, pages, lambda p: Fx(a="d", r=R(rel), pg=p).pack()))
    if rel != ".":
        parent = os.path.dirname(rel) or "."
        rows.append([btn("🔙 Back", Fx(a="d", r=R(parent)).pack()),
                     btn("🗑 Delete Folder", Fx(a="cd", r=R(rel)).pack(), DANGER)])
    rows.append([btn("🏠 Main Menu", Menu(a="main").pack())])
    r = (Rich().h(2, "📂 Explorer").p(code(f"{active}/{'' if rel == '.' else rel}"))
         .footer(f"{len(entries)} item(s) · 🟢 = running"))
    return r, kb(*rows)


async def view_file_actions(uid, rel):
    active = await db.aget_active_project(uid)
    return await view_file_panel(uid, active, rel, "x") if active else None


async def view_logs(uid, filename="run.log"):
    active = await db.aget_active_project(uid)
    if not active:
        return "noproj"
    path = safe_join(project_dir(uid, active), filename)
    if not path or not os.path.isfile(path):
        return "nolog"
    text = await asyncio.to_thread(tail_text, path)
    size = os.path.getsize(path)
    r = (Rich().h(3, "📜 Logs · ", code(f"{active}/{filename}"))
         .pre(text or "(empty)", lang="log")
         .footer(f"{format_size(size)} · {time.strftime('%H:%M:%S')}"))
    return r, kb([btn("🔄 Refresh Logs", Log(a="r", f=R(filename)).pack(), PRIMARY),
                  btn("📥 Download", Log(a="dl", f=R(filename)).pack())],
                 [btn("🔙 Menu", Menu(a="main").pack())])


def view_help():
    cmds = [
        ("/newproject [name]", "Create a project folder"), ("/deleteproject [name]", "Wipe a project"),
        ("/run [file.py]", "Execute a file (default main.py)"), ("/stop [file.py]", "Stop a script"),
        ("/input [text]", "Send OTP/password to the console"), ("/logs [file.log]", "View logs"),
        ("/tree", "Project structure"), ("/myfiles", "Graphical file manager"),
        ("/rename [old] [new]", "Rename file/folder"), ("/rmdir [folder]", "Delete subfolder"),
        ("/installreqs", "Install requirements.txt"), ("/clone [url]", "Clone a GitHub repo"),
        ("/export", "Download project as ZIP"), ("/backup", "Manual cloud save (admin)"),
        ("/cleanup", "Delete inactive files & projects (admin)"),
    ]
    r = (Rich().h(2, "🆘 Help & Guide").table(["Command", "What it does"], cmds)
         .quote("Send a .zip or any file to upload it into your active project.")
         .footer("Tip: auto-heal installs missing modules and restarts your script"))
    return r, kb([btn("🔙 Back to Menu", Menu(a="main").pack())])


def view_new():
    r = (Rich().h(2, "➕ Create a new project")
         .p("Send the command: ", code("/newproject ProjectName"))
         .h(4, "🔗 Or clone from GitHub").p(code("/clone https://github.com/user/repo")))
    return r, kb([btn("🔙 Back to Menu", Menu(a="main").pack())])


def view_stop_select(uid):
    rows = []
    for k in running_keys(uid):
        _, proj, rel = k.split(":", 2)
        rows.append([btn(f"🛑 Stop {RUNNING_SCRIPTS.get(k, rel)}", Kill(p=R(proj), r=R(rel)).pack(), DANGER)])
    rows.append([btn("🔙 Back to Menu", Menu(a="main").pack())])
    return Rich().h(3, "🛑 Select a process to stop"), kb(*rows)


# ---- dashboard (admin) ----
def _proc_mem(pid: int) -> float:
    try:
        p = psutil.Process(pid)
        return sum(c.memory_info().rss for c in [p] + p.children(recursive=True)) / MB
    except psutil.Error:
        return 0.0


def _collect_vitals(pids: list[int]) -> dict:
    """Every blocking psutil call for the dashboard. Run via asyncio.to_thread, never on the event loop."""
    return {
        "cpu": psutil.cpu_percent(0.3),
        "ram": psutil.virtual_memory(),
        "disk": psutil.disk_usage("/"),
        "panel_mb": psutil.Process().memory_info().rss / MB,   # the bot alone (scripts are its children)
        "mem": {pid: _proc_mem(pid) for pid in pids},
        "top": _top_procs(5),
    }


def _scan_users(users):
    out = []
    for uid in users:
        ud = user_dir(uid)
        if not os.path.isdir(ud):
            continue
        projects = [(n, [f for f, d in list_dir(os.path.join(ud, n)) if not d]) for n in project_names(uid)]
        if projects:
            out.append((uid, projects))
    return out


def _top_procs(n: int = 5):
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cmdline"]):
        try:
            mi = p.info["memory_info"]
            if mi:
                cmd = " ".join(p.info["cmdline"] or [])[:46] or p.info["name"] or "?"
                rows.append((mi.rss, p.info["pid"], cmd))
        except (psutil.Error, TypeError):
            continue
    rows.sort(reverse=True)
    return rows[:n]


async def memory_guard_loop() -> None:
    """Optional: stop (and tell the owner) any script whose RSS passes MAX_SCRIPT_MB."""
    while MAX_SCRIPT_MB > 0:
        await asyncio.sleep(30)
        for k in [k for k in list(ACTIVE_PROCESSES) if is_running(k)]:
            mem = await asyncio.to_thread(_proc_mem, ACTIVE_PROCESSES[k].pid)
            if mem > MAX_SCRIPT_MB:
                uid_s = k.split(":", 1)[0]
                name = await stop_process(k)
                log.warning("memory guard stopped %s (%.0f MB)", name, mem)
                with contextlib.suppress(Exception):
                    await ui.send(bot, int(uid_s), Rich().h(3, "🧠 Stopped by memory guard")
                                  .p(code(name), f" used {mem:.0f} MB (limit {MAX_SCRIPT_MB} MB)")
                                  .footer("Raise MAX_SCRIPT_MB or fix the leak, then start it again"))


async def user_label(uid):
    name = await db.acache_get(f"name:{uid}")
    if not name:
        try:
            chat = await bot.get_chat(uid)
            name = chat.first_name or chat.title or f"User {uid}"
        except TelegramAPIError:
            name = f"User {uid}"
        await db.acache_set(f"name:{uid}", name)
    return mention(uid, name)


_DEPLOY_CACHE: list = [0.0, 0]


async def view_dashboard(kind="sys"):
    users = await db.aget_all_users()
    r = Rich().h(1, "🌐 Cloud Operating System").p("aiogram ", aiogram.__version__, " · rich messages")
    if kind == "files":
        tree = await asyncio.to_thread(_scan_users, users)
        r.h(2, "📂 User File Architecture")
        if not tree:
            r.quote("No active users or projects found")
        size = 0
        for uid, projects in tree:
            lines = []
            for pi, (proj, files) in enumerate(projects):
                last_p = pi == len(projects) - 1
                lines.append(f"{'└──' if last_p else '├──'} 📂 {proj}/")
                for fi, f in enumerate(files):
                    last_f = fi == len(files) - 1
                    pad = "    " if last_p else "│   "
                    state = "[Running]" if is_running(f"{uid}:{proj}:{f}") else "[Idle]"
                    lines.append(f"{pad}{'└──' if last_f else '├──'} 📄 {f} ..... {state}")
            block = "\n".join(lines)
            size += len(block)
            if size > 24000:
                r.quote("… output truncated")
                break
            r.h(4, await user_label(uid)).pre(block)
    else:
        if time.time() - _DEPLOY_CACHE[0] > 30:
            _DEPLOY_CACHE[:] = [time.time(), sum(len(f) for _, ps in await asyncio.to_thread(_scan_users, users) for _, f in ps)]
        total = _DEPLOY_CACHE[1]
        live_all = [(k, p) for k, p in list(ACTIVE_PROCESSES.items()) if is_running(k)]   # snapshot
        active = len(live_all)
        vit = await asyncio.to_thread(_collect_vitals, [p.pid for _, p in live_all])
        cpu, ram, disk, mem = vit["cpu"], vit["ram"], vit["disk"], vit["mem"]
        r.h(2, "📊 System Vitals").table(["Metric", "Usage", "Load"], [
            ["⚡ CPU", f"{cpu:.1f}%", bar(cpu)],
            ["📟 RAM", f"{ram.percent:.1f}% ({format_size(ram.used)} / {format_size(ram.total)})", bar(ram.percent)],
            ["💾 Disk", f"{disk.percent:.1f}% ({format_size(disk.used)} / {format_size(disk.total)})", bar(disk.percent)],
        ])
        scripts_mb = sum(mem.values())
        panel_mb = vit["panel_mb"]
        used_mb = (ram.total - ram.available) / MB
        top = vit["top"]
        r.h(2, "🧠 Memory breakdown").table(["Who", "RAM"], [
            ["🤖 Panel (this bot)", f"{panel_mb:.0f} MB"],
            ["▶️ Your scripts", f"{scripts_mb:.0f} MB"],
            ["🖥 Everything else", f"{max(0.0, used_mb - panel_mb - scripts_mb):.0f} MB"],
        ])
        r.xquote("Top processes: " + " · ".join(f"{cmd} ({rss / MB:.0f} MB)" for rss, _, cmd in top))
        r.h(2, "⚙️ Runtime").table(["Item", "Value"], [
            ["⏱️ Uptime", format_uptime(time.time() - BOT_START_TIME)],
            ["🐍 Python", platform.python_version()],
            ["📦 Deployments", f"{total} total / {active} active"],
            ["✨ Status", "🟢 Operational"],
        ])
        live = live_all[:10]
        if live:
            r.h(3, "▶️ Running scripts").table(["Owner", "Script", "PID", "RAM"], [
                [k.split(":", 1)[0], RUNNING_SCRIPTS.get(k, k), p.pid, f"{mem.get(p.pid, 0.0):.0f} MB"] for k, p in live])
    r.footer(f"Last updated {time.strftime('%Y-%m-%d | %H:%M:%S')}")
    return r, kb(
        [btn("⚙️ System", Dash(v="sys").pack(), PRIMARY if kind == "sys" else None),
         btn("📂 File Manager", Dash(v="files").pack(), PRIMARY if kind == "files" else None)],
        [btn("🔄 Refresh Panel", Dash(v=kind, ref=True).pack(), SUCCESS)],
        [btn("🔙 Main Menu", Menu(a="main").pack())],
    )


# ============================================================================ #
# MIDDLEWARE + ERROR HANDLING
# ============================================================================ #
_seen: dict[int, float] = {}


@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def gate(handler, event, data):
    user = getattr(event, "from_user", None)
    if user:
        if ALLOWED_USERS and user.id not in ALLOWED_USERS and user.id not in ADMIN_IDS:
            if isinstance(event, CallbackQuery):
                await answer(event, "🔒 This bot is private.", True)
            elif isinstance(event, Message):
                await reply(event, "🔒 This bot is private.")
            return None
        now = time.monotonic()
        if now - _seen.get(user.id, 0) > 3600:
            _seen[user.id] = now
            spawn(db.atouch_user(user.id))
    return await handler(event, data)


@dp.error()
async def on_error(event: ErrorEvent) -> bool:
    log.error("Unhandled error", exc_info=event.exception)
    cb = event.update.callback_query
    if cb:
        await answer(cb, "⚠️ Something went wrong. Please try again.", True)
    return True


# ============================================================================ #
# CALLBACK HANDLERS
# ============================================================================ #
@router.callback_query(Menu.filter())
async def cb_menu(cb: CallbackQuery, callback_data: Menu):
    uid, a = cb.from_user.id, callback_data.a
    if not user_message(cb):
        return await answer(cb, "Message too old — send /start", True)
    if a == "main":
        await present(cb, await view_main(uid))
    elif a == "projects":
        await present(cb, await view_projects(uid, callback_data.pg))
    elif a == "new":
        await present(cb, view_new())
    elif a == "help":
        await present(cb, view_help())
    elif a == "logs":
        v = await view_logs(uid)
        if v == "noproj":
            return await answer(cb, "❌ No active project!", True)
        if v == "nolog":
            return await answer(cb, "❌ No logs yet.", True)
        await present(cb, v)
    elif a == "stop":
        keys = running_keys(uid)
        if not keys:
            return await answer(cb, "⚠️ No processes are running.", True)
        if len(keys) == 1:
            name = await stop_process(keys[0])
            await answer(cb, f"🛑 Stopped {name}", True)
            return await present(cb, await view_main(uid))
        await present(cb, view_stop_select(uid))
    await answer(cb)


@router.callback_query(Proj.filter())
async def cb_proj(cb: CallbackQuery, callback_data: Proj):
    name = await D(callback_data.n)
    if not name or not os.path.isdir(project_dir(cb.from_user.id, name)):
        return await answer(cb, "❌ Project not found.", True)
    await db.aset_active_project(cb.from_user.id, name)
    await present(cb, (Rich().h(3, "✅ Active project updated").p(code(name)),
                       kb([btn("📂 Open files", Fx(a="d", r=".").pack(), PRIMARY),
                           btn("🏠 Menu", Menu(a="main").pack())])))
    await answer(cb)


@router.callback_query(Kill.filter())
async def cb_kill(cb: CallbackQuery, callback_data: Kill):
    uid = cb.from_user.id
    proj, rel = await D(callback_data.p), await D(callback_data.r)
    key = f"{uid}:{proj}:{rel}"
    if is_running(key):
        name = await stop_process(key)
        await answer(cb, f"🛑 Stopped {name}", True)
    else:
        await answer(cb, "⚠️ Not running.", True)
    if user_message(cb):
        await present(cb, await view_main(uid))


@router.callback_query(Log.filter())
async def cb_log(cb: CallbackQuery, callback_data: Log):
    uid = cb.from_user.id
    fname = await D(callback_data.f) or "run.log"
    if callback_data.a == "dl":
        active = await db.aget_active_project(uid)
        path = safe_join(project_dir(uid, active), fname) if active else None
        if not path or not os.path.isfile(path):
            return await answer(cb, "❌ Not found.", True)
        if not user_message(cb):
            return await answer(cb, "Message too old — reopen the logs.", True)
        return await send_file(cb, path, cb.message.chat.id)
    v = await view_logs(uid, fname)
    if v in ("noproj", "nolog"):
        return await answer(cb, "❌ Not found.", True)
    changed = await ui.edit(bot, cb.message, *v)
    await answer(cb, "✅ Refreshed!" if changed else "⚠️ No new logs.")


@router.callback_query(Dash.filter())
async def cb_dash(cb: CallbackQuery, callback_data: Dash):
    if cb.from_user.id not in ADMIN_IDS:
        return await answer(cb, "❌ Only admins can view System Vitals.", True)
    if not callback_data.ref and user_message(cb):
        with contextlib.suppress(TelegramAPIError):
            await ui.edit(bot, cb.message, Rich().h(3, "🔄 Fetching data…"))
    try:
        view = await view_dashboard(callback_data.v)
    except Exception:
        log.exception("view_dashboard failed")
        if user_message(cb):                      # don't leave the panel stuck on "Fetching data…"
            with contextlib.suppress(TelegramAPIError):
                await ui.edit(bot, cb.message, Rich().h(3, "⚠️ Couldn't load vitals"),
                              kb([btn("🔄 Retry", Dash(v=callback_data.v, ref=True).pack(), SUCCESS)]))
        return await answer(cb, "⚠️ Dashboard failed — see logs.", True)
    changed = await ui.edit(bot, cb.message, *view)
    await answer(cb, ("✅ Dashboard refreshed!" if changed else "⚡ Already up to date!") if callback_data.ref else None)


@router.callback_query(Fx.filter())
async def cb_explorer(cb: CallbackQuery, callback_data: Fx):
    uid, a = cb.from_user.id, callback_data.a
    rel = await D(callback_data.r) or "."
    active = await db.aget_active_project(uid)
    if not active:
        return await answer(cb, "❌ No active project!", True)
    pdir = project_dir(uid, active)
    target = safe_join(pdir, rel)
    if not target:
        return await answer(cb, "❌ Invalid path.", True)
    if a == "d":
        v = await view_explorer(uid, rel, callback_data.pg)
        if not v:
            return await answer(cb, "❌ Invalid path.", True)
        await present(cb, v)
    elif a == "f":
        v = await view_file_actions(uid, rel)
        if not v:
            return await answer(cb, "❌ File not found.", True)
        await present(cb, v)
    elif a == "run":
        if not user_message(cb):
            return await answer(cb, "Message too old", True)
        with contextlib.suppress(TelegramAPIError):
            await cb.message.delete()
        await answer(cb)
        return spawn(start_process(cb.message.chat.id, uid, active, rel))
    elif a in ("cf", "cd"):
        icon, ok = ("📄", "xf") if a == "cf" else ("📁", "xd")
        parent = Fx(a="f" if a == "cf" else "d", r=R(rel)).pack()
        await present(cb, view_confirm(rel, Fx(a=ok, r=R(rel)).pack(), parent, icon))
    elif a in ("xf", "xd"):
        if rel in (".", "venv", ".git") or target == os.path.realpath(pdir):
            return await answer(cb, "❌ Denied.", True)
        if os.path.exists(target):
            await asyncio.to_thread(shutil.rmtree if a == "xd" else os.remove, target)
            await answer(cb, f"✅ Deleted {os.path.basename(rel)}", True)
        else:
            await answer(cb, "❌ Not found.", True)
        v = await view_explorer(uid, os.path.dirname(rel) or ".")
        if v:
            await present(cb, v)
        return
    await answer(cb)


@router.callback_query(Mf.filter())
async def cb_myfiles(cb: CallbackQuery, callback_data: Mf):
    uid, a = cb.from_user.id, callback_data.a
    folder = await D(callback_data.p) if callback_data.p else ""
    file = await D(callback_data.f) if callback_data.f else ""
    if not user_message(cb):
        return await answer(cb, "Message too old — send /myfiles", True)
    if a == "main":
        await present(cb, await view_myfiles(uid))
    elif a == "fld":
        v = await view_mf_folder(uid, folder, callback_data.pg)
        if not v:
            return await answer(cb, "❌ Folder not found.", True)
        await present(cb, v, embed=True)
    elif a == "fil":
        v = await view_mf_file(uid, folder, file)
        if not v:
            return await answer(cb, "❌ File not found.", True)
        await present(cb, v)
    elif a == "tg":
        sel = MSEL.setdefault((uid, folder), set())
        path = safe_join(project_dir(uid, folder), file)
        if not path or not os.path.isfile(path):
            return await answer(cb, "❌ File not found.", True)
        if file in sel:
            sel.discard(file)                           # toggle back
        else:
            sel.add(file)
        v = await view_mf_folder(uid, folder, callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "sa":
        sel = MSEL.setdefault((uid, folder), set())
        pdir = project_dir(uid, folder)
        for n, d in await asyncio.to_thread(list_dir, pdir):
            if not d and not is_running(f"{uid}:{folder}:{n}"):
                sel.add(n)
        v = await view_mf_folder(uid, folder, callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "sc":
        MSEL.pop((uid, folder), None)
        v = await view_mf_folder(uid, folder, callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "sd":
        sel = MSEL.get((uid, folder))
        if not sel:
            return await answer(cb, "Nothing selected.", True)
        await present(cb, view_mf_confirm(uid, folder, sel, callback_data.pg), embed=True)
    elif a == "sy":
        sel = MSEL.pop((uid, folder), set())
        done = skipped = 0
        for n in sel:
            path = safe_join(project_dir(uid, folder), n)
            if is_running(f"{uid}:{folder}:{n}") or not path or not os.path.isfile(path):
                skipped += 1
                continue
            await asyncio.to_thread(os.remove, path)
            done += 1
        await answer(cb, f"✅ Deleted {done} file(s)" + (f", skipped {skipped}" if skipped else ""), True)
        v = await view_mf_folder(uid, folder)
        if v:
            await present(cb, v, embed=True)
        return
    elif a == "run":
        if not file.endswith(".py"):
            return await answer(cb, "⚠️ Only Python scripts can be run.", True)
        await db.aset_active_project(uid, folder)
        with contextlib.suppress(TelegramAPIError):
            await cb.message.delete()
        await answer(cb)
        return spawn(start_process(cb.message.chat.id, uid, folder, file))
    elif a == "stp":
        key = f"{uid}:{folder}:{file}"
        if not is_running(key):
            return await answer(cb, "⚠️ Script is not running.", True)
        await stop_process(key)
        await answer(cb, f"🛑 Stopped {file}", True)
        v = await view_mf_file(uid, folder, file)
        if v:
            await present(cb, v)
        return
    elif a == "del":
        await present(cb, view_confirm(file, Mf(a="dely", p=R(folder), f=R(file)).pack(),
                                       Mf(a="fil", p=R(folder), f=R(file)).pack(), file_icon(file)))
    elif a == "dely":
        path = safe_join(project_dir(uid, folder), file)
        if path and os.path.isfile(path):
            await asyncio.to_thread(os.remove, path)
            await answer(cb, f"✅ Deleted {file}", True)
        else:
            await answer(cb, "❌ File not found.", True)
        v = await view_mf_folder(uid, folder)
        if v:
            await present(cb, v, embed=True)
        return
    elif a == "dl":
        path = safe_join(project_dir(uid, folder), file)
        if not path or not os.path.isfile(path):
            return await answer(cb, "❌ File not found.", True)
        return await send_file(cb, path, cb.message.chat.id)
    await answer(cb)


# ============================================================================ #
# COMMANDS
# ============================================================================ #
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)


async def need_project(message: Message) -> str | None:
    active = await db.aget_active_project(message.from_user.id)
    if not active:
        await reply(message, "❌ No active project selected. Use /myprojects or /newproject.")
    return active


@router.message(Command("start", "menu"))
async def cmd_start(message: Message):
    await present(message, await view_main(message.from_user.id))


@router.message(Command("help"))
async def cmd_help(message: Message):
    await present(message, view_help())


@router.message(Command("myfiles"))
async def cmd_myfiles(message: Message):
    await present(message, await view_myfiles(message.from_user.id))


@router.message(Command("myprojects"))
async def cmd_myprojects(message: Message):
    await present(message, await view_projects(message.from_user.id))


@router.message(Command("dashboard", "stats"), IS_ADMIN)
async def cmd_dashboard(message: Message):
    await present(message, await view_dashboard("sys"))


@router.message(Command("backup"), IS_ADMIN)
async def cmd_backup(message: Message):
    status = await reply(message, "⏳ Generating cloud backup…")
    await perform_backup(status)


@router.message(Command("broadcast"), IS_ADMIN)
async def cmd_broadcast(message: Message, command: CommandObject):
    if not command.args:
        return await reply(message, "Usage: <code>/broadcast [message]</code>")
    body = Rich().h(3, "📢 Admin Broadcast").quote(command.args)
    sent = failed = 0
    for uid in await db.aget_all_users():
        for attempt in range(2):
            try:
                await ui.send(bot, uid, body)
                sent += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError:
                failed += 1
                break
        await asyncio.sleep(0.05)               # stay under Telegram's flood limits
    await ui.send(bot, message.chat.id, Rich().h(3, "✅ Broadcast finished")
                  .table(["Delivered", "Failed"], [[sent, failed]]))


@router.message(Command("newproject"))
async def cmd_newproject(message: Message, command: CommandObject):
    name = (command.args or "").strip()
    if not name:
        return await reply(message, "Usage: <code>/newproject [name]</code>")
    if not re.fullmatch(r"[A-Za-z0-9\-]{1,40}", name):
        return await reply(message, "❌ Invalid name (letters, numbers and - only).")
    uid = message.from_user.id
    pdir = project_dir(uid, name)
    if os.path.exists(pdir):
        return await reply(message, "⚠️ Project exists!")
    msg = await reply(message, f"⏳ Creating <code>{esc(name)}</code>…")
    os.makedirs(pdir, exist_ok=True)
    await run_cmd(sys.executable, "-m", "venv", os.path.join(pdir, "venv"), timeout=300)
    await db.aset_active_project(uid, name)
    await edit_msg(msg, f"✅ <code>{esc(name)}</code> created and selected!")


@router.message(Command("deleteproject"))
async def cmd_deleteproject(message: Message, command: CommandObject):
    name = (command.args or "").strip()
    uid = message.from_user.id
    if not valid_project(name):
        return await reply(message, "Usage: <code>/deleteproject [name]</code>")
    pdir = project_dir(uid, name)
    if not os.path.isdir(pdir):
        return await reply(message, "❌ Project not found.")
    for k in [k for k in list(ACTIVE_PROCESSES) if k.startswith(f"{uid}:{name}:")]:
        await stop_process(k)
    await asyncio.to_thread(shutil.rmtree, pdir, True)
    if await db.aget_active_project(uid) == name:
        await db.aset_active_project(uid, None)
    await reply(message, f"✅ Project <code>{esc(name)}</code> and all its processes deleted.")


@router.message(Command("clone"))
async def cmd_clone(message: Message, command: CommandObject):
    url = (command.args or "").split()[0].split("/tree/")[0] if command.args else ""
    if not re.fullmatch(r"https://[\w.\-]+/[\w.\-~%/]+", url):
        return await reply(message, "Usage: <code>/clone https://github.com/user/repo</code>")
    name = re.sub(r"[^a-zA-Z0-9\-]", "-", url.rstrip("/").split("/")[-1].replace(".git", ""))[:40] or "repo"
    uid = message.from_user.id
    pdir = project_dir(uid, name)
    if os.path.exists(pdir):
        return await reply(message, f"⚠️ Project <code>{esc(name)}</code> exists!")
    msg = await reply(message, f"⏳ Cloning <code>{esc(name)}</code>…")
    os.makedirs(user_dir(uid), exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}      # private repo => fail fast instead of hanging
    rc, out = await run_cmd("git", "clone", "--", url, pdir, timeout=600, env=env)
    if rc != 0:
        return await edit_msg(msg, f"❌ Failed.\n<pre>{esc(out[-600:])}</pre>")
    await edit_msg(msg, "⏳ Creating virtual environment…")
    await run_cmd(sys.executable, "-m", "venv", os.path.join(pdir, "venv"), timeout=300)
    await db.aset_active_project(uid, name)
    await edit_msg(msg, f"✅ Cloned <code>{esc(name)}</code>!")


@router.message(Command("run"))
async def cmd_run(message: Message, command: CommandObject):
    active = await need_project(message)
    if active:
        spawn(start_process(message.chat.id, message.from_user.id, active, (command.args or "main.py").strip()))


@router.message(Command("stop"))
async def cmd_stop(message: Message, command: CommandObject):
    uid = message.from_user.id
    keys = running_keys(uid)
    if not keys:
        return await reply(message, "⚠️ No processes are running.")
    if command.args:
        target = command.args.strip()
        hits = [k for k in keys if k.endswith(f":{target}")]
        if not hits:
            return await reply(message, f"❌ No running process matches <code>{esc(target)}</code>.")
        for k in hits:
            await stop_process(k)
        return await reply(message, f"🛑 Stopped <code>{esc(target)}</code>.")
    if len(keys) == 1:
        return await reply(message, f"🛑 Stopped <code>{esc(await stop_process(keys[0]))}</code>.")
    await present(message, view_stop_select(uid))


@router.message(Command("input"))
async def cmd_input(message: Message, command: CommandObject):
    uid = message.from_user.id
    keys = running_keys(uid)
    if not keys:
        return await reply(message, "❌ No processes are running.")
    if not command.args:
        return await reply(message, "Usage: <code>/input [text]</code> or <code>/input [file.py] [text]</code>")
    key, text = None, command.args
    if len(keys) == 1:
        key = keys[0]
    else:
        first, _, rest = command.args.partition(" ")
        hits = [k for k in keys if k.endswith(f":{first}")]
        if hits and rest:
            key, text = hits[0], rest
        else:
            active = await db.aget_active_project(uid)
            mine = [k for k in keys if k.startswith(f"{uid}:{active}:")]
            if len(mine) == 1:
                key = mine[0]
    if not key:
        return await reply(message, "⚠️ Multiple processes running. Specify: <code>/input [file.py] [text]</code>")
    proc = ACTIVE_PROCESSES[key]
    try:
        proc.stdin.write((text + "\n").encode())
        await asyncio.wait_for(proc.stdin.drain(), 10)
    except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
        return await reply(message, "❌ Could not write to the process (it may have exited).")
    with contextlib.suppress(TelegramAPIError):
        await message.delete()                    # don't leave OTPs/passwords sitting in the chat
    await reply(message, f"⌨️ Input sent to <code>{esc(RUNNING_SCRIPTS.get(key, key))}</code>.")


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject):
    v = await view_logs(message.from_user.id, (command.args or "run.log").strip())
    if v == "noproj":
        return await reply(message, "❌ No active project selected.")
    if v == "nolog":
        return await reply(message, "❌ No logs found there.")
    await present(message, v)


@router.message(Command("tree"))
async def cmd_tree(message: Message):
    active = await need_project(message)
    if not active:
        return

    def gen(path, prefix=""):
        out = ""
        items = [n for n, _ in list_dir(path)]
        for idx, name in enumerate(items):
            last = idx == len(items) - 1
            out += f"{prefix}{'└── ' if last else '├── '}{name}\n"
            full = os.path.join(path, name)
            if os.path.isdir(full):
                out += gen(full, prefix + ("    " if last else "│   "))
        return out
    tree = await asyncio.to_thread(gen, project_dir(message.from_user.id, active))
    r = Rich().h(2, "🌳 Structure of ", code(active)).pre(f"{active}/\n{tree}" if tree.strip() else "(empty)")
    await present(message, (r, None))


@router.message(Command("export"))
async def cmd_export(message: Message):
    active = await need_project(message)
    if not active:
        return
    pdir = project_dir(message.from_user.id, active)
    zip_path = f"{pdir}.zip"
    msg = await reply(message, "⏳ Creating ZIP…")
    try:
        await asyncio.to_thread(zip_dir, pdir, zip_path)
        size = os.path.getsize(zip_path)
        if size > MAX_UPLOAD:
            return await edit_msg(msg, f"❌ ZIP is {format_size(size)} — over Telegram's {format_size(MAX_UPLOAD)} bot limit.")
        await edit_msg(msg, "📤 Uploading…")
        await message.answer_document(FSInputFile(zip_path), caption=f"📦 <b>Exported:</b> <code>{esc(active)}</code>")
    finally:
        with contextlib.suppress(OSError):
            os.remove(zip_path)


@router.message(Command("import"))
async def cmd_import(message: Message):
    await present(message, (Rich().h(3, "📥 How to import")
                            .ol([Raw(f"Use {code('/newproject name')}"), "Send me the .zip file"]), None))


@router.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject):
    active = await need_project(message)
    if not active:
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        return await reply(message, "Usage: <code>/rename [old] [new]</code>")
    pdir = project_dir(message.from_user.id, active)
    old, new = safe_join(pdir, parts[0]), safe_join(pdir, parts[1])
    if not old or not new:
        return await reply(message, "❌ Invalid path.")
    if not os.path.exists(old):
        return await reply(message, "❌ Not found.")
    os.makedirs(os.path.dirname(new), exist_ok=True)
    os.rename(old, new)
    await reply(message, f"✅ Renamed <code>{esc(parts[0])}</code> to <code>{esc(parts[1])}</code>.")


@router.message(Command("rmdir"))
async def cmd_rmdir(message: Message, command: CommandObject):
    active = await need_project(message)
    if not active:
        return
    name = (command.args or "").strip()
    if not name:
        return await reply(message, "Usage: <code>/rmdir [folder_name]</code>")
    pdir = project_dir(message.from_user.id, active)
    target = safe_join(pdir, name)
    if name in ("venv", ".git", ".") or not target or target == os.path.realpath(pdir):
        return await reply(message, "❌ Denied.")
    if os.path.isdir(target):
        await asyncio.to_thread(shutil.rmtree, target)
        await reply(message, f"✅ <code>{esc(name)}</code> deleted.")
    else:
        await reply(message, "❌ Not found.")


@router.message(Command("installreqs"))
async def cmd_installreqs(message: Message):
    active = await need_project(message)
    if not active:
        return
    pdir = project_dir(message.from_user.id, active)
    req = os.path.join(pdir, "requirements.txt")
    if not os.path.exists(req):
        return await reply(message, "❌ <code>requirements.txt</code> not found.")
    msg = await reply(message, "⏳ Installing…")
    rc, out = await run_cmd(venv_python(pdir), "-m", "pip", "install", "-r", req, cwd=pdir)
    await edit_msg(msg, "✅ Dependencies installed!" if rc == 0 else f"⚠️ Install problems:\n<pre>{esc(out[-700:])}</pre>")


# ---- file upload ----
@router.message(F.document, F.chat.type == "private")
async def on_document(message: Message):
    uid = message.from_user.id
    active = await need_project(message)
    if not active:
        return
    doc = message.document
    if doc.file_size and doc.file_size > MAX_DOWNLOAD:
        return await reply(message, f"❌ File is {format_size(doc.file_size)} — Telegram bots can only download "
                                    f"{format_size(MAX_DOWNLOAD)}. Send a smaller file or use /clone.")
    pdir = project_dir(uid, active)
    name = os.path.basename(doc.file_name or "upload.bin")
    path = safe_join(pdir, name)
    if not path:
        return await reply(message, "❌ Invalid file name.")
    msg = await reply(message, "📥 Downloading…")
    await bot.download(doc, destination=path)
    if name.lower().endswith(".zip"):
        try:
            await asyncio.to_thread(safe_extract, path, pdir)
        except (zipfile.BadZipFile, ValueError) as exc:
            return await edit_msg(msg, f"❌ Bad archive: <code>{esc(exc)}</code>")
        finally:
            with contextlib.suppress(OSError):
                os.remove(path)
    await edit_msg(msg, f"✅ Saved to <code>{esc(active)}</code>")


# ---- file actions: view / download / rename / run / logs / delete ----
class Ren(StatesGroup):
    name = State()


@router.callback_query(Fa.filter())
async def cb_file(cb: CallbackQuery, callback_data: Fa, state: FSMContext):
    uid, a, mode = cb.from_user.id, callback_data.a, callback_data.m
    proj, rel = await D(callback_data.p), await D(callback_data.r)
    msg = user_message(cb)
    if not proj or not rel or not msg:
        return await answer(cb, "Button expired. Reopen the file.", True)
    path = safe_join(project_dir(uid, proj), rel)
    if a != "rc" and (not path or not os.path.isfile(path)):
        return await answer(cb, "File not found.", True)
    key = f"{uid}:{proj}:{rel}"
    P, F_ = R(proj), R(rel)
    A = lambda act, **kw: Fa(a=act, p=P, r=F_, m=mode, **kw).pack()   # noqa: E731

    async def panel():
        v = await view_file_panel(uid, proj, rel, mode)
        if v:
            await present(cb, v)

    async def back_to_list():
        if mode == "x":
            await db.aset_active_project(uid, proj)
            v = await view_explorer(uid, os.path.dirname(rel) or ".")
        else:
            v = await view_mf_folder(uid, proj)
        if v:
            await present(cb, v, embed=(mode != "x"))

    if a == "o":
        await panel()
    elif a == "v":
        page, err = await asyncio.to_thread(read_text_page, path, callback_data.pg)
        if err:
            return await answer(cb, err, True)
        chunk, pg, pages = page
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        r = (Rich().h(3, "Viewing ", code(rel)).pre(chunk or "(empty file)", lang=LANG.get(ext))
             .footer(f"Page {pg + 1} of {pages}"))
        rows = [nav_row(pg, pages, lambda p: A("v", pg=p)),
                [btn("Download", A("dl")), btn("Rename", A("ren"))], [btn("Back to file", A("o"))]]
        await ui.edit(bot, msg, r, kb(*rows))
    elif a == "dl":
        return await send_file(cb, path, msg.chat.id)
    elif a == "ren":
        if is_running(key):
            return await answer(cb, "Stop the script before renaming it.", True)
        await state.set_state(Ren.name)
        await state.update_data(p=proj, r=rel, m=mode)
        await ui.send(bot, msg.chat.id, Rich().h(3, "Rename").p("Send the new name for ", code(rel), ".")
                      .p("Folders are allowed, e.g. ", code("sub/new.py"), "."),
                      kb([btn("Cancel", A("rc"))]))
    elif a == "rc":
        await state.clear()
        with contextlib.suppress(TelegramAPIError):
            await msg.delete()
        return await answer(cb, "Cancelled.")
    elif a == "run":
        if not rel.endswith(".py"):
            return await answer(cb, "Only Python scripts can be run.", True)
        await db.aset_active_project(uid, proj)
        with contextlib.suppress(TelegramAPIError):
            await msg.delete()
        await answer(cb)
        return spawn(start_process(msg.chat.id, uid, proj, rel))
    elif a == "stp":
        if not is_running(key):
            return await answer(cb, "Script is not running.", True)
        await stop_process(key)
        await answer(cb, f"Stopped {rel}", True)
        return await panel()
    elif a == "lg":
        lp = os.path.join(project_dir(uid, proj), "run.log")
        text = await asyncio.to_thread(tail_text, lp) if os.path.isfile(lp) else ""
        r = Rich().h(3, "Logs ", code(f"{proj}/run.log")).pre(text or "(empty)", lang="log").footer(time.strftime("%H:%M:%S"))
        changed = await ui.edit(bot, msg, r, kb([btn("Refresh", A("lg"), PRIMARY)], [btn("Back to file", A("o"))]))
        return await answer(cb, None if changed else "No new logs.")
    elif a == "del":
        await present(cb, view_confirm(rel, A("dely"), A("o"), ""))
    elif a == "dely":
        if is_running(key):
            await stop_process(key)
        await asyncio.to_thread(os.remove, path)
        await answer(cb, f"Deleted {os.path.basename(rel)}", True)
        return await back_to_list()
    await answer(cb)


@router.message(Ren.name, F.text, ~F.text.startswith("/"))
async def on_rename(message: Message, state: FSMContext):
    data = await state.get_data()
    uid, proj, rel, mode = message.from_user.id, data.get("p"), data.get("r"), data.get("m", "x")
    new = (message.text or "").strip()
    pdir = project_dir(uid, proj or "")
    old_path, new_path = safe_join(pdir, rel or ""), safe_join(pdir, new)
    if not proj or not old_path or not os.path.isfile(old_path):
        await state.clear()
        return await reply(message, "File no longer exists.")
    if not new or len(new) > 150 or not new_path or new_path == os.path.realpath(pdir):
        return await reply(message, "Invalid name. Send another name or /cancel.")
    if os.path.exists(new_path):
        return await reply(message, "That name already exists. Send another name or /cancel.")
    if is_running(f"{uid}:{proj}:{rel}"):
        await state.clear()
        return await reply(message, "Stop the script before renaming it.")
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    os.rename(old_path, new_path)
    await state.clear()
    new_rel = os.path.relpath(new_path, os.path.realpath(pdir))
    v = await view_file_panel(uid, proj, new_rel, mode)
    await reply(message, f"Renamed to <code>{esc(new_rel)}</code>.")
    if v:
        await present(message, v)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await reply(message, "Cancelled.")


# ============================================================================ #
# OWNER / ADMIN PANEL  (every running script of every user)
# ============================================================================ #
def _split_key(key: str):
    uid_s, proj, rel = key.split(":", 2)
    return int(uid_s), proj, rel


def _all_running() -> list[str]:
    return sorted(k for k in list(ACTIVE_PROCESSES) if is_running(k))


async def view_owner(pg: int = 0):
    keys = _all_running()
    r = Rich().h(1, "👑 Owner Panel")
    clean = [btn("🧹 Inactive files & projects", Own(a="i").pack(), PRIMARY), btn("💾 Free space", Own(a="sp").pack(), PRIMARY)]
    if not keys:
        r.quote("No scripts are running right now.")
        return r.footer(time.strftime("%H:%M:%S")), kb(
            clean, [btn("🔄 Refresh", Own(a="l").pack(), PRIMARY)], [btn("🏠 Menu", Menu(a="main").pack())])
    chunk, pg, pages = paginate(keys, pg, 10)
    rows_t, rows_b = [], []
    for k in chunk:
        uid, proj, rel = _split_key(k)
        p = ACTIVE_PROCESSES[k]
        mem = await asyncio.to_thread(_proc_mem, p.pid)
        rows_t.append([await user_label(uid), f"{proj}/{rel}", p.pid, f"{mem:.0f} MB",
                       format_uptime(time.time() - START_TIMES.get(k, time.time()))])
        rows_b.append([btn(f"{proj}/{rel}", Own(a="v", k=R(k)).pack(), SUCCESS, raw=True)])
    r.h(3, f"▶️ {len(keys)} running").table(["Owner", "Script", "PID", "RAM", "Up"], rows_t)
    r.footer(f"Tap a script to inspect, stop or delete · {time.strftime('%H:%M:%S')}")
    rows_b.append(nav_row(pg, pages, lambda x: Own(a="l", pg=x).pack()))
    rows_b.append([btn("🛑 Stop ALL", Own(a="sa").pack(), DANGER), btn("🔄 Refresh", Own(a="l", pg=pg).pack(), PRIMARY)])
    rows_b.append(clean)
    rows_b.append([btn("🏠 Menu", Menu(a="main").pack())])
    return r, kb(*rows_b)


# ---- inactive files & projects manager (owner) ----
# Selection is a checklist; every row has a toggle button (tap again = toggle back).
# Item ids: "uid:project" (whole project) or "uid:project:relative/path" (single file).
SEL: dict[int, set[str]] = {}            # admin id -> selected item ids
SEL_MAX = 200
INACT_PAGE = 8


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _age(ts: float) -> str:
    sec = max(0, time.time() - ts)
    if sec >= 86400:
        return f"{int(sec // 86400)}d ago"
    if sec >= 3600:
        return f"{int(sec // 3600)}h ago"
    return "just now"


def _all_user_ids() -> list[int]:
    out: list[int] = []
    with contextlib.suppress(OSError):
        for n in os.listdir(BASE_DIR):
            if n.startswith("user_") and n[5:].isdigit() and os.path.isdir(os.path.join(BASE_DIR, n)):
                out.append(int(n[5:]))
    return sorted(out)


def _project_busy(uid: int, proj: str) -> bool:
    return any(k.startswith(f"{uid}:{proj}:") and is_running(k) for k in list(ACTIVE_PROCESSES))


def _walk_files(pdir: str) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(pdir):
        dirs[:] = [d for d in dirs if visible(d)]
        out.extend(os.path.relpath(os.path.join(root, f), pdir) for f in files if visible(f))
    return sorted(out, key=str.lower)


def _parse_item(item: str):
    parts = item.split(":", 2)
    try:
        uid = int(parts[0])
    except ValueError:
        return None
    if len(parts) < 2 or not valid_project(parts[1]):
        return None
    return uid, parts[1], (parts[2] if len(parts) == 3 else None)


def _ctx_of(item: str) -> str:
    """The list an item lives in: projects live under 'uid', files under 'uid:project'."""
    parts = item.split(":", 2)
    return parts[0] if len(parts) == 2 else f"{parts[0]}:{parts[1]}"


def _scan_ctx(ctx: str):
    """Blocking scan. ctx '' = users, 'uid' = projects, 'uid:project' = files. Last tuple item is `busy`."""
    if not ctx:
        return "users", [(uid, len(names), len(running_keys(uid)))
                         for uid in _all_user_ids() if (names := project_names(uid))]
    parts = ctx.split(":", 1)
    try:
        uid = int(parts[0])
    except ValueError:
        return None
    if len(parts) == 1:
        rows = []
        for name in project_names(uid):
            pdir = project_dir(uid, name)
            files = _walk_files(pdir)
            last = max((_mtime(os.path.join(pdir, f)) for f in files), default=_mtime(pdir))
            rows.append((f"{uid}:{name}", name, len(files), last, _project_busy(uid, name)))
        return "projects", rows
    proj = parts[1]
    pdir = project_dir(uid, proj)
    if not valid_project(proj) or not os.path.isdir(pdir):
        return None
    rows = []
    for rel in _walk_files(pdir):
        full = os.path.join(pdir, rel)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        rows.append((f"{uid}:{proj}:{rel}", rel, size, _mtime(full), is_running(f"{uid}:{proj}:{rel}")))
    return "files", rows


def _mark(selected: bool) -> str:
    return "☑" if selected else "☐"


async def view_inactive(admin: int, ctx: str = "", pg: int = 0):
    scanned = await asyncio.to_thread(_scan_ctx, ctx)
    if scanned is None:
        return None
    kind, rows = scanned
    sel = SEL.setdefault(admin, set())
    r = Rich().h(1, "🧹 Inactive cleanup")
    btns: list[list] = []

    if kind == "users":
        chunk, pg, pages = paginate(rows, pg, 10)
        if chunk:
            r.p("Pick a user to browse their projects and files.")
            r.table(["User", "Projects", "Running"], [[await user_label(u), n, live] for u, n, live in chunk])
        else:
            r.quote("No user projects on disk.")
        btns += [[btn(f"{u} · {n} project(s)", Own(a="i", k=R(str(u))).pack(), PRIMARY, raw=True)] for u, n, _ in chunk]
        nav = lambda x: Own(a="i", pg=x).pack()                     # noqa: E731
        back = [btn("🔙 Owner Panel", Own(a="l").pack())]
    elif kind == "projects":
        uid = int(ctx)
        chunk, pg, pages = paginate(rows, pg, INACT_PAGE)
        r.h(2, "📁 Projects of ", await user_label(uid))
        r.quote("Select what to delete · 🔒 = has a running script (locked)")
        if chunk:
            r.checklist([
                (Raw(f"{code(name)} · {n} file(s) · changed {_age(last)}" + (" · 🔒 running" if busy else "")), item in sel)
                for item, name, n, last, busy in chunk])
        else:
            r.quote("This user has no projects.")
        for item, name, n, last, busy in chunk:
            row = []
            if not busy:
                row.append(btn(f"{_mark(item in sel)} {name}", Own(a="t", k=R(item), pg=pg).pack(),
                               DANGER if item in sel else None, raw=True))
            row.append(btn("Files", Own(a="i", k=R(item)).pack(), SUCCESS if busy else PRIMARY))
            btns.append(row)
        nav = lambda x: Own(a="i", k=R(ctx), pg=x).pack()           # noqa: E731
        back = [btn("🔙 Users", Own(a="i").pack())]
    else:
        uid, proj = int(ctx.split(":", 1)[0]), ctx.split(":", 1)[1]
        chunk, pg, pages = paginate(rows, pg, INACT_PAGE)
        r.h(2, "📄 Files in ", code(proj))
        r.quote("Select what to delete · 🔒 = running (locked)")
        if chunk:
            r.checklist([
                (Raw(f"{code(rel)} · {format_size(size)} · {_age(last)}" + (" · 🔒 running" if busy else "")), item in sel)
                for item, rel, size, last, busy in chunk])
        else:
            r.quote("No files in this project.")
        for item, rel, size, last, busy in chunk:
            if not busy:
                btns.append([btn(f"{_mark(item in sel)} {rel}", Own(a="t", k=R(item), pg=pg).pack(),
                                 DANGER if item in sel else None, raw=True)])
        nav = lambda x: Own(a="i", k=R(ctx), pg=x).pack()           # noqa: E731
        back = [btn("🔙 Projects", Own(a="i", k=R(str(uid))).pack())]

    btns.append(nav_row(pg, pages, nav))
    if kind != "users":
        btns.append([btn("Select all inactive", Own(a="ia", k=R(ctx), pg=pg).pack(), PRIMARY),
                     btn("Clear selection", Own(a="ic", k=R(ctx), pg=pg).pack())])
    if sel:
        btns.append([btn(f"Delete selected ({len(sel)})", Own(a="id", k=R(ctx), pg=pg).pack(), DANGER)])
    btns.append(back)
    if kind != "users":
        r.xquote("How it works: tap an item's button to select it, tap it again to toggle it back. ",
                 "Select all inactive skips running items. Nothing is deleted until you confirm.")
    r.footer(f"{len(sel)} selected · page {pg + 1}/{pages} · {time.strftime('%H:%M:%S')}")
    return r, kb(*btns)


def view_inactive_confirm(sel: set[str], ctx: str, pg: int):
    projects = sorted(i for i in sel if len(i.split(":", 2)) == 2)
    files = sorted(i for i in sel if len(i.split(":", 2)) == 3)
    shown = [(Raw(f"💣 {code(i.replace(':', ' / ', 1))}"), True) for i in projects[:15]]
    shown += [(Raw(f"📄 {code(i.replace(':', ' / '))}"), True) for i in files[:max(0, 30 - len(shown))]]
    more = len(sel) - len(shown)
    r = (Rich().h(2, "⚠️ Confirm cleanup")
         .p("These items will be permanently deleted. Running scripts are skipped automatically.")
         .quote(f"💣 {len(projects)} project(s) · 📄 {len(files)} file(s)")
         .checklist(shown)
         .xquote("Safety rules: running scripts are skipped, files inside a deleted project go with it, ",
                 "and hidden folders such as venv are never listed as files."))
    r.footer(f"… and {more} more" if more > 0 else "This cannot be undone")
    return r, kb([btn("Yes, delete", Own(a="iy", k=R(ctx), pg=pg).pack(), DANGER)],
                 [btn("Cancel", Own(a="i", k=R(ctx), pg=pg).pack())])


async def delete_selected(admin: int) -> tuple[int, int, int]:
    sel = SEL.pop(admin, set())
    projects = [i for i in sel if len(i.split(":", 2)) == 2]
    files = [i for i in sel if len(i.split(":", 2)) == 3]
    n_proj = n_files = skipped = 0
    gone: set[tuple[int, str]] = set()
    for item in projects:
        parsed = _parse_item(item)
        if not parsed:
            skipped += 1
            continue
        uid, proj, _ = parsed
        pdir = project_dir(uid, proj)
        if _project_busy(uid, proj) or not os.path.isdir(pdir):
            skipped += 1
            continue
        await asyncio.to_thread(shutil.rmtree, pdir, True)
        if await db.aget_active_project(uid) == proj:
            await db.aset_active_project(uid, None)
        gone.add((uid, proj))
        n_proj += 1
    for item in files:
        parsed = _parse_item(item)
        if not parsed or not parsed[2]:
            skipped += 1
            continue
        uid, proj, rel = parsed
        if (uid, proj) in gone:
            continue                                  # already removed with its project
        path = safe_join(project_dir(uid, proj), rel)
        if is_running(f"{uid}:{proj}:{rel}") or not path or not os.path.isfile(path):
            skipped += 1
            continue
        await asyncio.to_thread(os.remove, path)
        n_files += 1
    return n_proj, n_files, skipped


def _fsize(p: str) -> int:
    try:
        return os.lstat(p).st_size
    except OSError:
        return 0


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        total += sum(_fsize(os.path.join(root, f)) for f in files)
    return total


def _trim_log(path: str, keep: int) -> None:
    """Keep only the newest `keep` bytes, in place (the running script's append handle stays valid)."""
    try:
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= keep:
                return
            f.seek(size - keep)
            data = f.read()
            nl = data.find(b"\n")
            f.seek(0)
            f.write(data[nl + 1:] if nl != -1 else data)
            f.truncate()
    except OSError:
        pass


def _projects():
    return [(uid, proj) for uid in _all_user_ids() for proj in project_names(uid)]


def _pyc_dirs(root_dir: str) -> list[str]:
    out = []
    for root, dirs, _ in os.walk(root_dir):
        if "__pycache__" in dirs:
            out.append(os.path.join(root, "__pycache__"))
            dirs.remove("__pycache__")
    return out


def _tmp_files() -> list[str]:
    out = [f for f in ("system_backup.zip", "restore_tmp.zip", "runner_panel.db.snapshot") if os.path.isfile(f)]
    for uid in _all_user_ids():
        ud = user_dir(uid)
        with contextlib.suppress(OSError):
            out += [os.path.join(ud, n) for n in os.listdir(ud)
                    if n.endswith(".zip") and os.path.isfile(os.path.join(ud, n))]
    return out


def _idle_venvs() -> list[str]:
    busy = {tuple(k.split(":", 2)[:2]) for k in list(ACTIVE_PROCESSES) if is_running(k)}
    busy |= {tuple(k.split(":", 2)[:2]) for k in list(STARTING)}
    out = []
    for uid, proj in _projects():
        v = os.path.join(project_dir(uid, proj), "venv")
        if os.path.isdir(v) and (str(uid), proj) not in busy:
            out.append(v)
    return out


SPACE_ITEMS = {
    "pyc": ("Python cache", "__pycache__ folders, rebuilt automatically"),
    "pip": ("pip cache", "downloaded package files"),
    "logs": ("Old logs", "run.log trimmed to its newest 256 KB"),
    "tmp": ("Temp files", "leftover backup / export zips"),
    "venv": ("Idle environments", "venv of projects with nothing running; rebuilt on next Run (first start is slower)"),
}
SPACE_DEFAULT = {"pyc", "pip", "logs", "tmp"}
SPACE_SEL: dict[int, set[str]] = {}


def _scan_space() -> dict[str, int]:
    sizes = dict.fromkeys(SPACE_ITEMS, 0)
    for uid, proj in _projects():
        pdir = project_dir(uid, proj)
        sizes["pyc"] += sum(_dir_size(d) for d in _pyc_dirs(pdir))
        sizes["logs"] += max(0, _fsize(os.path.join(pdir, "run.log")) - LOG_KEEP)
    sizes["pip"] = _dir_size(PIP_CACHE) if os.path.isdir(PIP_CACHE) else 0
    sizes["tmp"] = sum(_fsize(p) for p in _tmp_files())
    sizes["venv"] = sum(_dir_size(v) for v in _idle_venvs())
    return sizes


def _clean_space(sel: set[str]) -> None:
    if "pyc" in sel:
        for uid, proj in _projects():
            for d in _pyc_dirs(project_dir(uid, proj)):
                shutil.rmtree(d, True)
    if "pip" in sel:
        shutil.rmtree(PIP_CACHE, True)
    if "logs" in sel:
        for uid, proj in _projects():
            _trim_log(os.path.join(project_dir(uid, proj), "run.log"), LOG_KEEP)
    if "tmp" in sel:
        for p in _tmp_files():
            with contextlib.suppress(OSError):
                os.remove(p)
    if "venv" in sel:
        for v in _idle_venvs():
            shutil.rmtree(v, True)


async def view_space(admin: int):
    sizes = await asyncio.to_thread(_scan_space)
    sel = SPACE_SEL.setdefault(admin, set(SPACE_DEFAULT))
    disk = psutil.disk_usage("/")
    r = (Rich().h(1, "💾 Free space")
         .table(["Disk", "Value"], [["Used", f"{format_size(disk.used)} / {format_size(disk.total)} ({disk.percent:.0f}%)"],
                                    ["Free", format_size(disk.free)]])
         .quote("Tick what to clean, then press Clean selected")
         .checklist([(f"{label} · {format_size(sizes[k])}", k in sel) for k, (label, _) in SPACE_ITEMS.items()])
         .xquote(" · ".join(f"{label}: {note}" for label, note in SPACE_ITEMS.values())))
    total = sum(sizes[k] for k in sel)
    r.footer(f"{len(sel)} selected · about {format_size(total)} can be freed")
    rows = [[btn(f"{_mark(k in sel)} {label}", Own(a="spt", k=k).pack(), DANGER if k in sel and k == "venv" else (SUCCESS if k in sel else None), raw=True)]
            for k, (label, _) in SPACE_ITEMS.items()]
    if sel:
        rows.append([btn(f"Clean selected ({format_size(total)})", Own(a="spy").pack(), SUCCESS)])
    rows.append([btn("🔄 Rescan", Own(a="sp").pack(), PRIMARY), btn("🔙 Owner Panel", Own(a="l").pack())])
    return r, kb(*rows)


async def view_owner_detail(key: str):
    if not is_running(key):
        return None
    uid, proj, rel = _split_key(key)
    p = ACTIVE_PROCESSES[key]
    mem = await asyncio.to_thread(_proc_mem, p.pid)
    r = (Rich().h(2, "⚙️ Script Details").table(["Field", "Value"], [
        ["👤 Owner", await user_label(uid)], ["📁 Project", code(proj)], ["📄 Script", code(rel)],
        ["🔢 PID", p.pid], ["📟 RAM", f"{mem:.0f} MB"],
        ["⏱️ Uptime", format_uptime(time.time() - START_TIMES.get(key, time.time()))]]))
    k = R(key)
    return r, kb(
        [btn("🛑 Stop", Own(a="s", k=k).pack(), DANGER), btn("📜 Logs", Own(a="lg", k=k).pack(), PRIMARY)],
        [btn("🗑 Stop & delete file", Own(a="df", k=k).pack(), DANGER)],
        [btn("💣 Delete whole project", Own(a="dp", k=k).pack(), DANGER)],
        [btn("🔙 Back", Own(a="l").pack())],
    )


@router.callback_query(Own.filter())
async def cb_owner(cb: CallbackQuery, callback_data: Own):
    if cb.from_user.id not in ADMIN_IDS:
        return await answer(cb, "❌ Owner only.", True)
    if not user_message(cb):
        return await answer(cb, "Message too old — send /owner", True)
    a = callback_data.a
    key = await D(callback_data.k) if callback_data.k else ""
    back = Own(a="l").pack()
    if a == "l":
        await present(cb, await view_owner(callback_data.pg))
    elif a == "v":
        v = await view_owner_detail(key)
        if not v:
            await answer(cb, "⚠️ No longer running.", True)
            return await present(cb, await view_owner())
        await present(cb, v)
    elif a == "s":
        name = await stop_process(key)
        await answer(cb, f"🛑 Stopped {name}", True)
        return await present(cb, await view_owner())
    elif a == "lg":
        uid, proj, rel = _split_key(key)
        path = os.path.join(project_dir(uid, proj), "run.log")
        text = await asyncio.to_thread(tail_text, path) if os.path.isfile(path) else ""
        r = (Rich().h(3, "📜 Logs · ", code(f"{proj}/{rel}")).pre(text or "(empty)", lang="log")
             .footer(time.strftime("%H:%M:%S")))
        changed = await ui.edit(bot, cb.message, r, kb(
            [btn("🔄 Refresh", Own(a="lg", k=R(key)).pack(), PRIMARY)], [btn("🔙 Back", Own(a="v", k=R(key)).pack())]))
        return await answer(cb, None if changed else "⚠️ No new logs.")
    elif a in ("df", "dp"):
        uid, proj, rel = _split_key(key)
        what = f"file {proj}/{rel} (owner {uid})" if a == "df" else f"project {proj} (owner {uid}) with ALL its files"
        await present(cb, view_confirm(what, Own(a=a + "y", k=R(key)).pack(), Own(a="v", k=R(key)).pack(),
                                       "📄" if a == "df" else "💣"))
    elif a == "dfy":
        uid, proj, rel = _split_key(key)
        await stop_process(key)
        path = safe_join(project_dir(uid, proj), rel)
        if path and os.path.isfile(path):
            await asyncio.to_thread(os.remove, path)
        await answer(cb, f"✅ Stopped and deleted {proj}/{rel}", True)
        return await present(cb, await view_owner())
    elif a == "dpy":
        uid, proj, _ = _split_key(key)
        for k in [k for k in list(ACTIVE_PROCESSES) if k.startswith(f"{uid}:{proj}:")]:
            await stop_process(k)
        await asyncio.to_thread(shutil.rmtree, project_dir(uid, proj), True)
        if await db.aget_active_project(uid) == proj:
            await db.aset_active_project(uid, None)
        await answer(cb, f"✅ Project {proj} deleted", True)
        return await present(cb, await view_owner())
    elif a == "i":
        v = await view_inactive(cb.from_user.id, key, callback_data.pg)
        if not v:
            return await answer(cb, "❌ Nothing there anymore.", True)
        await present(cb, v, embed=True)
    elif a == "t":
        if not _parse_item(key):
            return await answer(cb, "❌ Invalid item.", True)
        sel = SEL.setdefault(cb.from_user.id, set())
        if key in sel:
            sel.discard(key)                                  # toggle back
        elif len(sel) >= SEL_MAX:
            return await answer(cb, f"Selection limit is {SEL_MAX} items.", True)
        else:
            sel.add(key)
        v = await view_inactive(cb.from_user.id, _ctx_of(key), callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "ia":
        scanned = await asyncio.to_thread(_scan_ctx, key)
        if scanned and scanned[0] != "users":
            sel = SEL.setdefault(cb.from_user.id, set())
            for row in scanned[1]:
                if not row[-1] and len(sel) < SEL_MAX:
                    sel.add(row[0])
        v = await view_inactive(cb.from_user.id, key, callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "ic":
        SEL.pop(cb.from_user.id, None)
        v = await view_inactive(cb.from_user.id, key, callback_data.pg)
        if v:
            await present(cb, v, embed=True)
    elif a == "id":
        sel = SEL.get(cb.from_user.id)
        if not sel:
            return await answer(cb, "Nothing selected.", True)
        await present(cb, view_inactive_confirm(sel, key, callback_data.pg), embed=True)
    elif a == "iy":
        n_proj, n_files, skipped = await delete_selected(cb.from_user.id)
        r = (Rich().h(2, "✅ Cleanup finished")
             .table(["Projects", "Files", "Skipped"], [[n_proj, n_files, skipped]])
             .quote("Running scripts are never deleted.")
             .footer(time.strftime("%H:%M:%S")))
        await answer(cb, f"✅ Deleted {n_proj} project(s), {n_files} file(s)", True)
        return await present(cb, (r, kb([btn("Back to cleanup", Own(a="i").pack(), PRIMARY)],
                                        [btn("🔙 Owner Panel", Own(a="l").pack())])), embed=True)
    elif a == "sp":
        await present(cb, await view_space(cb.from_user.id), embed=True)
    elif a == "spt":
        sel = SPACE_SEL.setdefault(cb.from_user.id, set(SPACE_DEFAULT))
        if key in SPACE_ITEMS:
            sel.symmetric_difference_update({key})          # toggle on / toggle back
        await present(cb, await view_space(cb.from_user.id), embed=True)
    elif a == "spy":
        sel = set(SPACE_SEL.get(cb.from_user.id) or SPACE_DEFAULT)
        before = psutil.disk_usage("/").free
        await answer(cb, "🧹 Cleaning…")
        await asyncio.to_thread(_clean_space, sel)
        free = psutil.disk_usage("/").free
        r = (Rich().h(2, "✅ Space freed")
             .table(["Freed", "Free now"], [[format_size(max(0, free - before)), format_size(free)]])
             .footer(", ".join(SPACE_ITEMS[k][0] for k in SPACE_ITEMS if k in sel)))
        return await present(cb, (r, kb([btn("Back to Free space", Own(a="sp").pack(), PRIMARY)],
                                        [btn("🔙 Owner Panel", Own(a="l").pack())])), embed=True)
    elif a == "sa":
        n = len(_all_running())
        await present(cb, (Rich().h(2, "⚠️ Stop ALL scripts").p(f"This stops {n} running script(s) for every user."),
                           kb([btn("✅ Yes, stop all", Own(a="say").pack(), DANGER)], [btn("❌ Cancel", back)])))
    elif a == "say":
        keys = _all_running()
        for k in keys:
            await stop_process(k)
        await answer(cb, f"🛑 Stopped {len(keys)} script(s)", True)
        return await present(cb, await view_owner())
    await answer(cb)


@router.message(Command("owner", "runs", "admin"), IS_ADMIN)
async def cmd_owner(message: Message):
    await present(message, await view_owner())


@router.message(Command("cleanup"), IS_ADMIN)
async def cmd_cleanup(message: Message):
    await present(message, await view_inactive(message.from_user.id), embed=True)


# ============================================================================ #
# STARTUP / SHUTDOWN
# ============================================================================ #
COMMANDS = [
    ("start", "Open main panel"), ("myfiles", "Graphical file manager"), ("dashboard", "System vitals (admin)"),
    ("newproject", "Create project"), ("deleteproject", "Delete project"), ("myprojects", "Switch projects"),
    ("run", "Run .py file"), ("stop", "Stop script"), ("input", "Send OTP/password"), ("logs", "View logs"),
    ("tree", "View project map"), ("backup", "Cloud save (admin)"), ("clone", "Clone repo"),
    ("export", "Download ZIP"), ("import", "Import ZIP"), ("rename", "Rename file/folder"),
    ("rmdir", "Delete folder"), ("installreqs", "Install requirements"), ("help", "Help & guide"), ("owner", "Owner panel (admin)"),
    ("cleanup", "Delete inactive files/projects (admin)"),
]


@dp.startup()
async def on_startup():
    me = await bot.get_me()
    log.info("Started as @%s · aiogram %s · rich=%s", me.username, aiogram.__version__, ui.RICH_ENABLED)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: /backup, /dashboard and /broadcast are disabled.")
    await restore_system()
    os.makedirs(BASE_DIR, exist_ok=True)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    spawn(auto_backup_loop())
    if MAX_SCRIPT_MB > 0:
        spawn(memory_guard_loop())
    spawn(resume_active_processes())          # background: never blocks the bot from answering


@dp.shutdown()
async def on_shutdown():
    global SHUTTING_DOWN
    SHUTTING_DOWN = True                       # state file stays intact -> scripts resume next boot
    for proc in list(ACTIVE_PROCESSES.values()):
        if proc.returncode is None:
            _signal_group(proc, signal.SIGTERM)
    await asyncio.sleep(1)
    for proc in list(ACTIVE_PROCESSES.values()):
        if proc.returncode is None:
            _signal_group(proc, signal.SIGKILL)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
