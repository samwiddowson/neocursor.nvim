# Troubleshooting

[← back to README](../README.md)

Two commands answer most questions before you read any further:

| Command | Shows |
|---|---|
| `:NeocursorDebug` | resolved config, sidecar state, last error, whether this buffer is eligible |
| `:NeocursorLog` | live event stream — requests, responses, suppressions, jumps |

`:NeocursorDebug` prints no token and no buffer contents, so its output is safe
to paste into an issue.

---

## Contents

- [Cursor can't be found](#cursor-cant-be-found)
- [The sidecar won't start](#the-sidecar-wont-start)
- [The sidecar exits after it was working](#the-sidecar-exits-after-it-was-working)
- [No suggestions appear](#no-suggestions-appear)
- [`<Tab>` does nothing](#tab-does-nothing)
- [Suggestions appear but in the wrong place](#suggestions-appear-but-in-the-wrong-place)
- [Filing a bug](#filing-a-bug)

---

## Cursor can't be found

**Symptom:** the sidecar dies at startup, or `:NeocursorDebug` shows an error
mentioning `state.vscdb`, `auth.json`, or a missing config directory.

neocursor reads Cursor's signed-in session off disk. It looks where your
platform puts the desktop app:

| Platform | Cursor data dir |
|---|---|
| macOS | `~/Library/Application Support/Cursor` |
| Linux | `$XDG_CONFIG_HOME/Cursor` → `~/.config/Cursor` |
| Windows | `%APPDATA%\Cursor` |

Insiders builds and the lowercase `cursor` directory some Linux packages create
are detected automatically. The first candidate that actually contains a
`state.vscdb` wins.

### See exactly what's being checked

```sh
uv run cursor_paths.py   # in the plugin directory
```

This prints every candidate path and which one was selected. It's the single
most useful output for any path or auth problem — run it before anything else.

### Point it somewhere else

Portable installs, Flatpak/Snap sandboxes, or Windows-side Cursor seen from WSL
need an explicit path:

```sh
# the Cursor config directory
export CURSOR_CONFIG_DIR="/mnt/c/Users/me/AppData/Roaming/Cursor"   # WSL example

# or aim straight at the SQLite file
export CURSOR_STATE_DB_PATH="/path/to/User/globalStorage/state.vscdb"
```

Set these where Neovim will inherit them — your shell profile, or
`vim.env.CURSOR_CONFIG_DIR = "…"` in your config before `setup()`.

### Cursor CLI instead of the desktop app

If the desktop app is not installed, neocursor reads the Cursor CLI's
`auth.json`. That file is only read. neocursor does not refresh it, does not
write it, and does not read the macOS Keychain.

| Platform | `auth.json` |
|---|---|
| Linux | `~/.config/cursor/auth.json` (`$XDG_CONFIG_HOME/cursor/auth.json` when that variable is set) |
| macOS | `~/.cursor/auth.json` |
| Windows | `%APPDATA%\Cursor\auth.json` |

Linux and Windows write that file on a normal `agent login`. macOS does not:
the default CLI login stays in the Keychain. To use the file there, set
`AGENT_CLI_CREDENTIAL_STORE=file` and run `agent login` again.

Sign in with `agent login` (or `cursor-agent login`). When the desktop
database holds a token, that one wins, so the two can stay on different
accounts. A desktop database that is present but unsigned-in or unreadable
fails as a desktop session. The CLI file has no machine id. The first CLI-only
run writes a stable pair to `$XDG_STATE_HOME/neocursor/machine.json` (default
`~/.local/state/neocursor/machine.json`) and reuses it. A desktop
`storage.json` that already has a machine id still wins. One without a machine
id does not block CLI login. A desktop session still needs its own
`storage.json`.

`uv run cursor_paths.py` names the session the sidecar will use (`desktop`,
`cli`, or `none`). A desktop database that is present but unsigned-in or
unreadable is `desktop`, and the CLI file is not used. A `cli` session that
has to create machine ids also prints `machine.json`. The script never prints
the token.

neocursor never refreshes this token and never calls Cursor's refresh
endpoint. `agent` refreshes `auth.json` on its own schedule. A session keeps
the token it started with until the backend rejects a request; that rejection
re-reads the file once and retries, so a file the CLI has already refreshed
keeps working. When the re-read token is already expired, or the retry is
rejected too, the sidecar exits and does not restart itself. Run `agent
login` if the file is stale, then restart Neovim. The same expired-token
message appears at startup when `auth.json` is already past its expiry. See
[the sidecar exits after it was working](#the-sidecar-exits-after-it-was-working).

```sh
export CURSOR_CLI_AUTH_PATH="/path/to/auth.json"   # unusual location
```

### Signed in?

neocursor uses an existing session; it cannot create one. Open Cursor and
confirm you're logged in, or run `agent login`, before trying again. On macOS,
`agent login` fills the Keychain unless `AGENT_CLI_CREDENTIAL_STORE=file`.

---

## The sidecar won't start

**Symptom:** `:NeocursorDebug` shows `sidecar job : nil` or `(no ready signal)`.

Check `last stderr` in the same output — the sidecar's own error is replayed
there when it dies before signalling ready.

Common causes:

- **`uv` isn't on Neovim's `PATH`.** A GUI-launched Neovim often has a narrower
  `PATH` than your terminal. Verify from inside Neovim: `:echo exepath('uv')`.
  Empty means Neovim can't see it, even if your shell can.
- **First run is still fetching deps.** The sidecar pulls `httpx[http2]` on
  first launch. Give it a few seconds, or pre-warm it:
  ```sh
  uv run --with "httpx[http2]" python -c "import httpx"
  ```
- **A custom `sidecar_cmd` that doesn't work.** Clear the override and retry
  with the default before debugging anything else.

---

## The sidecar exits after it was working

**Symptom:** suggestions stop, and Neovim reports `neocursor: sidecar exited`
followed by one of these lines. Both name the file, never the token.

- `Cursor CLI access token in …/auth.json is already expired. Run agent login.`
  The token's expiry is in the past and the CLI has not written a new one.
  Run `agent login` (or leave `agent` running long enough for it to refresh
  the file), then restart Neovim. The sidecar does not come back on its own.
- `Cursor CLI session in …/auth.json was rejected. Run agent login.`
  The backend rejected the token, and re-reading the file found nothing
  usable — or the same token, and the retry was rejected too. Run
  `agent login`, then restart Neovim.

  One case needs no new login. The re-read happens once, immediately. If
  `agent` was rewriting `auth.json` at that instant, the read can land on an
  empty or half-written file, and that counts as no token even though the
  file is fine a moment later. Run `uv run cursor_paths.py`. When it now says
  `accessToken present`, the read landed mid-write: restart Neovim.

A desktop session is not failed this way. A rejected request there leaves the
sidecar running and that request empty.

---

## No suggestions appear

Work down this list in order:

1. **Is the sidecar up?** `:NeocursorDebug` → `sidecar job` should say `READY`.
   If not, see [above](#the-sidecar-wont-start).

2. **Is this buffer eligible?** Same output, `attach ok`. If it's `false`, check
   the `buffer` line — special buffers (non-empty `buftype`) are skipped by
   design, and a `filetypes` allow-list that doesn't include this filetype will
   exclude it.

3. **Is it being suppressed on purpose?** Check `last suppress`. neocursor
   inherits Cursor's heuristics and deliberately stays quiet while you read or
   navigate rather than firing on every cursor move. Type a little, then pause.

4. **Force one.** `:NeocursorSuggest` bypasses the debounce and requests
   immediately. Suggestions only render in insert mode, so run it from there —
   `<C-o>:NeocursorSuggest<CR>` — or just watch the reply arrive in the log
   (next step): a `RES` line carrying edits means the plumbing is fine and you
   were hitting gating, not a bug.

5. **Watch it live.** `:NeocursorLog` shows requests going out and what comes
   back, including responses dropped as stale.

---

## `<Tab>` does nothing

In Normal mode, that's by design: suggestions only exist in insert mode, and so
does the `<Tab>` mapping. In insert mode, almost always another plugin owns the
mapping. nvim-cmp, blink.cmp, LuaSnip and most snippet engines map `<Tab>` there.

Check who has it:

```vim
:verbose imap <Tab>
```

The output names the file that set it last. If it isn't neocursor, hand the key
over explicitly:

```lua
require("neocursor").setup({ map_tab = false })

-- then, in your own <Tab> mapping, try neocursor first:
if require("neocursor").accept() then return end
-- …otherwise fall through to cmp / blink / snippets
```

`accept()` returns `true` when it consumed the key and `false` when there was
nothing to accept, which makes it safe as the first branch of a chain.

See [Installation](./installation.md#letting-nvim-cmp--blinkcmp-own-tab) for the
full pattern.

---

## Suggestions appear but in the wrong place

If a suggestion lands at the wrong line, or an accept duplicates text, that's a
bug worth reporting — but first confirm it isn't a stale response: `:NeocursorLog`
marks responses dropped because the buffer changed since the request was sent.

Include the log excerpt and, if you can, the sequence of keystrokes that
produced it. This class of bug is almost always about ordering, so the log is
far more useful than a screenshot.

---

## Filing a bug

Include:

- `:NeocursorDebug` output
- `uv run cursor_paths.py` output — required for anything path- or auth-related
- OS, and how Cursor was installed (official build, Flatpak, Snap, AUR, WSL-side)
- `nvim --version`
- `:NeocursorLog` excerpt if the failure is intermittent

→ [open an issue](https://github.com/teocns/neocursor.nvim/issues) ·
[contributing guide](../CONTRIBUTING.md)
