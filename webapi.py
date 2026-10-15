"""
webapi.py - web control panel for Cloud Runner Panel (vps.py).

Runs INSIDE the bot process (same event loop), so the web panel sees exactly the same
processes, projects and DB as Telegram. It also serves index.html at "/", so the Railway
URL itself is the web app (same origin: no CORS, no second deployment).

Auth (nothing works without it):
  * Admin   : env WEB_PASSWORD  -> full access to every user, terminal, backup, broadcast
  * Users   : /weblogin in the bot gives a one-time code (10 min) -> access to own projects only
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time

import psutil
from aiohttp import web

import db

V = None                                   # the vps module (set by start())
HERE = os.path.dirname(os.path.abspath(__file__))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
SESSION_TTL = 12 * 3600
# Origins allowed to call the API from a browser (the static page is hosted on Netlify, the API on Railway).
# Override with the WEB_ORIGIN env var (comma separated).
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in os.environ.get(
    "WEB_ORIGIN", "https://marvelous-wisp-8b095a.netlify.app").split(",") if o.strip()}
MAX_UP = 200 * 1024 * 1024
MAX_EDIT = 1024 * 1024
SESS: dict[str, dict] = {}
CODES: dict[str, tuple[int, float]] = {}
FAILS: dict[str, list[float]] = {}
_runner: web.AppRunner | None = None
ENABLED = False                            # True once the server is listening (checked by /weblogin)


def new_code(uid: int) -> str:
    """Called by /weblogin in vps.py."""
    now = time.time()
    for c in [c for c, (_, exp) in CODES.items() if exp < now]:
        CODES.pop(c, None)
    code = secrets.token_hex(4).upper()
    CODES[code] = (uid, now + 600)
    return code


# ---------------------------------------------------------------- helpers
def _err(status: int, msg: str) -> web.HTTPException:
    cls = {400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden, 404: web.HTTPNotFound,
           409: web.HTTPConflict, 413: web.HTTPRequestEntityTooLarge, 415: web.HTTPUnsupportedMediaType,
           429: web.HTTPTooManyRequests}.get(status, web.HTTPBadRequest)
    return cls(text=json.dumps({"error": msg}), content_type="application/json")


def J(data=None, **kw) -> web.Response:
    return web.json_response(data if data is not None else kw)


async def args(req: web.Request) -> dict:
    d = dict(req.query)
    if req.can_read_body and (req.content_type or "").startswith("application/json"):
        with contextlib.suppress(Exception):
            d.update(await req.json())
    return d


def admin_only(req) -> None:
    if req["sess"]["role"] != "admin":
        raise _err(403, "admin only")


def who(req, a: dict) -> int:
    s = req["sess"]
    if s["role"] == "user":
        return s["uid"]
    try:
        return int(a.get("uid"))
    except (TypeError, ValueError):
        raise _err(400, "uid required")


def pdir_of(uid: int, proj: str) -> str:
    if not V.valid_project(proj):
        raise _err(400, "invalid project")
    p = V.project_dir(uid, proj)
    if not os.path.isdir(p):
        raise _err(404, "project not found")
    return p


def path_of(pdir: str, rel: str) -> str:
    p = V.safe_join(pdir, rel or ".")
    if not p:
        raise _err(403, "path escapes project")
    return p


def rel_of(pdir: str, full: str) -> str:
    r = os.path.relpath(full, os.path.realpath(pdir))
    return "" if r == "." else r


def key_of(uid, proj, rel) -> str:
    return f"{uid}:{proj}:{rel}"


async def sh(cmd: str, cwd: str, timeout: int = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "bash", "-lc", cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        env={k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "WEB_PASSWORD")})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        V._signal_group(proc, 9)
        return -1, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace")[-30000:]


# ---------------------------------------------------------------- middleware
@web.middleware
async def guard(req: web.Request, handler):
    origin = req.headers.get("Origin", "")
    cors = {}
    if origin.rstrip("/") in ALLOWED_ORIGINS:
        cors = {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS", "Access-Control-Max-Age": "600"}
    if req.method == "OPTIONS":
        return web.Response(status=204, headers=cors)
    if req.path.startswith("/api/") and req.path != "/api/login":
        tok = req.headers.get("Authorization", "")[7:] or (req.query.get("t", "") if req.path == "/api/download" else "")
        s = SESS.get(tok)
        if not s or s["exp"] < time.time():
            SESS.pop(tok, None)
            raise _err(401, "session expired")
        req["sess"] = s
    try:
        resp = await handler(req)
    except web.HTTPException as exc:
        resp = exc
    resp.headers.update(cors)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                         "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


# ---------------------------------------------------------------- auth
async def login(req):
    ip = req.headers.get("X-Forwarded-For", req.remote or "?").split(",")[0].strip()
    now = time.time()
    for k in [k for k, v in FAILS.items() if not v or now - v[-1] > 300]:
        FAILS.pop(k, None)
    recent = [t for t in FAILS.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        raise _err(429, "too many attempts - wait 5 minutes")
    d = await args(req)
    pw, code = str(d.get("password", "")), str(d.get("code", "")).strip().upper()
    if pw and WEB_PASSWORD and hmac.compare_digest(pw.encode(), WEB_PASSWORD.encode()):
        sess = {"role": "admin", "uid": None}
    elif code and code in CODES and CODES[code][1] > now:
        sess = {"role": "user", "uid": CODES.pop(code)[0]}
    else:
        FAILS[ip] = recent + [now]
        await asyncio.sleep(1)
        raise _err(401, "wrong password or code")
    tok = secrets.token_urlsafe(32)
    sess["exp"] = now + SESSION_TTL
    SESS[tok] = sess
    return J(token=tok, role=sess["role"], uid=sess["uid"])


async def logout(req):
    SESS.pop(req.headers.get("Authorization", "")[7:], None)
    return J(ok=True)


# ---------------------------------------------------------------- dashboard / users
def _vitals():
    vm, du = psutil.virtual_memory(), psutil.disk_usage("/")
    return {
        "cpu": psutil.cpu_percent(0.3), "cores": psutil.cpu_count(),
        "ram": {"pct": vm.percent, "used": vm.used, "total": vm.total},
        "disk": {"pct": du.percent, "used": du.used, "total": du.total},
        "panel_mb": psutil.Process().memory_info().rss / V.MB,
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
        "top": [{"mb": r / V.MB, "pid": pid, "cmd": cmd} for r, pid, cmd in V._top_procs(6)],
    }


async def overview(req):
    admin_only(req)
    v = await asyncio.to_thread(_vitals)
    v.update(uptime=time.time() - V.BOT_START_TIME, running=len(V._all_running()),
             users=len(V._all_user_ids()), python=sys.version.split()[0])
    return J(v)


async def _name(uid: int) -> str:
    name = await db.acache_get(f"name:{uid}")
    if not name:
        try:
            chat = await asyncio.wait_for(V.bot.get_chat(uid), 3)
            name = chat.first_name or chat.title or ""
            if name:
                await db.acache_set(f"name:{uid}", name)
        except Exception:
            name = ""
    return name


async def users(req):
    admin_only(req)
    out = []
    for uid in V._all_user_ids():
        out.append({"uid": uid, "name": await _name(uid),
                    "projects": len(V.project_names(uid)), "running": len(V.running_keys(uid)),
                    "active": await db.aget_active_project(uid)})
    return J(users=out)


# ---------------------------------------------------------------- projects
async def projects(req):
    a = await args(req)
    uid = who(req, a)
    active = await db.aget_active_project(uid)
    out = []
    for n in V.project_names(uid):
        pd = V.project_dir(uid, n)
        run = [k.split(":", 2)[2] for k in V.running_keys(uid) if k.split(":", 2)[1] == n]
        out.append({"name": n, "running": run, "active": n == active,
                    "mtime": V._mtime(pd), "env": os.path.isdir(os.path.join(pd, "venv"))})
    return J(projects=out)


async def project_create(req):
    a = await args(req)
    uid, name = who(req, a), str(a.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9\-]{1,40}", name):
        raise _err(400, "name: letters, numbers and - only (max 40)")
    pd = V.project_dir(uid, name)
    if os.path.exists(pd):
        raise _err(409, "project exists")
    url = str(a.get("clone", "")).strip()
    os.makedirs(V.user_dir(uid), exist_ok=True)
    if url:
        if not re.fullmatch(r"https://[\w.\-]+/[\w.\-~%/]+", url):
            raise _err(400, "invalid git url")
        rc, out = await V.run_cmd("git", "clone", "--", url, pd, timeout=600,
                                  env={"GIT_TERMINAL_PROMPT": "0"})
        if rc:
            shutil.rmtree(pd, True)
            raise _err(400, "clone failed: " + out[-300:])
    else:
        os.makedirs(pd)
    await V.run_cmd(sys.executable, "-m", "venv", os.path.join(pd, "venv"), timeout=300)
    await db.aset_active_project(uid, name)
    return J(ok=True)


async def project_delete(req):
    a = await args(req)
    uid, proj = who(req, a), str(a.get("project", ""))
    pd = pdir_of(uid, proj)
    for k in [k for k in list(V.ACTIVE_PROCESSES) if k.startswith(f"{uid}:{proj}:")]:
        await V.stop_process(k)
    await asyncio.to_thread(shutil.rmtree, pd, True)
    if await db.aget_active_project(uid) == proj:
        await db.aset_active_project(uid, None)
    return J(ok=True)


async def project_select(req):
    a = await args(req)
    uid, proj = who(req, a), str(a.get("project", ""))
    pdir_of(uid, proj)
    await db.aset_active_project(uid, proj)
    return J(ok=True)


async def install(req):
    a = await args(req)
    uid, proj = who(req, a), str(a.get("project", ""))
    pd = pdir_of(uid, proj)
    if not os.path.exists(os.path.join(pd, "venv")):
        await V.run_cmd(sys.executable, "-m", "venv", os.path.join(pd, "venv"), timeout=300)
    if not os.path.exists(os.path.join(pd, "requirements.txt")):
        raise _err(404, "no requirements.txt")
    rc, out = await V.run_cmd(V.venv_python(pd), "-m", "pip", "install", "-r", "requirements.txt", cwd=pd)
    return J(ok=rc == 0, output=out)


# ---------------------------------------------------------------- files
async def files(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    target = path_of(pd, a.get("path", ""))
    if not os.path.isdir(target):
        raise _err(404, "not a folder")
    items = []
    for n, is_dir in V.list_dir(target):
        full = os.path.join(target, n)
        st = os.lstat(full)
        rel = rel_of(pd, full)
        items.append({"name": n, "path": rel, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                      "mtime": st.st_mtime, "running": V.is_running(key_of(uid, a["project"], rel))})
    return J(path=rel_of(pd, target), items=items)


async def file_read(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    p = path_of(pd, a.get("path", ""))
    if not os.path.isfile(p):
        raise _err(404, "file not found")
    if os.path.getsize(p) > MAX_EDIT:
        raise _err(413, "file over 1 MB - download it instead")
    raw = await asyncio.to_thread(lambda: open(p, "rb").read())
    if b"\0" in raw:
        raise _err(415, "binary file - download it instead")
    return J(content=raw.decode("utf-8", "replace"))


async def file_write(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    p = path_of(pd, a.get("path", ""))
    if p == os.path.realpath(pd) or os.path.isdir(p):
        raise _err(400, "not a file")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = str(a.get("content", "")).encode("utf-8")
    if len(data) > MAX_EDIT:
        raise _err(413, "file over 1 MB - upload it instead")
    await asyncio.to_thread(lambda: open(p, "wb").write(data))
    return J(ok=True)


async def mkdir(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    os.makedirs(path_of(pd, a.get("path", "")), exist_ok=True)
    return J(ok=True)


async def delete(req):
    a = await args(req)
    uid, proj = who(req, a), str(a.get("project", ""))
    pd = pdir_of(uid, proj)
    p = path_of(pd, a.get("path", ""))
    if p == os.path.realpath(pd):
        raise _err(400, "delete the project instead")
    rel = rel_of(pd, p)
    if any(k == key_of(uid, proj, rel) or k.startswith(key_of(uid, proj, rel) + "/") for k in V.running_keys(uid)):
        raise _err(409, "stop the running script first")
    await asyncio.to_thread(shutil.rmtree if os.path.isdir(p) else os.remove, p)
    return J(ok=True)


async def rename(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    src = path_of(pd, a.get("path", ""))
    new = os.path.basename(str(a.get("new", "")).strip())
    if not new or new in (".", "..") or src == os.path.realpath(pd):
        raise _err(400, "invalid name")
    dst = path_of(pd, os.path.join(os.path.dirname(rel_of(pd, src)), new))
    if os.path.exists(dst):
        raise _err(409, "already exists")
    os.rename(src, dst)
    return J(ok=True)


async def upload(req):
    """multipart: send uid (admin), project, path, extract BEFORE the file parts."""
    reader, f, saved = await req.multipart(), {}, []
    async for part in reader:
        if part.filename is None:
            f[part.name] = await part.text()
            continue
        uid = who(req, f)
        pd = pdir_of(uid, f.get("project", ""))
        name = os.path.basename(part.filename)
        dest = path_of(pd, os.path.join(f.get("path", ""), name))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size = 0
        with open(dest, "wb") as out:
            while chunk := await part.read_chunk(1 << 20):
                size += len(chunk)
                if size > MAX_UP:
                    out.close()
                    os.remove(dest)
                    raise _err(413, "file over 200 MB")
                out.write(chunk)
        if name.lower().endswith(".zip") and f.get("extract") == "1":
            try:
                await asyncio.to_thread(V.safe_extract, dest, os.path.dirname(dest))
            except Exception as exc:
                raise _err(400, f"bad archive: {exc}")
            finally:
                with contextlib.suppress(OSError):
                    os.remove(dest)
        saved.append(name)
    return J(saved=saved)


async def download(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    p = path_of(pd, a.get("path", ""))
    if os.path.isfile(p):
        return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{os.path.basename(p)}"'})
    if not os.path.isdir(p):
        raise _err(404, "not found")
    tmp = os.path.join(tempfile.gettempdir(), f"dl_{secrets.token_hex(6)}.zip")
    await asyncio.to_thread(V.zip_dir, p, tmp)
    asyncio.get_running_loop().call_later(600, lambda: os.path.exists(tmp) and os.remove(tmp))
    name = (os.path.basename(p) or a["project"]) + ".zip"
    return web.FileResponse(tmp, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- processes / logs
async def processes(req):
    s, out = req["sess"], []
    for k in V._all_running():
        uid, proj, rel = V._split_key(k)
        if s["role"] == "user" and uid != s["uid"]:
            continue
        p = V.ACTIVE_PROCESSES[k]
        out.append({"uid": uid, "project": proj, "path": rel, "pid": p.pid,
                    "mb": await asyncio.to_thread(V._proc_mem, p.pid),
                    "up": time.time() - V.START_TIMES.get(k, time.time())})
    return J(processes=out)


async def run(req):
    a = await args(req)
    uid, proj, rel = who(req, a), str(a.get("project", "")), str(a.get("path", "main.py"))
    pd = pdir_of(uid, proj)
    t = path_of(pd, rel)
    if not os.path.isfile(t):
        raise _err(404, "script not found")
    if V.is_running(key_of(uid, proj, rel_of(pd, t))):
        raise _err(409, "already running")
    V.spawn(V.start_process(uid, uid, proj, rel_of(pd, t)))
    return J(ok=True)


async def stop(req):
    a = await args(req)
    uid = who(req, a)
    k = key_of(uid, a.get("project", ""), a.get("path", ""))
    if not V.is_running(k):
        raise _err(404, "not running")
    await V.stop_process(k)
    return J(ok=True)


async def stop_all(req):
    admin_only(req)
    keys = V._all_running()
    for k in keys:
        await V.stop_process(k)
    return J(stopped=len(keys))


async def send_input(req):
    a = await args(req)
    uid = who(req, a)
    proc = V.ACTIVE_PROCESSES.get(key_of(uid, a.get("project", ""), a.get("path", "")))
    if not proc or proc.returncode is not None:
        raise _err(404, "not running")
    try:
        proc.stdin.write((str(a.get("text", "")) + "\n").encode())
        await asyncio.wait_for(proc.stdin.drain(), 10)
    except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
        raise _err(409, "process not accepting input")
    return J(ok=True)


async def logs(req):
    a = await args(req)
    uid = who(req, a)
    pd = pdir_of(uid, str(a.get("project", "")))
    lp = path_of(pd, a.get("file", "run.log"))
    text = await asyncio.to_thread(V.tail_text, lp, 400, 60000) if os.path.isfile(lp) else ""
    return J(text=text, running=[k.split(":", 2)[2] for k in V.running_keys(uid) if k.split(":", 2)[1] == a.get("project")])


async def logs_clear(req):
    a = await args(req)
    uid = who(req, a)
    lp = path_of(pdir_of(uid, str(a.get("project", ""))), "run.log")
    if os.path.isfile(lp):
        open(lp, "w").close()
    return J(ok=True)


# ---------------------------------------------------------------- admin tools
async def terminal(req):
    admin_only(req)
    a = await args(req)
    cmd = str(a.get("cmd", "")).strip()
    if not cmd:
        raise _err(400, "empty command")
    rc, out = await sh(cmd, os.getcwd(), min(int(a.get("timeout", 60) or 60), 300))
    return J(rc=rc, output=out)


async def backup(req):
    admin_only(req)
    V.spawn(V.perform_backup(None))
    return J(ok=True, note="backup started - it will be pinned in the backup channel")


async def broadcast(req):
    admin_only(req)
    text = str((await args(req)).get("text", "")).strip()
    if not text:
        raise _err(400, "empty message")

    async def go():
        for u in await db.aget_all_users():
            with contextlib.suppress(Exception):
                await V.bot.send_message(u, text, parse_mode=None)
            await asyncio.sleep(0.05)
    V.spawn(go())
    return J(ok=True)


async def healthz(_):
    return web.Response(text="ok")


async def index(_):
    return web.FileResponse(os.path.join(HERE, "index.html"))


# ---------------------------------------------------------------- start / stop
def make_app() -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=2 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/healthz", healthz)
    for method, path, fn in [
        ("POST", "/api/login", login), ("POST", "/api/logout", logout),
        ("GET", "/api/overview", overview), ("GET", "/api/users", users),
        ("GET", "/api/projects", projects), ("POST", "/api/project", project_create),
        ("POST", "/api/project/delete", project_delete), ("POST", "/api/project/select", project_select),
        ("POST", "/api/install", install),
        ("GET", "/api/files", files), ("GET", "/api/file", file_read), ("PUT", "/api/file", file_write),
        ("POST", "/api/mkdir", mkdir), ("POST", "/api/delete", delete), ("POST", "/api/rename", rename),
        ("GET", "/api/download", download),
        ("GET", "/api/processes", processes), ("POST", "/api/run", run), ("POST", "/api/stop", stop),
        ("POST", "/api/stopall", stop_all), ("POST", "/api/input", send_input),
        ("GET", "/api/logs", logs), ("POST", "/api/logs/clear", logs_clear),
        ("POST", "/api/terminal", terminal), ("POST", "/api/backup", backup), ("POST", "/api/broadcast", broadcast),
    ]:
        r.add_route(method, path, fn)
    # upload needs a big body limit, so it gets its own sub-app limit via the handler (streamed)
    app.router.add_post("/api/upload", upload)
    return app


async def start(vps_module) -> None:
    global V, _runner, ENABLED
    V = vps_module
    if not WEB_PASSWORD:
        V.log.warning("WEB_PASSWORD is not set -> web panel disabled (set it in Railway variables)")
        return
    app = make_app()
    app._client_max_size = MAX_UP + (1 << 20)          # streamed in 1 MB chunks, never held in RAM
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    await web.TCPSite(_runner, "0.0.0.0", port).start()
    ENABLED = True
    V.log.info("Web panel listening on :%d", port)


async def stop_server() -> None:
    global ENABLED
    ENABLED = False
    if _runner:
        await _runner.cleanup()
