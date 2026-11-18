#!/usr/bin/env python3
"""Cross-platform path resolution spec for cursor_paths.py.

Runs anywhere — each case fakes a platform (os.name / sys.platform), plants a
state.vscdb in a temp tree, and asserts the resolver lands on it. So the
Linux/Windows branches are covered from a macOS checkout and vice versa.

    uv run test/paths_spec.py          # or: python3 test/paths_spec.py
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cursor_paths  # noqa: E402

REL_DB = ("User", "globalStorage", "state.vscdb")
_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got  {got}\n       want {want}")
        _fails.append(name)


def plant(root: Path) -> Path:
    """Create <root>/User/globalStorage/{state.vscdb,storage.json}."""
    gs = root.joinpath(*REL_DB[:-1])
    gs.mkdir(parents=True, exist_ok=True)
    gs.joinpath("state.vscdb").write_bytes(b"")
    gs.joinpath("storage.json").write_text("{}")
    return root


def env(**over):
    """Clean slate for every var the resolver reads, plus the case's overrides."""
    base = dict(os.environ)
    for k in (
        "CURSOR_CONFIG_DIR",
        "CURSOR_STATE_DB_PATH",
        "CURSOR_CLI_AUTH_PATH",
        "NEOCURSOR_MACHINE_ID_PATH",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
    ):
        base.pop(k, None)
    over = {k: str(v) for k, v in over.items()}
    # expanduser("~") reads HOME on POSIX but USERPROFILE on Windows, where HOME
    # is ignored outright. A case that sets only HOME therefore tests the
    # runner's real home dir on a Windows host — which is how this spec passed
    # locally and failed in CI. Set both.
    if "HOME" in over:
        over.setdefault("USERPROFILE", over["HOME"])
    base.update(over)
    return mock.patch.dict(os.environ, base, clear=True)


def as_mac():
    return mock.patch.object(sys, "platform", "darwin")


def as_linux():
    return mock.patch.object(sys, "platform", "linux")


def as_windows():
    # only sys.platform is faked — os.name stays "posix" so pathlib keeps
    # building PosixPath, which is what this host can actually instantiate
    return mock.patch.object(sys, "platform", "win32")


def case(title, platform_patch, env_patch, body):
    print(title)
    with platform_patch, env_patch:
        body()


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # --- macOS: ~/Library/Application Support/Cursor ---------------------------
    home = plant(tmp / "mac" / "Library" / "Application Support" / "Cursor").parents[2]
    mac_access, mac_refresh = "mac-cli-access-token", "mac-cli-refresh-token"
    mac_auth = home / ".cursor" / "auth.json"
    mac_auth.parent.mkdir(parents=True)
    mac_auth.write_text(
        json.dumps({"accessToken": mac_access, "refreshToken": mac_refresh}),
        encoding="utf-8",
    )
    case(
        "macOS — Library/Application Support",
        as_mac(),
        env(HOME=home),
        lambda: (
            check(
                "support_dir",
                cursor_paths.support_dir(),
                home / "Library" / "Application Support" / "Cursor",
            ),
            check(
                "state_db",
                cursor_paths.state_db(),
                home / "Library" / "Application Support" / "Cursor" / Path(*REL_DB),
            ),
            # the space must be percent-encoded, and mode=ro kept
            check(
                "uri escapes space",
                "Application%20Support" in cursor_paths.state_db_uri(),
                True,
            ),
            check("uri is read-only", cursor_paths.state_db_uri().endswith("?mode=ro"), True),
            check("cli auth.json", cursor_paths.cli_auth_path(), mac_auth),
            check("cli accessToken", cursor_paths.read_cli_access_token(), mac_access),
            check("cli ignores refresh", cursor_paths.read_cli_access_token() != mac_refresh, True),
        ),
    )

    # --- Linux: XDG_CONFIG_HOME wins over ~/.config ---------------------------
    xdg = plant(tmp / "xdg" / "Cursor").parent
    case(
        "Linux — $XDG_CONFIG_HOME/Cursor",
        as_linux(),
        env(HOME=tmp / "nohome", XDG_CONFIG_HOME=xdg),
        lambda: check("support_dir", cursor_paths.support_dir(), xdg / "Cursor"),
    )

    # --- Linux: default ~/.config/Cursor (the reported bug) -------------------
    lhome = plant(tmp / "lin" / ".config" / "Cursor").parents[1]
    case(
        "Linux — default ~/.config/Cursor",
        as_linux(),
        env(HOME=lhome),
        lambda: check("support_dir", cursor_paths.support_dir(), lhome / ".config" / "Cursor"),
    )

    # --- Linux: lowercase dir from third-party packages ----------------------
    # Asserted by "the db resolves", not by dir name: a case-insensitive host FS
    # (macOS) matches the capitalised candidate against a lowercase directory.
    lc = plant(tmp / "lc" / ".config" / "cursor").parents[1]
    case(
        "Linux — lowercase ~/.config/cursor",
        as_linux(),
        env(HOME=lc),
        lambda: check("state_db resolves", cursor_paths.state_db().is_file(), True),
    )

    # --- Windows: %APPDATA%\Cursor -------------------------------------------
    appdata = plant(tmp / "win" / "AppData" / "Roaming" / "Cursor").parent
    win_access, win_refresh = "win-cli-access-token", "win-cli-refresh-token"
    win_auth = appdata / "Cursor" / "auth.json"
    win_auth.write_text(
        json.dumps({"accessToken": win_access, "refreshToken": win_refresh}),
        encoding="utf-8",
    )
    case(
        "Windows — %APPDATA%\\Cursor",
        as_windows(),
        env(APPDATA=appdata, HOME=tmp / "win"),
        lambda: (
            check("support_dir", cursor_paths.support_dir(), appdata / "Cursor"),
            check("cli auth.json", cursor_paths.cli_auth_path(), win_auth),
            check("cli accessToken", cursor_paths.read_cli_access_token(), win_access),
            check("cli ignores refresh", cursor_paths.read_cli_access_token() != win_refresh, True),
        ),
    )

    # --- Windows: APPDATA unset -> ~/AppData/Roaming --------------------------
    whome = plant(tmp / "win2" / "AppData" / "Roaming" / "Cursor").parents[2]
    whome_access = "win-fallback-cli-access-token"
    whome_auth = whome / "AppData" / "Roaming" / "Cursor" / "auth.json"
    whome_auth.write_text(
        json.dumps({"accessToken": whome_access, "refreshToken": "win-fallback-refresh"}),
        encoding="utf-8",
    )
    case(
        "Windows — APPDATA unset, falls back to ~/AppData/Roaming",
        as_windows(),
        env(HOME=whome),
        lambda: (
            check(
                "support_dir",
                cursor_paths.support_dir(),
                whome / "AppData" / "Roaming" / "Cursor",
            ),
            check("cli auth.json", cursor_paths.cli_auth_path(), whome_auth),
            check("cli accessToken", cursor_paths.read_cli_access_token(), whome_access),
        ),
    )

    # --- Insiders channel ----------------------------------------------------
    ins = plant(tmp / "ins" / ".config" / "Cursor - Insiders").parents[1]
    case(
        "Linux — Cursor - Insiders",
        as_linux(),
        env(HOME=ins),
        lambda: check(
            "support_dir", cursor_paths.support_dir(), ins / ".config" / "Cursor - Insiders"
        ),
    )

    # --- Overrides -----------------------------------------------------------
    custom = plant(tmp / "custom" / "CursorPortable")
    case(
        "CURSOR_CONFIG_DIR overrides everything",
        as_linux(),
        env(HOME=lhome, CURSOR_CONFIG_DIR=custom),
        lambda: (
            check("support_dir", cursor_paths.support_dir(), custom),
            check("state_db", cursor_paths.state_db(), custom / Path(*REL_DB)),
        ),
    )

    odd = tmp / "odd" / "somewhere" / "cursor.sqlite"
    odd.parent.mkdir(parents=True)
    odd.write_bytes(b"")
    case(
        "CURSOR_STATE_DB_PATH points straight at the db",
        as_linux(),
        env(HOME=lhome, CURSOR_STATE_DB_PATH=odd),
        lambda: check("state_db", cursor_paths.state_db(), odd),
    )

    # --- Nothing installed: report the canonical path, don't crash -----------
    empty = tmp / "empty"
    empty.mkdir()
    case(
        "no install — canonical path + actionable error text",
        as_linux(),
        env(HOME=empty),
        lambda: (
            check("support_dir", cursor_paths.support_dir(), empty / ".config" / "Cursor"),
            check(
                "message names the override",
                "CURSOR_CONFIG_DIR" in cursor_paths.not_found_message(),
                True,
            ),
            check(
                "message names the CLI login",
                "agent login" in cursor_paths.not_found_message(),
                True,
            ),
            check(
                "expired message names the fact",
                "already expired" in cursor_paths.cli_expired_message()
                and "about to expire" not in cursor_paths.cli_expired_message(),
                True,
            ),
            check(
                "rejected message names the fact",
                "was rejected" in cursor_paths.cli_rejected_message()
                and "about to expire" not in cursor_paths.cli_rejected_message(),
                True,
            ),
        ),
    )

    # --- Linux CLI auth file (not the desktop database) ----------------------
    cli_secret = "cli-access-token"
    cli_refresh = "cli-refresh-token"
    cli_home = tmp / "cli-home"
    cli_auth = cli_home / ".config" / "cursor" / "auth.json"
    cli_auth.parent.mkdir(parents=True)
    cli_auth.write_text(
        json.dumps({"accessToken": cli_secret, "refreshToken": cli_refresh}),
        encoding="utf-8",
    )

    def diagnostics():
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cursor_paths.print_diagnostics()
        return code, buf.getvalue()

    def cli_default():
        check("cli_auth_path", cursor_paths.cli_auth_path(), cli_auth)
        check("accessToken only", cursor_paths.read_cli_access_token(), cli_secret)
        code, text = diagnostics()
        check("diagnostics say present", "accessToken present" in text, True)
        check("diagnostics hide access token", cli_secret not in text, True)
        check("diagnostics hide refresh token", cli_refresh not in text, True)
        check("diagnostics session is cli", "session       cli\n" in text, True)
        check(
            "diagnostics name machine.json",
            f"machine.json  {cursor_paths.machine_id_path()}  MISSING" in text,
            True,
        )
        check("diagnostics exit 0", code, 0)

    case(
        "Linux — CLI auth.json under ~/.config/cursor",
        as_linux(),
        env(HOME=cli_home),
        cli_default,
    )

    xdg_cli = tmp / "xdg-cli"
    xdg_auth = xdg_cli / "cursor" / "auth.json"
    xdg_auth.parent.mkdir(parents=True)
    xdg_auth.write_text(json.dumps({"accessToken": cli_secret}), encoding="utf-8")
    case(
        "Linux — CLI auth.json under $XDG_CONFIG_HOME",
        as_linux(),
        env(HOME=tmp / "nohome", XDG_CONFIG_HOME=xdg_cli),
        lambda: check("cli_auth_path", cursor_paths.cli_auth_path(), xdg_auth),
    )

    override_auth = tmp / "override-auth.json"
    override_auth.write_text(json.dumps({"refreshToken": cli_refresh}), encoding="utf-8")
    case(
        "CURSOR_CLI_AUTH_PATH with no accessToken",
        as_mac(),
        env(HOME=tmp / "mac-empty", CURSOR_CLI_AUTH_PATH=override_auth),
        lambda: (
            check("cli_auth_path", cursor_paths.cli_auth_path(), override_auth),
            check("no accessToken", cursor_paths.read_cli_access_token(), None),
        ),
    )

    mid_path = tmp / "state" / "machine.json"

    def machine_ids():
        first = cursor_paths.load_or_create_machine_ids()
        second = cursor_paths.load_or_create_machine_ids()
        check("stable", first, second)
        check("machineId length", len(first[0]), 64)
        check("macMachineId length", len(first[1]), 64)
        check("distinct", first[0] != first[1], True)
        check(
            "hex",
            all(c in "0123456789abcdef" for c in first[0] + first[1]),
            True,
        )
        check("persisted", mid_path.is_file(), True)

    case(
        "machine ids are created once and reused",
        as_linux(),
        env(HOME=empty, NEOCURSOR_MACHINE_ID_PATH=mid_path),
        machine_ids,
    )

    # The file is neocursor's, not Cursor's, and it is the same shape on every
    # OS. Lock that default the same way the CLI auth path is locked per OS.
    def expect_machine_id(home: Path):
        check(
            "machine_id_path",
            cursor_paths.machine_id_path(),
            home / ".local" / "state" / "neocursor" / "machine.json",
        )

    for title, platform, home in (
        ("macOS — machine.json under ~/.local/state", as_mac(), tmp / "mid-mac"),
        ("Linux — machine.json under ~/.local/state", as_linux(), tmp / "mid-linux"),
        ("Windows — machine.json under ~/.local/state", as_windows(), tmp / "mid-win"),
    ):
        case(title, platform, env(HOME=home), lambda home=home: expect_machine_id(home))

    xdg_state = tmp / "xdg-state"
    case(
        "Linux — machine.json under $XDG_STATE_HOME",
        as_linux(),
        env(HOME=tmp / "mid-xdg-home", XDG_STATE_HOME=xdg_state),
        lambda: check(
            "machine_id_path",
            cursor_paths.machine_id_path(),
            xdg_state / "neocursor" / "machine.json",
        ),
    )
    case(
        "Windows — machine.json ignores APPDATA",
        as_windows(),
        env(HOME=tmp / "mid-win-appdata", APPDATA=tmp / "appdata"),
        lambda: expect_machine_id(tmp / "mid-win-appdata"),
    )

    def write_desktop_db(root: Path, token):
        gs = root / "User" / "globalStorage"
        gs.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(gs / "state.vscdb")
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        if token is not None:
            con.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                ("cursorAuth/accessToken", token),
            )
        con.commit()
        con.close()
        gs.joinpath("storage.json").write_text(
            json.dumps({"telemetry.machineId": "ab" * 32}),
            encoding="utf-8",
        )

    desktop_token = "desktop-access-token"
    desktop_root = tmp / "desktop-wins"
    write_desktop_db(desktop_root, desktop_token)
    both_auth = tmp / "both-auth.json"
    both_auth.write_text(
        json.dumps({"accessToken": cli_secret, "refreshToken": cli_refresh}),
        encoding="utf-8",
    )

    def desktop_wins():
        code, text = diagnostics()
        check("session desktop", "session       desktop\n" in text, True)
        check("desktop exit 0", code, 0)
        check("no machine.json line", "machine.json" not in text, True)
        check("hides desktop token", desktop_token not in text, True)
        check("hides cli token", cli_secret not in text, True)

    case(
        "diagnostics — desktop token wins over the CLI file",
        as_linux(),
        env(HOME=tmp / "desk-home", CURSOR_CONFIG_DIR=desktop_root, CURSOR_CLI_AUTH_PATH=both_auth),
        desktop_wins,
    )

    unsigned_root = tmp / "unsigned-desktop"
    write_desktop_db(unsigned_root, None)

    def unsigned_blocks_cli():
        code, text = diagnostics()
        check(
            "session stays desktop",
            "session       desktop (state.vscdb has no accessToken; CLI auth is not used)" in text,
            True,
        )
        check("unsigned-in exit 1", code, 1)
        check("hides cli token", cli_secret not in text, True)
        check("no machine.json line", "machine.json" not in text, True)

    case(
        "diagnostics — unsigned-in desktop database ignores the CLI file",
        as_linux(),
        env(
            HOME=tmp / "unsigned-home",
            CURSOR_CONFIG_DIR=unsigned_root,
            CURSOR_CLI_AUTH_PATH=both_auth,
        ),
        unsigned_blocks_cli,
    )

    corrupt_root = tmp / "corrupt-desktop"
    corrupt_db = corrupt_root / "User" / "globalStorage" / "state.vscdb"
    corrupt_db.parent.mkdir(parents=True)
    corrupt_db.write_bytes(b"not a sqlite database")

    def corrupt_blocks_cli():
        code, text = diagnostics()
        check(
            "session stays desktop",
            "session       desktop (state.vscdb is unreadable; CLI auth is not used)" in text,
            True,
        )
        check("unreadable exit 1", code, 1)
        check("hides cli token", cli_secret not in text, True)

    case(
        "diagnostics — unreadable desktop database ignores the CLI file",
        as_linux(),
        env(
            HOME=tmp / "corrupt-home",
            CURSOR_CONFIG_DIR=corrupt_root,
            CURSOR_CLI_AUTH_PATH=both_auth,
        ),
        corrupt_blocks_cli,
    )

    reuse_root = tmp / "cli-reuse"
    reuse_auth = reuse_root / "auth.json"
    reuse_auth.parent.mkdir(parents=True)
    reuse_auth.write_text(json.dumps({"accessToken": cli_secret}), encoding="utf-8")
    reuse_storage = reuse_root / "Cursor" / "User" / "globalStorage" / "storage.json"
    reuse_storage.parent.mkdir(parents=True)
    reuse_storage.write_text(
        json.dumps({"telemetry.machineId": "cd" * 32}),
        encoding="utf-8",
    )

    def cli_reuses_storage():
        code, text = diagnostics()
        check("session cli", "session       cli\n" in text, True)
        check("machine ids come from storage.json", "machine ids   storage.json" in text, True)
        check("no machine.json line", "machine.json" not in text, True)
        check("exit 0", code, 0)
        check("hides cli token", cli_secret not in text, True)

    case(
        "diagnostics — CLI session reuses storage.json machine id",
        as_linux(),
        env(
            HOME=tmp / "reuse-home",
            CURSOR_CONFIG_DIR=reuse_root / "Cursor",
            CURSOR_CLI_AUTH_PATH=reuse_auth,
        ),
        cli_reuses_storage,
    )

    def no_session():
        code, text = diagnostics()
        check("session none", "session       none\n" in text, True)
        check("exit 1", code, 1)

    case(
        "diagnostics — nothing installed",
        as_linux(),
        env(HOME=tmp / "diag-empty", CURSOR_CLI_AUTH_PATH=tmp / "missing-auth.json"),
        no_session,
    )

# --- the URI a real Windows path yields, checkable from any host -------------
# PureWindowsPath is constructible everywhere, so the exact string SQLite will be
# handed on Windows can be asserted from macOS/Linux. This is the form that a
# hand-built f"file:{path}" got wrong: backslashes and a bare drive letter.
print("Windows SQLite URI shape (via PureWindowsPath)")
from pathlib import PureWindowsPath  # noqa: E402

win_uri = PureWindowsPath(
    r"C:\Users\me\AppData\Roaming\Cursor\User\globalStorage\state.vscdb"
).as_uri()
check(
    "drive letter + forward slashes",
    win_uri,
    "file:///C:/Users/me/AppData/Roaming/Cursor/User/globalStorage/state.vscdb",
)
check("no backslashes survive", "\\" not in win_uri, True)

# --- live check on the host, if Cursor is actually installed -----------------
real = cursor_paths.state_db()
print(f"live host ({sys.platform}) — {real}")
print(f"  {'ok  ' if real.is_file() else 'skip'} real state.vscdb present")

print(f"\n{'FAILED: ' + ', '.join(_fails) if _fails else 'all path cases passed'}")
sys.exit(1 if _fails else 0)
