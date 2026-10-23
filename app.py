import os
import sys
import json
import uuid
import html
import shutil
import zipfile
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware


APP_NAME = "ADII X HOST"

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/adii-host"))
BOTS_DIR = DATA_DIR / "bots"
LOGS_DIR = DATA_DIR / "logs"
DB_FILE = DATA_DIR / "bots.json"

for folder in (DATA_DIR, BOTS_DIR, LOGS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")

if not ADMIN_USERNAME or not ADMIN_PASSWORD or not APP_SECRET:
    raise RuntimeError(
        "Set ADMIN_USERNAME, ADMIN_PASSWORD and APP_SECRET in Render."
    )

app = FastAPI(title=APP_NAME)

app.add_middleware(
    SessionMiddleware,
    secret_key=APP_SECRET,
    https_only=True,
    same_site="lax",
    max_age=3600,
)

bots = {}
processes = {}


def load_bots():
    if DB_FILE.exists():
        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                bots.update(data)
        except (OSError, json.JSONDecodeError):
            pass


def save_bots():
    temp = DB_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(bots, indent=2), encoding="utf-8")
    temp.replace(DB_FILE)


load_bots()


def authenticated(request):
    return request.session.get("authenticated") is True


def page(title, body):
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{background:#10131c;color:#eee;font-family:Arial;
max-width:800px;margin:25px auto;padding:16px}}
.card{{background:#202638;padding:18px;margin:15px 0;
border-radius:12px;overflow-wrap:anywhere}}
input,button{{padding:12px;margin:7px 0;max-width:100%;
border-radius:7px}}
input[type=text],input[type=password]{{width:95%}}
button{{background:#16a34a;color:white;border:0;font-weight:bold}}
.danger{{background:#dc2626}}
a{{color:#60a5fa}}
pre{{background:#080a10;padding:12px;white-space:pre-wrap;
overflow-wrap:anywhere}}
</style>
</head>
<body>
<h1>🚀 ADII X HOST</h1>
<p>Personal Bot Hosting Panel</p>
{body}
</body>
</html>""")


def login_required(request):
    if not authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return None


def valid_id(bot_id):
    return bot_id in bots and bot_id.isalnum()


def bot_status(bot_id):
    process = processes.get(bot_id)
    if process is not None and process.poll() is None:
        return "🟢 RUNNING"
    return "🔴 STOPPED"


@app.get("/health")
def health():
    return JSONResponse({"status": "ok", "app": APP_NAME})


@app.get("/login")
def login_page(request: Request):
    if authenticated(request):
        return RedirectResponse("/", status_code=303)

    return page("Admin Login", """
    <div class="card">
    <h2>🔐 Admin Login</h2>
    <form method="post" action="/login">
    <input name="username" placeholder="Username" required>
    <input name="password" type="password" placeholder="Password" required>
    <button type="submit">Login</button>
    </form>
    </div>
    """)


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.session.clear()
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)

    return HTMLResponse("Invalid username or password.", status_code=401)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def dashboard(request: Request):
    redirect = login_required(request)
    if redirect:
        return redirect

    cards = ""

    for bot_id, info in list(bots.items()):
        name = html.escape(str(info.get("name", bot_id)))

        cards += f"""
        <div class="card">
        <h3>🤖 {name}</h3>
        <p>ID: {bot_id}</p>
        <p>Status: {bot_status(bot_id)}</p>

        <form method="post" action="/start/{bot_id}">
        <button type="submit">▶ Start</button>
        </form>

        <form method="post" action="/stop/{bot_id}">
        <button type="submit" class="danger">⏹ Stop</button>
        </form>

        <a href="/logs/{bot_id}">📋 View Logs</a>

        <form method="post" action="/delete/{bot_id}"
        onsubmit="return confirm('Delete this bot?')">
        <button type="submit" class="danger">🗑 Delete</button>
        </form>
        </div>
        """

    if not cards:
        cards = '<div class="card">No bots uploaded yet.</div>'

    body = f"""
    <div class="card">
    <h2>📤 Upload Python Bot</h2>
    <form method="post" action="/upload" enctype="multipart/form-data">
    <input type="file" name="file" accept=".py,.zip" required>
    <button type="submit">Upload Bot</button>
    </form>
    <p>Python files and ZIP archives, up to 20 MB.</p>
    </div>

    <div class="card">
    <h3>🔑 Bot Token</h3>
    <p>Set BOT_TOKEN in Render Environment Variables.
    Never put tokens in public source code.</p>
    </div>

    <h2>📦 Your Bots</h2>
    {cards}
    <p><a href="/logout">Logout</a></p>
    """

    return page(APP_NAME, body)


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    redirect = login_required(request)
    if redirect:
        return redirect

    filename = Path(file.filename or "").name

    if not filename.lower().endswith((".py", ".zip")):
        return HTMLResponse("Only .py and .zip files are allowed.", status_code=400)

    bot_id = uuid.uuid4().hex[:12]
    bot_dir = BOTS_DIR / bot_id
    bot_dir.mkdir(parents=True)

    upload_path = bot_dir / filename
    total_size = 0

    try:
        with upload_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total_size += len(chunk)
                if total_size > 20 * 1024 * 1024:
                    raise ValueError("Upload limit is 20 MB.")

                output.write(chunk)

        if filename.lower().endswith(".zip"):
            if not zipfile.is_zipfile(upload_path):
                raise ValueError("Invalid ZIP archive.")

            source_dir = bot_dir / "source"
            source_dir.mkdir()

            with zipfile.ZipFile(upload_path) as archive:
                members = archive.infolist()

                if len(members) > 500:
                    raise ValueError("ZIP contains too many files.")

                root = source_dir.resolve()
                extracted_size = 0

                for member in members:
                    target = (source_dir / member.filename).resolve()

                    if not target.is_relative_to(root):
                        raise ValueError("Unsafe ZIP path.")

                    extracted_size += member.file_size
                    if extracted_size > 100 * 1024 * 1024:
                        raise ValueError("Extracted ZIP exceeds 100 MB.")

                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ValueError("ZIP symlinks are not supported.")

                archive.extractall(source_dir)

            upload_path.unlink(missing_ok=True)

            preferred = {"main.py", "bot.py", "app.py", "run.py"}
            candidates = [
                path for path in source_dir.rglob("*.py")
                if "__pycache__" not in path.parts
            ]

            if not candidates:
                raise ValueError("ZIP does not contain a Python file.")

            candidates.sort(
                key=lambda path: (
                    path.name.lower() not in preferred,
                    len(path.parts)
                )
            )
            entry = candidates[0]

        else:
            entry = upload_path

        bots[bot_id] = {
            "name": filename,
            "directory": str(bot_dir),
            "entry": str(entry.relative_to(bot_dir))
        }

        save_bots()
        return RedirectResponse("/", status_code=303)

    except Exception as error:
        bots.pop(bot_id, None)
        shutil.rmtree(bot_dir, ignore_errors=True)
        return HTMLResponse(
            "Upload failed: " + html.escape(str(error)),
            status_code=400
        )

    finally:
        await file.close()


@app.post("/start/{bot_id}")
def start_bot(request: Request, bot_id: str):
    redirect = login_required(request)
    if redirect:
        return redirect

    if not valid_id(bot_id):
        return HTMLResponse("Bot not found.", status_code=404)

    process = processes.get(bot_id)
    if process and process.poll() is None:
        return RedirectResponse("/", status_code=303)

    info = bots[bot_id]
    bot_dir = Path(info["directory"]).resolve()
    entry = (bot_dir / info["entry"]).resolve()

    if not entry.is_relative_to(bot_dir) or not entry.is_file():
        return HTMLResponse("Entry file not found.", status_code=400)

    log_file = LOGS_DIR / f"{bot_id}.log"

    try:
        with log_file.open("ab") as log:
            log.write(b"\\n--- Starting bot ---\\n")

            process = subprocess.Popen(
                [sys.executable, "-u", str(entry)],
                cwd=str(bot_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={
                    **os.environ,
                    "BOT_TOKEN": os.getenv("BOT_TOKEN", "")
                }
            )

        processes[bot_id] = process

    except OSError as error:
        return HTMLResponse(
            "Start failed: " + html.escape(str(error)),
            status_code=500
        )

    return RedirectResponse("/", status_code=303)


@app.post("/stop/{bot_id}")
def stop_bot(request: Request, bot_id: str):
    redirect = login_required(request)
    if redirect:
        return redirect

    if not valid_id(bot_id):
        return HTMLResponse("Bot not found.", status_code=404)

    process = processes.get(bot_id)

    if process and process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    processes.pop(bot_id, None)
    return RedirectResponse("/", status_code=303)


@app.get("/logs/{bot_id}")
def view_logs(request: Request, bot_id: str):
    redirect = login_required(request)
    if redirect:
        return redirect

    if not valid_id(bot_id):
        return HTMLResponse("Bot not found.", status_code=404)

    log_file = LOGS_DIR / f"{bot_id}.log"
    log_text = "No logs available."

    if log_file.exists():
        with log_file.open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 10000))
            log_text = log.read().decode("utf-8", errors="replace")

    return page(
        "Bot Logs",
        f'<div class="card"><h2>📋 Logs</h2>'
        f'<pre>{html.escape(log_text)}</pre>'
        f'<a href="/">Back</a></div>'
    )


@app.post("/delete/{bot_id}")
def delete_bot(request: Request, bot_id: str):
    redirect = login_required(request)
    if redirect:
        return redirect

    if not valid_id(bot_id):
        return HTMLResponse("Bot not found.", status_code=404)

    process = processes.get(bot_id)

    if process and process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    processes.pop(bot_id, None)
    info = bots.pop(bot_id)
    save_bots()

    shutil.rmtree(info["directory"], ignore_errors=True)
    (LOGS_DIR / f"{bot_id}.log").unlink(missing_ok=True)

    return RedirectResponse("/", status_code=303)
