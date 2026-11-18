#!/usr/bin/env python3
r"""Locate Cursor's per-user data directory on macOS, Linux and Windows.

Cursor is an Electron app, so its user-data root is Electron's `userData` —
`appData` plus the app name — i.e. the same layout VS Code uses with "Code"
swapped for "Cursor":

    macOS     ~/Library/Application Support/Cursor
    Linux     $XDG_CONFIG_HOME/Cursor          (default ~/.config/Cursor)
    Windows   %APPDATA%\Cursor                 (…\AppData\Roaming\Cursor)

Below that root the two files the sidecar needs are always at the same relative
paths, on every platform:

    User/globalStorage/state.vscdb    auth token (SQLite)
    User/globalStorage/storage.json   telemetry machine ids

The Cursor CLI (`agent` / `cursor-agent`) keeps a separate login file, which
is not the desktop database. It is read-only here. The desktop database wins
when it holds a token. `CURSOR_CLI_AUTH_PATH` overrides the CLI file location.

    macOS     ~/.cursor/auth.json
              (written only when AGENT_CLI_CREDENTIAL_STORE=file; the default
              macOS store is the Keychain, which is not read)
    Linux     $XDG_CONFIG_HOME/cursor/auth.json (default ~/.config/cursor/auth.json)
    Windows   %APPDATA%\Cursor\auth.json       (…\AppData\Roaming\Cursor\auth.json)

Candidates are probed in order and the first one that actually holds a
state.vscdb wins, which covers Insiders builds and the lowercase directory some
third-party Linux packages create. Two env vars override the desktop root, for
portable installs, WSL and sandboxed packages:

    CURSOR_CONFIG_DIR       the Cursor root shown above
    CURSOR_STATE_DB_PATH    full path to a state.vscdb

A CLI login has no telemetry ids. The first CLI-only run creates them once
under $XDG_STATE_HOME/neocursor/machine.json (default
~/.local/state/neocursor/machine.json). `NEOCURSOR_MACHINE_ID_PATH` overrides
that file. A desktop session still requires storage.json.

Run this file directly for a diagnostic dump (handy in bug reports):

    uv run cursor_paths.py

The dump names the session the sidecar will use — desktop, cli, or none —
and never prints a token. A desktop database that is present but unsigned-in
or unreadable stays desktop, so a CLI file beside it is not a usable session.
A CLI session that has to create machine ids also prints that file.
"""
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

_REL_DB = ("User", "globalStorage", "state.vscdb")
_REL_JSON = ("User", "globalStorage", "storage.json")

# Stable, Insiders, and the all-lowercase variant seen in some community Linux
# packages. Order matters: first hit with a state.vscdb wins.
_APP_DIRS = ("Cursor", "Cursor - Insiders", "cursor")


def _home() -> Path:
    # expanduser resolves HOME on POSIX and USERPROFILE / HOMEDRIVE+HOMEPATH on
    # Windows, so this stays correct under both without branching.
    return Path(os.path.expanduser("~"))


def _appdata_roots() -> list:
    """Platform `appData` dirs — the parents of the "Cursor" directory."""
    # Dispatch on sys.platform, not os.name: under Cygwin/MSYS os.name lies, and
    # pathlib keys its concrete class off os.name, so tests can't fake it.
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata)] if appdata else [_home() / "AppData" / "Roaming"]
    if sys.platform == "darwin":
        return [_home() / "Library" / "Application Support"]
    # Linux and the BSDs: XDG first, then the config roots that Flatpak and
    # Snap sandboxes relocate to (neither is an official Cursor channel, but
    # both are cheap to probe and users do build them).
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return [
        Path(xdg) if xdg else _home() / ".config",
        _home() / ".var" / "app" / "co.anysphere.cursor" / "config",
        _home() / "snap" / "cursor" / "current" / ".config",
    ]


def candidates() -> list:
    """Every directory we'd accept as Cursor's user-data root, best first."""
    env = os.environ.get("CURSOR_CONFIG_DIR")
    if env:
        return [Path(env).expanduser()]
    db = os.environ.get("CURSOR_STATE_DB_PATH")
    if db:
        p = Path(db).expanduser()
        # …/<root>/User/globalStorage/state.vscdb -> <root>
        return [p.parents[2]] if len(p.parents) >= 3 else [p.parent]
    return [root / app for root in _appdata_roots() for app in _APP_DIRS]


def support_dir() -> Path:
    """Cursor's user-data root. Falls back to the canonical path for this OS."""
    cands = candidates()
    for c in cands:
        if c.joinpath(*_REL_DB).is_file():
            return c
    return cands[0]


def state_db() -> Path:
    """Path to state.vscdb (the SQLite store holding cursorAuth/accessToken)."""
    db = os.environ.get("CURSOR_STATE_DB_PATH")
    if db:
        return Path(db).expanduser()
    return support_dir().joinpath(*_REL_DB)


def state_db_uri(read_only: bool = True) -> str:
    """A SQLite URI for state_db().

    as_uri() percent-encodes the space in "Application Support" and emits the
    file:///C:/… form Windows needs; SQLite decodes both. Building the URI by
    hand breaks on one platform or the other.
    """
    uri = state_db().absolute().as_uri()
    return uri + "?mode=ro" if read_only else uri


def storage_json() -> Path:
    """Path to storage.json (telemetry.machineId / telemetry.macMachineId)."""
    return support_dir().joinpath(*_REL_JSON)


def cli_auth_path() -> Path:
    """Cursor CLI auth.json. ``CURSOR_CLI_AUTH_PATH`` overrides on any platform.

    Matches the CLI file store for domain ``cursor``:

        macOS     ~/.cursor/auth.json
        Linux     $XDG_CONFIG_HOME/cursor/auth.json or ~/.config/cursor/auth.json
        Windows   %APPDATA%\\Cursor\\auth.json

    On macOS the CLI writes that file only when
    ``AGENT_CLI_CREDENTIAL_STORE=file``. The default macOS store is the
    Keychain, which this function does not read. Anything that is not macOS
    or Windows uses the Linux path, which is what the CLI does.
    """
    env = os.environ.get("CURSOR_CLI_AUTH_PATH")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return _home() / ".cursor" / "auth.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata).expanduser() if appdata else _home() / "AppData" / "Roaming"
        return base / "Cursor" / "auth.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else _home() / ".config"
    return base / "cursor" / "auth.json"


def read_cli_access_token(path: Path | None = None) -> str | None:
    """Return the CLI file's accessToken, or None.

    ``refreshToken`` is intentionally ignored. This never writes the file.
    """
    if path is None:
        path = cli_auth_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tok = data.get("accessToken")
    if isinstance(tok, str) and tok:
        return tok
    return None


def _cli_auth_file(path: Path | None) -> Path:
    return path if path is not None else cli_auth_path()


def cli_expired_message(path: Path | None = None) -> str:
    """The access token's exp is already past. Names the file, never the token.

    A token that has not expired yet is not this case; the sidecar keeps it.
    """
    where = _cli_auth_file(path)
    return (
        f"Cursor CLI access token in {where} is already expired. "
        "Run `agent login`."
    )


def cli_rejected_message(path: Path | None = None) -> str:
    """The backend rejected the CLI session. Names the file, never the token."""
    where = _cli_auth_file(path)
    return (
        f"Cursor CLI session in {where} was rejected. "
        "Run `agent login`."
    )


def read_desktop_access_token() -> str | None:
    """cursorAuth/accessToken from state.vscdb, or None when that file is absent.

    A database that is present but unsigned-in raises RuntimeError. An
    unreadable database raises sqlite3.Error. Either one blocks the CLI file:
    that login can belong to a different account.
    """
    db = state_db()
    if not db.is_file():
        return None
    con = sqlite3.connect(state_db_uri(), uri=True)
    try:
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'"
        ).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        raise RuntimeError(
            f"no cursorAuth/accessToken in {db} — is Cursor signed in?"
        )
    val = row[0]
    if isinstance(val, bytes):
        val = val.decode("utf-8", "replace")
    if isinstance(val, str) and val:
        return val
    raise RuntimeError(f"no cursorAuth/accessToken in {db} — is Cursor signed in?")


def cli_mints_machine_ids() -> bool:
    """True when a CLI session would write machine.json.

    storage.json with telemetry.machineId is reused instead. Invalid JSON, a
    non-object, or a missing id is not a desktop machine id.
    """
    sj_path = storage_json()
    if not sj_path.is_file():
        return True
    try:
        data = json.loads(sj_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    return not data.get("telemetry.machineId")


def machine_id_path() -> Path:
    """Where neocursor persists machine ids when storage.json is absent."""
    env = os.environ.get("NEOCURSOR_MACHINE_ID_PATH")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else _home() / ".local" / "state"
    return base / "neocursor" / "machine.json"


def _valid_machine_id(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def load_or_create_machine_ids() -> tuple:
    """Stable (machineId, macMachineId), both 64-char hex.

    Created once. A later call returns the same pair. Never touches the CLI
    auth file or the desktop database.
    """
    path = machine_id_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            mid = data.get("machineId")
            mac = data.get("macMachineId")
            if _valid_machine_id(mid) and _valid_machine_id(mac):
                return mid, mac
    mid = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    mac = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"machineId": mid, "macMachineId": mac}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return mid, mac


def searched() -> list:
    """Human-readable candidate list, for "not found" errors."""
    return [str(c) for c in candidates()]


def not_found_message() -> str:
    lines = ["Cursor's state.vscdb not found. Looked for:"]
    lines += [f"  {c}{os.sep}" + os.sep.join(_REL_DB) for c in candidates()]
    lines.append(
        "Is Cursor installed and signed in? If it lives somewhere unusual, set "
        "CURSOR_CONFIG_DIR (the Cursor data dir) or CURSOR_STATE_DB_PATH."
    )
    path = cli_auth_path()
    lines.append(
        f"No Cursor CLI accessToken in {path}. Sign in with `agent login`, "
        "or set CURSOR_CLI_AUTH_PATH."
    )
    return "\n".join(lines)


def _desktop_auth_state() -> str:
    """absent, present, unsigned-in, or unreadable. Never the token."""
    try:
        tok = read_desktop_access_token()
    except sqlite3.Error:
        return "unreadable"
    except RuntimeError:
        return "unsigned-in"
    return "absent" if tok is None else "present"


def print_diagnostics() -> int:
    """Print resolved paths and which session the sidecar will use.

    Never prints a token. Returns 0 only when that session can start: a
    desktop database with an access token, or no desktop database and a CLI
    access token. A desktop file that is unsigned-in or unreadable returns 1
    even if auth.json also holds a token.
    """
    db = state_db()
    print(f"platform      {sys.platform} (os.name={os.name})")
    print(f"support_dir   {support_dir()}")
    print(f"state.vscdb   {db}  {'OK' if db.is_file() else 'MISSING'}")
    sj = storage_json()
    print(f"storage.json  {sj}  {'OK' if sj.is_file() else 'MISSING'}")
    print(f"sqlite uri    {state_db_uri()}")
    path = cli_auth_path()
    cli_ok = False
    if not path.is_file():
        print(f"cli auth      {path}  MISSING")
    elif read_cli_access_token(path):
        cli_ok = True
        print(f"cli auth      {path}  accessToken present")
    else:
        print(f"cli auth      {path}  no accessToken")
    desktop = _desktop_auth_state()
    if desktop == "present":
        print("session       desktop")
        ok = True
    elif desktop == "unsigned-in":
        print("session       desktop (state.vscdb has no accessToken; CLI auth is not used)")
        ok = False
    elif desktop == "unreadable":
        print("session       desktop (state.vscdb is unreadable; CLI auth is not used)")
        ok = False
    elif cli_ok:
        print("session       cli")
        if cli_mints_machine_ids():
            mid = machine_id_path()
            print(f"machine.json  {mid}  {'OK' if mid.is_file() else 'MISSING'}")
        else:
            print("machine ids   storage.json")
        ok = True
    else:
        print("session       none")
        ok = False
    print("candidates:")
    for c in searched():
        print(f"  {c}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(print_diagnostics())
