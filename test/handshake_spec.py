#!/usr/bin/env python3
"""Startup-contract spec for the real sidecar, on any OS, with no credentials.

    uv run test/handshake_spec.py

Launches sidecar.py exactly as init.lua does, pointed at a synthesized Cursor
install (see fake_cursor_home.py), and asserts the parts that are platform-
specific and therefore easy to break:

  * the resolver finds Cursor where this OS puts it
  * SQLite opens state.vscdb through a file:// URI (backslashes, drive letters,
    the space in "Application Support")
  * stdout is framed with LF, never CRLF — Windows text mode would break the
    client's line splitting
  * a request still gets a framed reply (an auth error here, since the token is
    fake) rather than killing the process
  * a missing install exits 1 with an actionable message, not a traceback

The live backend is deliberately out of scope: that path is httpx over TLS and
is not platform-specific.
"""
import base64
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))
import fake_cursor_home  # noqa: E402

# Same launch shape as init.lua's sidecar_cmd; override for a preinstalled env.
_override = os.environ.get("NC_SIDECAR_CMD")
CMD = (_override.split() if _override else ["uv", "run", "--with", "httpx[http2]"]) + [
    str(ROOT / "sidecar.py")
]

_fails = []


def check(name, got, want=True):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got  {got!r}\n       want {want!r}")
        _fails.append(name)


class Sidecar:
    """Line-pumped subprocess; keeps raw bytes so CRLF is observable."""

    def __init__(self, cursor_dir):
        env = dict(os.environ, CURSOR_CONFIG_DIR=str(cursor_dir))
        env.pop("CURSOR_STATE_DB_PATH", None)
        # Keep a developer machine's real CLI login out of this process.
        env["CURSOR_CLI_AUTH_PATH"] = str(Path(cursor_dir) / "no-cli-auth.json")
        env["NEOCURSOR_MACHINE_ID_PATH"] = str(Path(cursor_dir) / "machine.json")
        self.p = subprocess.Popen(
            CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        self.q = queue.Queue()
        for tag, stream in (("out", self.p.stdout), ("err", self.p.stderr)):
            threading.Thread(target=self._pump, args=(tag, stream), daemon=True).start()

    def _pump(self, tag, stream):
        for raw in iter(stream.readline, b""):
            self.q.put((tag, raw))
        self.q.put((tag, None))

    def wait_for(self, tag, pred, timeout=60):
        """Next line on `tag` matching pred (raw bytes), or None on timeout."""
        end = time.time() + timeout
        while time.time() < end:
            try:
                t, raw = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if raw is None:  # stream closed; the other one may still speak
                continue
            if t == tag and pred(raw):
                return raw
        return None

    def send(self, obj):
        assert self.p.stdin, "stdin pipe closed"
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def close(self):
        self.p.kill()
        self.p.wait(timeout=10)


print(f"launch: {' '.join(CMD)}")

# --- happy path: synthesized install ----------------------------------------
with tempfile.TemporaryDirectory() as td:
    cursor_dir = Path(td) / "Cursor"
    fake_cursor_home.build(cursor_dir)
    print(f"synthesized install — {cursor_dir}")

    sc = Sidecar(cursor_dir)
    try:
        ready = sc.wait_for("err", lambda b: b"ready" in b, timeout=120)
        check("sidecar reaches ready", ready is not None)

        cfg = sc.wait_for("out", lambda b: b.strip().startswith(b"{"), timeout=60)
        check("emits a config line on stdout", cfg is not None)
        if cfg:
            # The Windows assertion: text mode would have made this b"}\r\n".
            check("stdout framed with LF, not CRLF", cfg.endswith(b"\n") and not cfg.endswith(b"\r\n"))
            check("config line is JSON with a config key", "config" in json.loads(cfg.decode()))

        sc.send(
            {
                "id": 42,
                "path": "x.py",
                "content": "def f():\n    pass\n",
                "line": 1,
                "col": 8,
                "language": "python",
            }
        )
        reply = sc.wait_for("out", lambda b: b'"id": 42' in b or b'"id":42' in b, timeout=90)
        check("request gets a framed reply", reply is not None)
        if reply:
            j = json.loads(reply.decode())
            # A fake token can only produce an error — what matters is that the
            # process framed it and stayed alive instead of dying.
            check("reply is an error (fake token), not a crash", "error" in j or "text" in j)
            check("process still alive after the error", sc.p.poll() is None)
    finally:
        sc.close()

# --- failure path: nothing installed ----------------------------------------
with tempfile.TemporaryDirectory() as td:
    print("missing install")
    env = dict(os.environ, CURSOR_CONFIG_DIR=td)
    env.pop("CURSOR_STATE_DB_PATH", None)
    env["CURSOR_CLI_AUTH_PATH"] = str(Path(td) / "no-cli-auth.json")
    env["NEOCURSOR_MACHINE_ID_PATH"] = str(Path(td) / "machine.json")
    r = subprocess.run(CMD, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=300)
    err = r.stderr.decode(errors="replace")
    check("exits non-zero", r.returncode != 0)
    check("names state.vscdb", "state.vscdb" in err)
    check("points at the override", "CURSOR_CONFIG_DIR" in err)
    check("names agent login", "agent login" in err)
    check("no python traceback", "Traceback" not in err)


def _b64(obj) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(exp: float) -> str:
    return _b64({"alg": "none"}) + "." + _b64({"exp": int(exp)}) + ".sig"


def _py_prefix():
    """Same uv/python prefix the sidecar launch uses, without sidecar.py."""
    if _override:
        return _override.split()
    return ["uv", "run", "--with", "httpx[http2]"]


def _run_py(script: str, env: dict, timeout=180):
    return subprocess.run(
        _py_prefix() + ["python", "-c", script],
        capture_output=True,
        env=env,
        timeout=timeout,
    )


_CLI_SESSION = r"""
import json, os, sys
sys.path.insert(0, os.environ["ROOT"])
import cursor_paths
import sidecar

def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)

expect = os.environ["EXPECT_TOKEN"]
refresh = os.environ["REFRESH_TOKEN"]
if sidecar.TOK != expect:
    die("token mismatch")
if refresh and refresh in sidecar.TOK:
    die("refresh token was used")
if sidecar._AUTH_FROM != os.environ["EXPECT_SOURCE"]:
    die("unexpected auth source")
if os.environ["EXPECT_SOURCE"] == "cli":
    if len(sidecar.MID) != 64 or len(sidecar.MAC) != 64 or sidecar.MID == sidecar.MAC:
        die("machine ids")
    if cursor_paths.load_or_create_machine_ids() != (sidecar.MID, sidecar.MAC):
        die("machine ids not stable")
if sidecar.HEADERS.get("x-cursor-client-type") != "ide":
    die("client type changed")
print("session-ok")
"""

_CLI_BEHAVIOR = r"""
import base64, io, json, os, sys, time
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.environ["ROOT"])
import sidecar

def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)

def b64(obj):
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

def jwt(exp):
    return b64({"alg": "none"}) + "." + b64({"exp": int(exp)}) + ".sig"

def write_auth(token):
    Path(auth_path).write_text(
        json.dumps({"accessToken": token, "refreshToken": refresh}),
        encoding="utf-8",
    )

auth_path = os.environ["AUTH_PATH"]
refresh = os.environ["REFRESH_TOKEN"]
soon = jwt(time.time() + 120)
write_auth(soon)
if sidecar._cli_token_for_session(auth_path) != soon:
    die("not-yet-expired token was dropped")

past = jwt(time.time() - 30)
write_auth(past)
try:
    sidecar._cli_token_for_session(auth_path)
except RuntimeError as e:
    text = str(e)
    if (
        "already expired" not in text
        or "about to expire" in text
        or "agent login" not in text
        or past in text
    ):
        die("expired token message")
else:
    die("expired token was accepted")

rotated = jwt(time.time() + 90000)
calls = []

class Resp:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = {} if payload is None else payload
    def json(self):
        return self._payload

def post(url, content=None, headers=None, timeout=None):
    calls.append(headers.get("authorization", ""))
    if len(calls) == 1:
        write_auth(rotated)
        return Resp(401)
    return Resp(200, {"cppUrl": "https://example.invalid"})

with mock.patch.object(sidecar.CLIENT, "post", post):
    cfg = sidecar.fetch_config()
if cfg.get("cppUrl") != "https://example.invalid" or len(calls) != 2:
    die("CppConfig did not retry once")
if calls[0].endswith(rotated) or not calls[1].endswith(rotated):
    die("CppConfig retry did not use the re-read token")

streamed = jwt(time.time() + 100000)
stream_calls = []

class Stream:
    def __init__(self, code):
        self.status_code = code
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def iter_bytes(self):
        return iter(())

def stream(method, url, content=None, headers=None):
    stream_calls.append(headers.get("authorization", ""))
    if len(stream_calls) == 1:
        write_auth(streamed)
        return Stream(401)
    return Stream(200)

req = {"path": "x.py", "content": "x\n", "line": 0, "col": 0, "language": "python"}
with mock.patch.object(sidecar.CLIENT, "stream", stream):
    res = sidecar.complete(req)
if res is None or len(stream_calls) != 2:
    die("StreamCpp did not retry once")
if stream_calls[0].endswith(streamed) or not stream_calls[1].endswith(streamed):
    die("StreamCpp retry did not use the re-read token")

# A later rejection gets its own re-read. Nothing is reset between requests.
again_tok = jwt(time.time() + 120000)
again = []

def post_again(url, content=None, headers=None, timeout=None):
    again.append(headers.get("authorization", ""))
    if len(again) == 1:
        write_auth(again_tok)
        return Resp(401)
    return Resp(200, {"cppUrl": "https://example.invalid/again"})

with mock.patch.object(sidecar.CLIENT, "post", post_again):
    cfg = sidecar.fetch_config()
if cfg.get("cppUrl") != "https://example.invalid/again" or len(again) != 2:
    die("a later 401 did not re-read")
if again[0].endswith(again_tok) or not again[1].endswith(again_tok):
    die("later 401 retry did not use the re-read token")

rejects = {"n": 0}

def reject(url, content=None, headers=None, timeout=None):
    rejects["n"] += 1
    return Resp(401)

reject_err = io.StringIO()
try:
    with redirect_stderr(reject_err), mock.patch.object(sidecar.CLIENT, "post", reject):
        sidecar.fetch_config()
except SystemExit:
    pass
else:
    die("a second rejection did not exit")
if rejects["n"] != 2:
    die("a repeated 401 did not retry once before exiting")
reject_text = reject_err.getvalue()
if "was rejected" not in reject_text or "about to expire" in reject_text or again_tok in reject_text:
    die("repeated 401 message")

expired_now = jwt(time.time() - 10)

def reject_expired(url, content=None, headers=None, timeout=None):
    write_auth(expired_now)
    return Resp(401)

expired_err = io.StringIO()
try:
    with redirect_stderr(expired_err), mock.patch.object(sidecar.CLIENT, "post", reject_expired):
        sidecar.fetch_config()
except SystemExit:
    pass
else:
    die("expired re-read did not exit")
expired_text = expired_err.getvalue()
if (
    "already expired" not in expired_text
    or "about to expire" in expired_text
    or expired_now in expired_text
):
    die("expired re-read message")
print("cli-behavior-ok")
"""

_DESKTOP_401 = r"""
import os, sys
from unittest import mock
sys.path.insert(0, os.environ["ROOT"])
import sidecar

def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)

class Resp:
    status_code = 401
    def json(self):
        return {"nope": True}

def post(url, content=None, headers=None, timeout=None):
    return Resp()

try:
    with mock.patch.object(sidecar.CLIENT, "post", post):
        cfg = sidecar.fetch_config()
except SystemExit:
    die("desktop CppConfig 401 exited")
if cfg != {}:
    die("desktop CppConfig 401 changed the empty-config path")
if sidecar.TOK != os.environ["EXPECT_TOKEN"]:
    die("desktop 401 replaced the desktop token")

class Stream:
    status_code = 401
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def iter_bytes(self):
        return iter(())

def stream(method, url, content=None, headers=None):
    return Stream()

req = {"path": "x.py", "content": "x\n", "line": 0, "col": 0, "language": "python"}
try:
    with mock.patch.object(sidecar.CLIENT, "stream", stream):
        sidecar.complete(req)
except SystemExit:
    die("desktop StreamCpp 401 exited")
print("desktop-401-ok")
"""


def _isolated_env(root: Path, **extra) -> dict:
    env = dict(os.environ)
    env.pop("CURSOR_STATE_DB_PATH", None)
    env["ROOT"] = str(ROOT)
    env["HOME"] = str(root)
    env["USERPROFILE"] = str(root)
    env["CURSOR_CONFIG_DIR"] = str(root / "empty-cursor")
    env["NEOCURSOR_MACHINE_ID_PATH"] = str(root / "machine.json")
    (root / "empty-cursor").mkdir(exist_ok=True)
    env.update(extra)
    return env


print("CLI auth file, no desktop database")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    access = "cli-access-token.fake"
    refresh = "cli-refresh-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, refresh)
    env = _isolated_env(
        root,
        CURSOR_CLI_AUTH_PATH=str(auth),
        EXPECT_TOKEN=access,
        REFRESH_TOKEN=refresh,
        EXPECT_SOURCE="cli",
        AUTH_PATH=str(auth),
    )
    r = _run_py(_CLI_SESSION, env)
    out = r.stdout.decode(errors="replace")
    err = r.stderr.decode(errors="replace")
    check("cli session starts", r.returncode == 0 and "session-ok" in out)
    if r.returncode != 0:
        print(err)
    check("cli session hides access token", access not in out and access not in err)
    check("cli session hides refresh token", refresh not in out and refresh not in err)

    r = _run_py(_CLI_BEHAVIOR, env)
    out = r.stdout.decode(errors="replace")
    err = r.stderr.decode(errors="replace")
    blob = out + err
    check("cli expiry reload and 401 retry", r.returncode == 0 and "cli-behavior-ok" in out)
    if r.returncode != 0:
        print(err)
    check("behavior output hides access token", access not in blob)
    check("behavior output hides refresh token", refresh not in blob)

print("desktop database wins over the CLI file")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cursor_dir = root / "Cursor"
    fake_cursor_home.build(cursor_dir)
    access = "cli-access-token.fake"
    refresh = "cli-refresh-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, refresh)
    env = _isolated_env(
        root,
        CURSOR_CONFIG_DIR=str(cursor_dir),
        CURSOR_CLI_AUTH_PATH=str(auth),
        EXPECT_TOKEN=fake_cursor_home.TOKEN,
        REFRESH_TOKEN=refresh,
        EXPECT_SOURCE="desktop",
        AUTH_PATH=str(auth),
    )
    r = _run_py(_CLI_SESSION, env)
    out = r.stdout.decode(errors="replace")
    err = r.stderr.decode(errors="replace")
    check("desktop token wins", r.returncode == 0 and "session-ok" in out)
    if r.returncode != 0:
        print(err)
    check("precedence hides both tokens", fake_cursor_home.TOKEN not in out + err and access not in out + err)

print("CLI token that has not expired yet still starts")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    soon = _jwt(time.time() + 120)
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, soon, "refresh-not-printed")
    env = _isolated_env(root, CURSOR_CLI_AUTH_PATH=str(auth))
    r = _run_py("import os, sys\nsys.path.insert(0, os.environ['ROOT'])\nimport sidecar\n", env)
    blob = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
    check("not-yet-expired token starts", r.returncode == 0)
    if r.returncode != 0:
        print(blob)
    check("not-yet-expired token is not printed", soon not in blob)

print("already-expired CLI token exits without printing it")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    expired = _jwt(time.time() - 30)
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, expired, "refresh-not-printed")
    env = _isolated_env(root, CURSOR_CLI_AUTH_PATH=str(auth))
    r = _run_py("import os, sys\nsys.path.insert(0, os.environ['ROOT'])\nimport sidecar\n", env)
    err = r.stderr.decode(errors="replace")
    out = r.stdout.decode(errors="replace")
    check("expired token exits", r.returncode != 0)
    check("expired token says already expired", "already expired" in err)
    check("expired token does not say about to expire", "about to expire" not in err, True)
    check("expired token names agent login", "agent login" in err)
    check("expired token names the auth file", str(auth) in err)
    check("expired token is not printed", expired not in out and expired not in err)
    check("expired token has no traceback", "Traceback" not in err)

print("CLI login ignores storage.json without a machine id")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    access = "cli-access-token.fake"
    refresh = "cli-refresh-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, refresh)
    env = _isolated_env(
        root,
        CURSOR_CLI_AUTH_PATH=str(auth),
        EXPECT_TOKEN=access,
        REFRESH_TOKEN=refresh,
        EXPECT_SOURCE="cli",
    )
    leftover = Path(env["CURSOR_CONFIG_DIR"]) / "User" / "globalStorage" / "storage.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("{}", encoding="utf-8")
    r = _run_py(_CLI_SESSION, env)
    out = r.stdout.decode(errors="replace")
    err = r.stderr.decode(errors="replace")
    check("leftover storage.json still starts", r.returncode == 0 and "session-ok" in out)
    if r.returncode != 0:
        print(err)
    check("leftover storage.json mints machine ids", Path(env["NEOCURSOR_MACHINE_ID_PATH"]).is_file(), True)
    check("leftover storage.json hides tokens", access not in out + err and refresh not in out + err)

_IMPORT_SIDECAR = "import os, sys\nsys.path.insert(0, os.environ['ROOT'])\nimport sidecar\n"

print("desktop database without storage.json")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cursor_dir = root / "Cursor"
    fake_cursor_home.build(cursor_dir)
    storage = cursor_dir / "User" / "globalStorage" / "storage.json"
    storage.unlink()
    mid = root / "machine.json"
    env = _isolated_env(
        root,
        CURSOR_CONFIG_DIR=str(cursor_dir),
        CURSOR_CLI_AUTH_PATH=str(root / "no-cli-auth.json"),
        NEOCURSOR_MACHINE_ID_PATH=str(mid),
    )
    r = _run_py(_IMPORT_SIDECAR, env)
    err = r.stderr.decode(errors="replace")
    check("missing storage.json exits", r.returncode != 0)
    check("missing storage.json names the file", str(storage) in err)
    check("missing storage.json does not mint machine ids", not mid.is_file(), True)
    check("missing storage.json has no traceback", "Traceback" not in err)

print("unsigned-in desktop database ignores the CLI file")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cursor_dir = root / "Cursor"
    db = fake_cursor_home.build(cursor_dir)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM ItemTable WHERE key='cursorAuth/accessToken'")
    con.commit()
    con.close()
    access = "cli-access-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, "cli-refresh-token.fake")
    mid = root / "machine.json"
    env = _isolated_env(
        root,
        CURSOR_CONFIG_DIR=str(cursor_dir),
        CURSOR_CLI_AUTH_PATH=str(auth),
        NEOCURSOR_MACHINE_ID_PATH=str(mid),
    )
    r = _run_py(_IMPORT_SIDECAR, env)
    err = r.stderr.decode(errors="replace")
    out = r.stdout.decode(errors="replace")
    check("unsigned-in desktop exits", r.returncode != 0)
    check("unsigned-in desktop names the database", "cursorAuth/accessToken" in err and str(db) in err)
    check("unsigned-in desktop asks about sign-in", "signed in" in err)
    check("unsigned-in desktop ignores the CLI token", access not in out + err)
    check("unsigned-in desktop does not mint machine ids", not mid.is_file(), True)
    check("unsigned-in desktop has no traceback", "Traceback" not in err)

print("unreadable desktop database ignores the CLI file")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cursor_dir = root / "Cursor"
    db = fake_cursor_home.build(cursor_dir)
    db.write_bytes(b"not a sqlite database")
    access = "cli-access-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, "cli-refresh-token.fake")
    mid = root / "machine.json"
    env = _isolated_env(
        root,
        CURSOR_CONFIG_DIR=str(cursor_dir),
        CURSOR_CLI_AUTH_PATH=str(auth),
        NEOCURSOR_MACHINE_ID_PATH=str(mid),
    )
    r = _run_py(_IMPORT_SIDECAR, env)
    err = r.stderr.decode(errors="replace")
    out = r.stdout.decode(errors="replace")
    check("corrupt database exits", r.returncode != 0)
    check("corrupt database reports sqlite", "database" in err.lower())
    check("corrupt database ignores the CLI token", access not in out + err)
    check("corrupt database does not mint machine ids", not mid.is_file(), True)
    check("corrupt database has no traceback", "Traceback" not in err)

print("desktop 401 keeps today's failure mode")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cursor_dir = root / "Cursor"
    fake_cursor_home.build(cursor_dir)
    access = "cli-access-token.fake"
    auth = root / "auth.json"
    fake_cursor_home.build_cli_auth(auth, access, "cli-refresh-token.fake")
    env = _isolated_env(
        root,
        CURSOR_CONFIG_DIR=str(cursor_dir),
        CURSOR_CLI_AUTH_PATH=str(auth),
        EXPECT_TOKEN=fake_cursor_home.TOKEN,
    )
    r = _run_py(_DESKTOP_401, env)
    out = r.stdout.decode(errors="replace")
    err = r.stderr.decode(errors="replace")
    check("desktop 401 stays up", r.returncode == 0 and "desktop-401-ok" in out)
    if r.returncode != 0:
        print(err)
    check("desktop 401 hides both tokens", fake_cursor_home.TOKEN not in out + err and access not in out + err)

print(f"\n{'FAILED: ' + ', '.join(_fails) if _fails else 'handshake contract holds'}")
sys.exit(1 if _fails else 0)
