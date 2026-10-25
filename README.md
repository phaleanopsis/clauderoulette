# claude-roulette

Use two (or more) Claude subscriptions from one Claude Code setup. A local proxy picks which account handles each prompt and switches in the middle of a session when that's worth it. Every day at 07:00 the account with less weekly usage gets a tiny Haiku prompt, which starts its 5-hour window.

> [!WARNING]
> **Experimental, LLM-generated, and not reviewed by a human.**
>
> This code was written by an LLM (Claude). No human has read it line by line.
> It handles credentials; read it before trusting it with anything you would mind losing.

## Architecture

```
 claude ── normal login, all data in ~/.claude (never modified)
   │  ANTHROPIC_BASE_URL=http://127.0.0.1:8789
   ▼
 ┌──────────────────────── serve (one launchd process) ─────────────────────────┐
 │ Proxy   /v1/messages → bearer replaced with the chosen account's access token│
 │         everything else (/usage, profile, …) → passed through untouched      │
 │         429 → mark account blocked, retry once on the other                  │
 │ Router  usage per account: /api/oauth/usage every 5 min + live response      │
 │         headers; sticky "use it or lose it" score, primary preferred         │
 │ Tokens  refreshed before expiry; the rotated refresh token is saved back     │
 │ 07:00   least weekly usage → claude -p "Say hello" --model haiku             │
 └──────────────────────────────────────────────────────────────────────────────┘
 Keychain: service "claude-rotator", one item per account, holding that account's OAuth JSON
```

### Credentials (based on how claude-swap handles them, but none of its code)

`setup-token` tokens can only send prompts. They lack the `user:profile` scope, so the usage endpoint answers 403 and they're not used any more. Instead, `add NAME` does this:

1. Starts Claude Code with a temporary, empty `CLAUDE_CONFIG_DIR`. You `/login` as that account and then `/exit`.
2. Reads the credential Claude Code just created, from the Keychain item `Claude Code-credentials-<sha256(dir)[:8]>`, falling back to `<dir>/.credentials.json`.
3. Copies it into the tool's own Keychain item, then **deletes the temporary Keychain item and folder**.

That gives this tool its own login session per account, separate from Claude Code's own login, so the two never fight over a refresh token. Your normal Claude Code login and `~/.claude` are never read or written.

Secrets go to `security` on stdin (hex-encoded via `security -i`), never on a command line where `ps` could see them. They're never logged or written to `state.json`. `refresh` treats only an explicit `invalid_grant` response as a dead token; that account is then excluded until you run `add` again.

### When it switches accounts

Every account gets a score:
- **Active 5h window:** unused % (up to the account's session cap) ÷ hours until reset, so capacity that's about to expire is used first.
- **No active window:** a flat 10.
- **Weekly pacing:** the score is multiplied by how far the account is behind an even 100 %/7 per day (unused weekly % ÷ days until reset, up to `weekly_factor_max`, default 50×). An account with lots left shortly before its weekly reset is therefore drained first.
- **Primary account:** its score is also multiplied by 1.2.
- **Excluded:** an account at ≥ 95 % of its session, ≥ 98 % of its week, rate-limited, or with a dead login.
- **Reserving capacity:** `account_caps` in `config.json` lowers those caps per account, e.g. `{"main": {"session": 80, "weekly": 90}}` keeps 20 % of each 5h window and 10 % of the week on `main` for the chat app.

The proxy stays on the current account unless another one scores **1.5× higher** and at least **15 minutes** have passed since the last switch. A full account is left immediately. This avoids flip-flopping because each switch loses the prompt cache.

## Setup

Claude Code must be logged in normally (any account, `claude` → `/login`).

```bash
python3 claude_rotator.py add work --primary     # follow the prompts: /login as work, then /exit
python3 claude_rotator.py add main               # same for the second account
python3 claude_rotator.py status
python3 claude_rotator.py serve --debug          # first run in the foreground to watch it
python3 claude_rotator.py run                    # in another terminal: claude through the proxy
python3 claude_rotator.py run --account main     # pin a session to one account
```

### Route every Claude Code session (global setting)

Instead of starting sessions with `run`, you can make every Claude Code session use the proxy by adding this to `~/.claude/settings.json`:

```json
"env": {
  "ANTHROPIC_BASE_URL": "http://127.0.0.1:8789"
}
```

- **While the proxy is down, every session that picks up this setting fails** with a connection error. Use it together with the launchd service below.
- **This is a documented setup.** Anthropic's [gateway docs](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways) say that setting only `ANTHROPIC_BASE_URL`, with no gateway credential, keeps the claude.ai login active, so subscription limits and billing apply. The proxy must forward the OAuth `anthropic-beta` value, which it does because it passes headers through unchanged.
- **Terminal `claude` (macOS Terminal, iTerm) is routed** (tested 2026-10-25, Claude Code 2.1.308). In my testing, though, an `ANTHROPIC_BASE_URL` already set in the environment won over the settings file, even though the docs say the settings file should win. Shells started by the Claude desktop app (including its Terminal panel) inherit `ANTHROPIC_BASE_URL=https://api.anthropic.com`, so plain `claude` there goes direct. `claude_rotator.py run` works everywhere because it sets the variable itself.
- **The Claude desktop app's Code tab is not routed, and this tool doesn't support routing it.** According to the [docs](https://code.claude.com/docs/en/llm-gateway-connect#desktop-app), the desktop app ignores `ANTHROPIC_BASE_URL` and `settings.json`. It only uses a gateway through its *third-party inference* configuration (Developer → Configure Third-Party Inference), which relaunches the whole app into "Claude Desktop on 3P" mode:
  - your claude.ai sign-in isn't used, and cloud environments and Remote Control are unavailable;
  - the app sends a fixed gateway API key instead of your subscription login, so making it work would mean disguising API-configured traffic as subscription traffic;
  - the app fails to start its sessions whenever the proxy is down.

  Desktop Code-tab usage still counts toward the signed-in account's limits, and the proxy sees it through its usage checks.
- **claude.ai chat** (desktop or web) can never be routed. It still counts toward the same 5h/weekly limits, and the proxy sees that through its usage checks.
- **To undo it,** delete the `env` block, or restore `~/.claude/settings.json.bak-rotator`.

### Background service and menu bar app

```bash
python3 claude_rotator.py install          # proxy + menu bar app, both start at every login
python3 claude_rotator.py install --no-menubar
python3 claude_rotator.py uninstall        # removes both (credentials and config are kept)
```

`install` sets up two LaunchAgents in `~/Library/LaunchAgents`:

| Agent | What it runs | Restarts |
|---|---|---|
| `com.user.claude-rotator` | `claude_rotator.py serve`, logging to `~/.claude-rotator/serve.log` | always (`KeepAlive`) |
| `com.user.claude-rotator.bar` | `~/Applications/Claude Rotator.app` | only after a crash; choosing **Quit** keeps it closed until the next login |

Both start whenever you log in, so after every restart of the Mac. Re-run `install` after moving the repository or updating Python, because the agent stores the absolute paths.

**Sleep and wake.** Sleep pauses both processes and wake resumes them; nothing needs relaunching. On wake:
- `serve` notices the gap and fetches fresh usage straight away, retrying every minute until the network is back.
- The menu bar app redraws at once.

If the 07:00 ping falls while the Mac is asleep, it runs on wake, up to 09:00. To have the Mac awake for it:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 06:58:00
```

**Menu bar app.** This is a SwiftUI `MenuBarExtra` compiled locally by `install` from [`menubar/ClaudeRotatorBar.swift`](menubar/ClaudeRotatorBar.swift). It needs the Xcode command line tools (`xcode-select --install`), uses only Apple frameworks, and is signed ad hoc.
- **Menu bar:** shows the active account and its 5-hour usage (e.g. `work 12%`), or ⚠ `off` when the proxy isn't running.
- **Panel:** shows each account's 5-hour and weekly bars with reset times, the primary tag, request counts, rate-limit and dead-login warnings, and the next ping time.
- **Buttons:** Ping now, Restart (the service), Log, Folder (`~/.claude-rotator`) and Quit.
- **Credentials:** the app never reads them. It only reads `~/.claude-rotator/state.json` and calls `launchctl` and the script.

### Using it on another machine

Nothing personal lives in this repository. Credentials, account names and usage state stay in `~/.claude-rotator/` and the macOS Keychain on each machine. On a new machine:

```bash
git clone <your repo URL> && cd <repo>
python3 claude_rotator.py add work --primary
python3 claude_rotator.py add main
python3 claude_rotator.py install
```

Each `add` creates a separate login session for that machine, so two machines never share or invalidate each other's refresh tokens. Revoking one machine's login doesn't affect the others. The tool needs macOS for Keychain storage; on Linux it falls back to a chmod-600 `~/.claude-rotator/tokens.json`.

Config: `~/.claude-rotator/config.json`. Only the keys you set are stored; everything else follows the defaults in the script. Log: `~/.claude-rotator/serve.log`.

## Tested vs. unverified

Tested against a fake server:
- importing a login and cleaning up afterwards
- refreshing a token and saving the rotated refresh token
- switching accounts per request, and 429 failover
- passing other requests through untouched, and pinning a session to one account
- detecting a dead login, and keeping secrets out of logs

Tested against your real Keychain: writing, updating, reading and deleting an item.

Verified against Anthropic (2026-10-25, Claude Code 2.1.308):
- `/api/oauth/usage` works with the imported credentials for both accounts.
- Claude Code sends `/v1/messages` to `ANTHROPIC_BASE_URL`; the proxy swaps the token and the reply comes back.
- Rate-limit headers are `anthropic-ratelimit-unified-{5h,7d}-utilization` (a 0–1 fraction) and `-{5h,7d}-reset` (unix seconds), as the parser assumes.

Not yet seen in practice: an actual token refresh against `platform.claude.com`. The first one happens when an access token nears expiry; check `serve.log` for "refreshed access token".

## Terms of service

Both subscriptions must genuinely be yours. Anthropic has said it acts against limit circumvention and account sharing. Automatic failover between accounts is in that grey zone and could get the accounts flagged, so use it at your own risk.
