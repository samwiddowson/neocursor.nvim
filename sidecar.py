#!/usr/bin/env python3
"""
neocursor.nvim sidecar — persistent stdio server speaking Cursor's StreamCpp.

Protocol (one JSON object per line, both directions):
  in : {"id":N,"path":str,"content":str,"line":int0,"col":int0,"language":str,
        "additional_files"?:[{path,is_open,last_viewed_at?,ranges:[{start,stop,content}]}],
        "linter_errors"?:{path,errors:[{message,source?,severity,range:{sl,sc,el,ec}}]},
        "file_diff_histories"?:[{file_name,diff_history:[str],diff_history_timestamps:[ms]}]}
  out: {"id":N,"text":str,"range":{...}|null,
        "edits":[{"text":str,"range":{"start":int1,"endInclusive":int1}|null}],
        "prediction":{"path":str,"line":int1}|null}   # text/range == edits[0]
       {"id":N,"error":str}

line/col in the request are 0-indexed (neovim). range in the reply is
1-indexed inclusive (as Cursor's backend returns it).

Launch:  uv run --with 'httpx[http2]' sidecar.py
"""
import io, os, sys, json, base64, time, struct, socket, subprocess, threading
import httpx
import cursor_paths

# Pin the stdio codec instead of inheriting the platform's: Windows text mode
# would rewrite our "\n" record separator to "\r\n" (the client frames replies
# on LF), and a POSIX C locale would give us ASCII, mangling any non-ASCII
# buffer content on the way through.
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", newline="\n", line_buffering=True
)
sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")

# --- resolver resilience -----------------------------------------------------
# macOS mDNSResponder occasionally negative-caches a CNAME hop (Cursor's hosts
# chain api2 -> api2geo -> api2direct), so getaddrinfo fails while `dig` still
# resolves. Fall back to dig and connect by IP; httpx keeps the hostname for
# SNI/cert verification, so TLS stays valid.
_ORIG_GAI = socket.getaddrinfo


def _dig_ipv4(host: str):
    for cmd in (["dig", "+short", host, "A"], ["host", "-t", "A", host]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except Exception:
            continue
        for ln in out.splitlines():
            tok = ln.strip().rstrip(".").split()[-1:] or [""]
            ip = tok[0]
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                return ip
    return None


def _getaddrinfo(host, port, *a, **kw):
    try:
        return _ORIG_GAI(host, port, *a, **kw)
    except socket.gaierror:
        ip = _dig_ipv4(host)
        if not ip:
            raise
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]


if sys.platform == "darwin":  # the bug is mDNSResponder's; elsewhere getaddrinfo is fine
    socket.getaddrinfo = _getaddrinfo
# -----------------------------------------------------------------------------

# The signed-in session lives in Cursor's user-data dir, which moves per
# platform (macOS Application Support / Linux XDG / Windows APPDATA) —
# cursor_paths resolves it.
URL = "https://api2.cursor.sh/aiserver.v1.AiService/StreamCpp"


# "desktop" keeps today's failure mode. "cli" may re-read auth.json after HTTP 401.
_AUTH_FROM = "desktop"


def _desktop_token() -> str | None:
    """cursorAuth/accessToken from the desktop database.

    None means the database file is absent, so the CLI file may be tried.
    A database that is present but unsigned-in or unreadable stays a desktop
    failure: the CLI session can belong to a different account.
    """
    return cursor_paths.read_desktop_access_token()


def _jwt_exp(token: str):
    """Numeric `exp` claim, or None when the token isn't a JWT we can read."""
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    seg = parts[1]
    pad = "=" * ((4 - len(seg) % 4) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg + pad))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


def _token_expired(token: str) -> bool:
    exp = _jwt_exp(token)
    if exp is None:
        return False
    return exp <= time.time()


def _cli_token_for_session(path) -> str | None:
    """CLI accessToken, or None when the file has none.

    A token that has not expired yet is kept, however close `exp` is. A token
    that is already expired raises. Nothing here re-reads the file: the two
    reads would be microseconds apart, and the CLI only refreshes on its own
    schedule. A later HTTP 401 re-reads once. This does not write the file
    and does not call Cursor's refresh endpoint.
    """
    tok = cursor_paths.read_cli_access_token(path)
    if not tok:
        return None
    if _token_expired(tok):
        raise RuntimeError(cursor_paths.cli_expired_message(path))
    return tok


def read_token() -> tuple:
    """(token, source). Desktop SQLite wins; otherwise the CLI auth file."""
    desktop = _desktop_token()
    if desktop:
        return desktop, "desktop"
    tok = _cli_token_for_session(cursor_paths.cli_auth_path())
    if tok:
        return tok, "cli"
    raise RuntimeError(cursor_paths.not_found_message())


def alf(b: bytes) -> bytes:
    o, t = bytearray(b), 0xA5
    for i in range(len(o)):
        o[i] = ((o[i] ^ t) + (i % 256)) & 0xFF
        t = o[i]
    return bytes(o)


def checksum(mid: str, mac: str) -> str:
    ts = int(time.time() * 1000) // 1_000_000
    return base64.b64encode(alf(ts.to_bytes(6, "big"))).decode() + f"{mid}/{mac}"


def frame(b: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(b)) + b


def read_machine_ids(source: str) -> tuple:
    """Desktop storage.json when this session is desktop, or when it has an id.

    The CLI auth file has no telemetry ids. StreamCpp still wants the two
    values that go into x-cursor-checksum, so a CLI-only machine gets a
    stable pair of its own. A leftover storage.json that never received a
    machine id is not a desktop session and must not block that login.
    Desktop sessions keep the requirement that Cursor has written the id.
    Request headers stay the IDE client either way.
    """
    sj_path = cursor_paths.storage_json()
    if not sj_path.is_file():
        if source != "cli":
            try:
                with open(sj_path, encoding="utf-8"):
                    pass
            except OSError as e:
                raise RuntimeError(f"cannot read {sj_path}: {e}")
            raise RuntimeError(f"cannot read {sj_path}")
        return cursor_paths.load_or_create_machine_ids()
    try:
        with open(sj_path, encoding="utf-8") as fh:
            sj = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        if source == "cli":
            return cursor_paths.load_or_create_machine_ids()
        if isinstance(e, json.JSONDecodeError):
            raise RuntimeError(f"cannot read {sj_path}: invalid JSON")
        raise RuntimeError(f"cannot read {sj_path}: {e}")
    mid = sj.get("telemetry.machineId") if isinstance(sj, dict) else None
    if not mid:
        if source == "cli":
            return cursor_paths.load_or_create_machine_ids()
        raise RuntimeError(f"no telemetry.machineId in {sj_path} — launch Cursor once")
    # macMachineId is MAC-address-derived, not macOS-specific: VS Code (and so
    # Cursor) writes it everywhere. Fall back to machineId if it's missing.
    mac = sj.get("telemetry.macMachineId") if isinstance(sj, dict) else None
    return mid, mac or mid


def read_session():
    """Token + machine ids. Desktop session preferred, else the CLI auth file."""
    global _AUTH_FROM
    tok, source = read_token()
    _AUTH_FROM = source
    mid, mac = read_machine_ids(source)
    return tok, mid, mac


try:
    TOK, MID, MAC = read_session()
except Exception as e:
    # The common first-run failure is "Cursor isn't where we looked", and a
    # traceback buries it. The client replays these lines verbatim in :messages.
    sys.stderr.write(f"neocursor sidecar: {e}\n")
    sys.stderr.flush()
    raise SystemExit(1)
CLIENT = httpx.Client(http2=True, timeout=30)
HEADERS = {
    "x-cursor-client-version": "1.1.3",
    "x-cursor-client-type": "ide",
    "connect-protocol-version": "1",
    "content-type": "application/connect+json",
}
STREAM_URL = URL


def _request_headers(content_type: str | None = None) -> dict:
    h = dict(HEADERS)
    h["authorization"] = f"Bearer {TOK}"
    h["x-cursor-checksum"] = checksum(MID, MAC)
    if content_type:
        h["content-type"] = content_type
    return h


def _bail_cli(message: str):
    """The CLI session is still unusable. The message names the file, not the token."""
    sys.stderr.write(f"neocursor sidecar: {message}\n")
    sys.stderr.flush()
    raise SystemExit(1)


def _on_unauthorized() -> bool:
    """Re-read the CLI auth file after this request got HTTP 401.

    Returns True when the caller should retry the request once. Each rejected
    request gets its own re-read; a later request may find that the CLI has
    rotated the file. A re-read that is missing or already expired exits.
    Desktop sessions are not reloaded and are not failed here. Never writes
    auth.json and never calls the refresh endpoint.
    """
    global TOK
    if _AUTH_FROM != "cli":
        return False
    tok = cursor_paths.read_cli_access_token(cursor_paths.cli_auth_path())
    if not tok:
        _bail_cli(cursor_paths.cli_rejected_message())
    if _token_expired(tok):
        _bail_cli(cursor_paths.cli_expired_message())
    TOK = tok
    return True


def fetch_config() -> dict:
    url = URL.rsplit("/", 1)[0] + "/CppConfig"

    def post():
        # CppConfig is unary, not connect+json.
        return CLIENT.post(
            url,
            content=json.dumps({"model": "", "supportsCpt": True}).encode(),
            headers=_request_headers("application/json"),
            timeout=10,
        )

    try:
        r = post()
        if r.status_code == 401 and _on_unauthorized():
            r = post()
        if r.status_code == 401 and _AUTH_FROM == "cli":
            _bail_cli(cursor_paths.cli_rejected_message())
        return r.json() if r.status_code == 200 else {}
    except SystemExit:
        raise
    except Exception:
        return {}


def _addl(a: dict) -> dict:
    # reshape a neutral nvim range-bundle into Cursor's AdditionalFile message
    ranges = a.get("ranges") or []
    out = {
        "relativeWorkspacePath": a.get("path") or "untitled",
        "isOpen": bool(a.get("is_open")),
        "visibleRangeContent": [r.get("content", "") for r in ranges],
        "startLineNumberOneIndexed": [r.get("start", 1) for r in ranges],
        "visibleRanges": [
            {
                "startLineNumber": r.get("start", 1),
                "endLineNumberInclusive": r.get("stop", r.get("start", 1)),
            }
            for r in ranges
        ],
    }
    if a.get("last_viewed_at") is not None:
        out["lastViewedAt"] = a["last_viewed_at"]
    return out


def _fdh(h: dict) -> dict:
    # neutral nvim bundle -> Cursor CppFileDiffHistory
    out = {
        "fileName": h.get("file_name") or "untitled",
        "diffHistory": h.get("diff_history") or [],
    }
    ts = h.get("diff_history_timestamps")
    if ts:
        out["diffHistoryTimestamps"] = ts
    return out


def _linter(l: dict) -> dict:
    # neutral nvim bundle -> Cursor LinterErrors (positions are 0-indexed)
    errors = []
    for e in l.get("errors") or []:
        item = {"message": e.get("message", ""), "severity": e.get("severity", 1)}
        if e.get("source"):
            item["source"] = e["source"]
        r = e.get("range")
        if r:
            item["range"] = {
                "startPosition": {"line": r.get("sl", 0), "column": r.get("sc", 0)},
                "endPosition": {"line": r.get("el", 0), "column": r.get("ec", 0)},
            }
        errors.append(item)
    return {"relativeWorkspacePath": l.get("path") or "untitled", "errors": errors}


def complete(req: dict, cancelled=lambda: False):
    body = {
        "currentFile": {
            "relativeWorkspacePath": req.get("path") or "untitled",
            "contents": req["content"],
            "cursorPosition": {"line": req["line"], "column": req["col"]},
            "languageId": req.get("language") or "plaintext",
        },
        "modelName": "",
        "diffHistory": [],
        "supportsCpt": True,       # opt into Cursor Prediction Target (tab-tab chain)
        "supportsCrlfCpt": True,
    }
    adds = req.get("additional_files") or []
    if adds:
        body["additionalFiles"] = [_addl(a) for a in adds]
    fdh = req.get("file_diff_histories") or []
    if fdh:
        body["fileDiffHistories"] = [_fdh(h) for h in fdh]
    lint = req.get("linter_errors")
    if lint and lint.get("errors"):
        body["linterErrors"] = _linter(lint)

    # The multidiff model streams a SEQUENCE of edits in one response, each
    # bracketed by beginEdit/doneEdit, plus a cursorPredictionTarget pointing at
    # the chain's first jump. Split them into an ordered list so the client can
    # walk the chain locally (one Tab per edit) with no extra round-trips.
    # A CLI-session 401 re-reads auth.json and retries this request once.
    for attempt in (1, 2):
        h = _request_headers()
        edits, cur, prediction = [], None, None

        def flush():
            nonlocal cur
            if cur is not None and (cur["text"] or cur["range"] is not None):
                t = cur["text"]
                if cur["eol"]:  # shouldRemoveLeadingEol: drop the leading newline
                    if t.startswith("\r\n"):
                        t = t[2:]
                    elif t.startswith("\n"):
                        t = t[1:]
                edits.append({"text": t, "range": cur["range"]})
            cur = None

        def feed(flag, msg):
            nonlocal cur, prediction
            if flag & 0x02:
                return
            try:
                j = json.loads(msg)
            except Exception:
                return
            if j.get("beginEdit"):
                flush()
                cur = {"text": "", "range": None, "eol": False}
                return
            if "text" in j:
                if cur is None:
                    cur = {"text": "", "range": None, "eol": False}
                cur["text"] += j.get("text", "")
                return
            rr = j.get("rangeToReplace")
            if rr:
                if cur is None:
                    cur = {"text": "", "range": None, "eol": False}
                cur["range"] = {
                    "start": rr.get("startLineNumber"),
                    "endInclusive": rr.get("endLineNumberInclusive"),
                }
                if j.get("shouldRemoveLeadingEol"):
                    cur["eol"] = True
                return
            cpt = j.get("cursorPredictionTarget")
            if cpt and prediction is None:
                prediction = {
                    "path": cpt.get("relativePath"),
                    "line": cpt.get("lineNumberOneIndexed"),
                }
            if j.get("doneEdit"):
                flush()

        # Stream the reply and bail between chunks once superseded: exiting the
        # `with` closes the HTTP/2 stream, i.e. Cursor's cancelCpp-on-abort. The
        # connect frames (1B flag + 4B len) can split across chunks, so buffer.
        buf = b""
        with CLIENT.stream(
            "POST", STREAM_URL, content=frame(json.dumps(body).encode()), headers=h
        ) as r:
            if r.status_code == 401 and attempt == 1 and _on_unauthorized():
                continue
            if r.status_code == 401 and _AUTH_FROM == "cli":
                _bail_cli(cursor_paths.cli_rejected_message())
            for chunk in r.iter_bytes():
                if cancelled():
                    return None
                buf += chunk
                while len(buf) >= 5:
                    ln = struct.unpack(">I", buf[1:5])[0]
                    if len(buf) < 5 + ln:
                        break
                    feed(buf[0], buf[5 : 5 + ln])
                    buf = buf[5 + ln :]
        if cancelled():
            return None
        flush()  # in case the stream ends mid-edit

        first = edits[0] if edits else {"text": "", "range": None}
        return {
            "text": first["text"],
            "range": first["range"],
            "edits": edits,
            "prediction": prediction,
        }


def main():
    global STREAM_URL
    cfg = fetch_config()
    if cfg.get("cppUrl"):
        STREAM_URL = cfg["cppUrl"].rstrip("/") + "/aiserver.v1.AiService/StreamCpp"
    sys.stderr.write("neocursor sidecar ready\n")
    sys.stderr.flush()
    sys.stdout.write(json.dumps({"config": {
        "debounce": cfg.get("clientDebounceDurationMillis"),
        "exclude_patterns": cfg.get("excludeRecentlyViewedFilesPatterns") or [],
        "heuristics": cfg.get("heuristics") or [],
        "reject_hard": (cfg.get("recentlyRejectedEditThresholds") or {}).get("hardRejectThreshold"),
        "max_cleared": cfg.get("maxNumberOfClearedSuggestionsSinceLastAccept"),
        "is_fused": cfg.get("isFusedCursorPredictionModel"),
    }}) + "\n")
    sys.stdout.flush()
    # Requests are served concurrently, newest-wins: each arrival bumps `gen`,
    # which every older worker polls between stream chunks and bails on (the
    # editor's abort-and-refire — Cursor's cancelCpp). Without this the loop is
    # serial, and during a typing burst every reply describes a buffer two
    # keystrokes old: the client bins it as stale and the user sees nothing.
    wlock = threading.Lock()
    latest = {"gen": 0}

    def emit(obj):
        with wlock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def serve(req, my_gen):
        rid = req.get("id")
        if os.environ.get("NC_DUMP"):  # debug: tee each incoming request
            with open(os.environ["NC_DUMP"], "a") as fh:
                fh.write(json.dumps(req) + "\n")

        def cancelled():
            return latest["gen"] != my_gen

        try:
            res = complete(req, cancelled)
        except SystemExit:
            # _bail_cli in a worker thread would only kill the worker. The
            # editor should see the sidecar exit, not hang on a silent request.
            os._exit(1)
        except Exception as e:
            # a stream torn down mid-abort raises; that's a cancel, not an error
            if cancelled():
                emit({"id": rid, "aborted": True})
            else:
                emit({"id": rid, "error": str(e)})
            return
        if res is None or cancelled():
            emit({"id": rid, "aborted": True})
        else:
            res["id"] = rid
            emit(res)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            emit({"id": None, "error": str(e)})
            continue
        latest["gen"] += 1
        threading.Thread(target=serve, args=(req, latest["gen"]), daemon=True).start()


if __name__ == "__main__":
    main()
