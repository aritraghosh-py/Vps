"""
runner_panel database layer (SQLite) - aiogram edition.

Public API keeps the same names/signatures as before:
set_active_project / get_active_project / get_all_users (+ a* async twins).

- One shared connection guarded by an RLock -> no "stuck" / locked / recursive-cursor errors
- WAL mode, 30s busy timeout, automatic retry on lock errors
- Max-performance PRAGMAs (big page cache, memory-mapped I/O, in-memory temp store)
- Nightly maintenance at local midnight: clears logs + cache (+ project run.log files),
  removes junk rows, checkpoints WAL, VACUUMs and releases memory
- Async wrappers (a*) so aiogram handlers never block the event loop
- backup_to() makes a consistent snapshot for the cloud backup; release() lets the
  restore routine safely replace the DB file
"""
from __future__ import annotations

import asyncio
import atexit
import functools
import gc
import glob
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
DB_PATH = "runner_panel.db"
LOG_FILES = ["runner_panel.log"]
# Per-project script logs are truncated at midnight too
LOG_GLOBS = ["projects/user_*/*/run.log"]

CACHE_MB = 256
MMAP_MB = 1024
BUSY_TIMEOUT_S = 30

# Delete users not seen for N days during nightly cleanup. 0 = never (default).
USER_RETENTION_DAYS = 0

_log = logging.getLogger("runner_panel.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are opened explicitly.
    conn = sqlite3.connect(
        DB_PATH,
        timeout=BUSY_TIMEOUT_S,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size=-{CACHE_MB * 1024}")
    conn.execute(f"PRAGMA mmap_size={MMAP_MB * 1024 * 1024}")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA journal_size_limit=67108864")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _reset_conn() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


def _retry(fn):
    """Retry on lock/IO errors instead of hanging or crashing."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(1, 4):
            try:
                return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if attempt == 3:
                    raise
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    _reset_conn()
                _log.warning("DB error (%s), retry %d/3", exc, attempt)
                time.sleep(0.25 * attempt)
    return wrapper


@contextmanager
def _db(write: bool = False):
    """Thread-safe cursor. Writes run inside BEGIN IMMEDIATE and roll back on error."""
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()
        try:
            if write:
                cur.execute("BEGIN IMMEDIATE")
            yield cur
            if write:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            cur.close()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
@_retry
def setup_db() -> None:
    """Creates tables if missing and upgrades old databases in place."""
    with _db(write=True) as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                active_project TEXT,
                last_seen INTEGER
            )
        """)
        cols = {row[1] for row in cur.execute("PRAGMA table_info(users)")}
        if "last_seen" not in cols:
            cur.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
        cur.execute("UPDATE users SET last_seen = ? WHERE last_seen IS NULL", (int(time.time()),))

        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                level TEXT,
                message TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                expires_at INTEGER
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen)")


# --------------------------------------------------------------------------- #
# Public API (unchanged names)
# --------------------------------------------------------------------------- #
@_retry
def set_active_project(user_id, project_name):
    """Saves or updates the currently selected project for a user."""
    with _db(write=True) as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, active_project, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                active_project = excluded.active_project,
                last_seen = excluded.last_seen
            """,
            (user_id, project_name, int(time.time())),
        )


@_retry
def get_active_project(user_id):
    """Retrieves the currently selected project for a user."""
    with _db() as cur:
        cur.execute("SELECT active_project FROM users WHERE user_id = ?", (user_id,))
        result = cur.fetchone()
    return result[0] if result else None


@_retry
def get_all_users():
    """Fetches a list of all unique user IDs (used for admin broadcasts/stats)."""
    with _db() as cur:
        cur.execute("SELECT DISTINCT user_id FROM users")
        return [row[0] for row in cur.fetchall()]


@_retry
def touch_user(user_id) -> None:
    """Refreshes last_seen for an existing user (never creates a row)."""
    with _db(write=True) as cur:
        cur.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (int(time.time()), user_id))


# --------------------------------------------------------------------------- #
# Logs + cache helpers
# --------------------------------------------------------------------------- #
@_retry
def log_event(message: str, level: str = "INFO") -> None:
    with _db(write=True) as cur:
        cur.execute(
            "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
            (int(time.time()), level, str(message)),
        )


@_retry
def cache_set(key: str, value: str, ttl_seconds: int = 86400) -> None:
    with _db(write=True) as cur:
        cur.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, value, int(time.time()) + ttl_seconds),
        )


@_retry
def cache_get(key: str):
    with _db() as cur:
        cur.execute("SELECT value FROM cache WHERE key = ? AND expires_at > ?", (key, int(time.time())))
        row = cur.fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Backup / restore helpers
# --------------------------------------------------------------------------- #
def backup_to(dest: str) -> None:
    """Consistent snapshot of the live DB (safe while the bot is running)."""
    with _lock:
        if os.path.exists(dest):
            os.remove(dest)
        src = _get_conn()
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()


def release() -> None:
    """Checkpoint and close the connection. It re-opens lazily on next use.
    Call before replacing the DB file (restore), then call setup_db()."""
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        _reset_conn()


# --------------------------------------------------------------------------- #
# Async wrappers - use inside aiogram handlers (they never block the loop)
# --------------------------------------------------------------------------- #
async def aset_active_project(user_id, project_name):
    return await asyncio.to_thread(set_active_project, user_id, project_name)


async def aget_active_project(user_id):
    return await asyncio.to_thread(get_active_project, user_id)


async def aget_all_users():
    return await asyncio.to_thread(get_all_users)


async def atouch_user(user_id):
    return await asyncio.to_thread(touch_user, user_id)


async def alog_event(message: str, level: str = "INFO"):
    return await asyncio.to_thread(log_event, message, level)


async def acache_set(key: str, value: str, ttl_seconds: int = 86400):
    return await asyncio.to_thread(cache_set, key, value, ttl_seconds)


async def acache_get(key: str):
    return await asyncio.to_thread(cache_get, key)


async def abackup_to(dest: str):
    return await asyncio.to_thread(backup_to, dest)


# --------------------------------------------------------------------------- #
# Nightly maintenance (runs automatically at local midnight)
# --------------------------------------------------------------------------- #
def _truncate(path: str) -> None:
    try:
        if os.path.exists(path):
            open(path, "w").close()
    except OSError as exc:
        _log.warning("Could not clear %s: %s", path, exc)


def daily_cleanup() -> None:
    """Clear logs + cache, drop junk rows, compact the file, free memory."""
    _log.info("Running daily cleanup")
    with _lock:
        with _db(write=True) as cur:
            cur.execute("DELETE FROM logs")
            cur.execute("DELETE FROM cache")
            cur.execute("DELETE FROM users WHERE active_project IS NULL OR TRIM(active_project) = ''")
            if USER_RETENTION_DAYS > 0:
                cutoff = int(time.time()) - USER_RETENTION_DAYS * 86400
                cur.execute("DELETE FROM users WHERE last_seen < ?", (cutoff,))

        conn = _get_conn()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA optimize")
            conn.execute("PRAGMA shrink_memory")
        except sqlite3.Error as exc:
            _log.warning("Cleanup maintenance step failed: %s", exc)

    for path in LOG_FILES:
        _truncate(path)
    for pattern in LOG_GLOBS:
        for path in glob.glob(pattern):
            _truncate(path)

    gc.collect()


def _next_midnight() -> datetime:
    return (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _scheduler_loop() -> None:
    while not _stop.is_set():
        target = _next_midnight()
        # Wake every <=30s so clock changes / VPS suspend can't make us oversleep
        while not _stop.is_set():
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            _stop.wait(min(remaining, 30))
        if _stop.is_set():
            return
        try:
            daily_cleanup()
        except Exception:
            _log.exception("Daily cleanup crashed; will retry tomorrow")


def start_scheduler() -> None:
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, name="db-midnight-cleanup", daemon=True)
    _scheduler_thread.start()


def close_db() -> None:
    """Flush the WAL and close cleanly (runs automatically on exit)."""
    _stop.set()
    release()


atexit.register(close_db)

# Set up the database and start the midnight cleaner on import
setup_db()
start_scheduler()
