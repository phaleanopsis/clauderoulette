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
 │         headers; "use it or lose it" score, primary preferred                │
 │ Pins    each conversation stays on one account (its prompt cache lives      │
 │         there); new and cache-cold ones go where there's most room          │
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

### One account per conversation

The API keeps no conversation state: every request re-sends the whole conversation, so nothing is lost when a conversation changes account. What *is* per account is the prompt cache (each subscription is its own organization). The first turn after a move re-sends the whole context uncached, about 10× the cost of a cached turn on a 150k-token conversation. So the proxy decides per conversation, not globally ([`conversations.py`](src/claude_rotator/conversations.py)):

| Situation | Account |
|---|---|
| New conversation | best score after discounting its active conversations' load: score ÷ (1 + their context / `pin_load_scale`) |
| Subagent | its parent's |
| Cache warm | stays; if the account fills up, you're asked first (below) |
| Cache cold (idle past its TTL) | free to move to the best account; if only the long-TTL part (e.g. a 1h system prompt) may still be cached, only when another scores `switch_ratio` (1.5×) higher |

Conversations are identified by the session id in `metadata.user_id`: Claude Code sends JSON with `session_id` (and `parent_session_id` for a subagent); add-ons recognise other clients. The cache TTL comes from the request's own `cache_control` marks (5 min or 1 h). Turns from the main agent (requests with tools) record the conversation's context size; side calls like titles don't. Requests without an id fall back to one shared current account, which only changes when another scores **1.5× higher** and **15 minutes** have passed.

**Asking before moving.** For the questions below, the main agent's request is *held* while a dialog asks, and the answer decides where that very request goes. While it waits (up to `pin_hold_s`, 120 s), the proxy has already answered with an event stream and sends `ping` events every 10 s, as Anthropic's own stream does, so neither client's first-byte watchdog gives up. No answer in time: the request is sent as before. Side calls and subagents are never held.

| Trigger | Buttons | The held request |
|---|---|---|
| Warm conversation of 20k+ tokens, account past 85 % of its 5h window | **Switch and send on B account** / Send on current account / Cancel sending | goes to B (one cache rebuild); or stays on A, and you're asked again once A is full; or is cancelled |
| Account full | **Switch and send on B account** / Cancel sending | goes to B, so no usage-limit error and nothing to re-send; or is cancelled |
| Context passes 150k tokens (once per conversation) | **Send prompt anyway** / Cancel sending | is sent; or is cancelled |

The proxy can't start a new conversation itself: the history lives in the client, which re-sends all of it with every message. So *Cancel sending* only stops this message (nothing reaches Anthropic), and the client shows "Message not sent: you cancelled it in Claude Rotator. To start fresh, type /clear (or /compact to keep a summary). To continue this conversation, send the message again." (in Claude Science: "open a new conversation"). After a cancel in the first two dialogs, the next new conversation is placed on B; after a cancel because A is full, the current conversation moves to B as well, so `/compact` or a resend works. For the held request only, the client doesn't see Anthropic's response headers (an error from Anthropic arrives as a stream `error` event instead).

`claude-rotator status` lists recent conversations with client, account, context size and whether the cache is warm. Pins are kept in `~/.claude-rotator/conversation-pins.json` for 30 days.

| Key (`config.json`) | Default | Meaning |
|---|---|---|
| `code_mode` | `"pin"` | Claude Code: `"pin"`, `"observe"` (log session ids, use the shared account) or `"off"` |
| `pin_active_s` | `3600` | how long after its last turn a conversation counts toward its account's load |
| `pin_load_scale` | `100000` | context tokens of active conversations that halve an account's score |
| `pin_warn_pct` | `85` | 5h usage at which a big warm conversation's account triggers the question |
| `pin_warn_min_context` | `20000` | smaller conversations are never asked about |
| `pin_ask_every_s` | `600` | the same conversation isn't asked again sooner |
| `pin_hold_s` | `120` | how long a request waits for an answer |
| `pin_hint_tokens` | `150000` | context size for the one-time Continue / Start fresh question (`0` turns it off) |

### How much do cache reads count?

Anthropic doesn't document how cache reads, cache writes, uncached input and output weigh against the 5h and weekly limits. Every proxied turn appends its token counts and the account's utilization headers to `~/.claude-rotator/usage-log.jsonl` (no content). `claude-rotator measure` fits the change in utilization between consecutive turns on an account against the tokens of the earlier turn, per model family, and reports each token type relative to uncached input. The headers only have whole-percent precision and traffic outside the proxy adds noise, so give it a few hundred turns.

## Setup

You need [uv](https://docs.astral.sh/uv/), and Claude Code logged in normally (any account, `claude` → `/login`). The package has no dependencies beyond the Python standard library (Python ≥ 3.10).

```bash
git clone <your repo URL> && cd <repo>
uv tool install --editable .              # puts `claude-rotator` on your PATH (~/.local/bin)

claude-rotator add work --primary         # follow the prompts: /login as work, then /exit
claude-rotator add main                   # same for the second account
claude-rotator status
claude-rotator install                    # background proxy + menu bar app + client settings (below)
```

The install is editable, so the command always runs the code in this checkout. To add the optional [Claude Science add-on](#add-ons), install `'.[science]'` instead of `.`. If you'd rather not install a global command, run every command as `uv run claude-rotator …` from the repository instead.

To try it without installing anything:

```bash
claude-rotator serve --debug              # run the proxy in the foreground to watch it
claude-rotator run                        # in another terminal: claude through the proxy
claude-rotator run --account main         # pin a session to one account
```

### Updating

```bash
git pull
claude-rotator install
```

`install` is safe to re-run at any time. It rebuilds the menu bar app, rewrites both LaunchAgents with the current paths, restarts them, and leaves settings that are already correct untouched. Sessions that go through the proxy fail for a second or two while it restarts.

If you installed with `uv tool install` *without* `--editable`, run `uv tool install --reinstall .` (or `'.[science]'`) before `install`. A non-editable install is also the way to keep the repository in `~/Documents`: macOS doesn't let the launchd service read files there.

### Route every Claude Code session (done by `install`)

By default `install` points Claude Code at the proxy in two places:

1. **`~/.claude/settings.json`** (terminal `claude`). It adds or updates:

   ```json
   "env": {
     "ANTHROPIC_BASE_URL": "http://127.0.0.1:8789"
   }
   ```

2. **VS Code user settings** (the Claude Code extension), at `~/Library/Application Support/Code/User/settings.json`, plus Code - Insiders, Cursor and VSCodium if those are installed. It adds or updates:

   ```json
   "claudeCode.environmentVariables": [
     { "name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:8789" }
   ]
   ```

How the edits are made:
- **Only that one key changes.** Its other entries (other env vars, other extension variables) are kept. Comments and formatting in the file stay exactly as they were, because VS Code's settings file is JSONC and is edited as text, not reloaded and re-dumped.
- **A backup is made first.** Before the first change, the original is saved next to the file as `settings.json.bak-rotator`. Later reinstalls never overwrite that backup.
- **Broken files are skipped.** A file that can't be parsed (e.g. a missing comma) is left alone and reported in the `install` output.
- **Opting out:** `--no-claude-settings` and `--no-vscode-settings` skip either file.
- **Undoing it:** `uninstall` removes the entries again, but only if they still point at this proxy.

Points to know:
- **While the proxy is down, every session that picks up these settings fails** with a connection error. That's why `install` sets them together with the background service.
- **This is a documented setup.** Anthropic's [gateway docs](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways) say that setting only `ANTHROPIC_BASE_URL`, with no gateway credential, keeps the claude.ai login active, so subscription limits and billing apply. The proxy must forward the OAuth `anthropic-beta` value, which it does because it passes headers through unchanged.
- **Terminal `claude` (macOS Terminal, iTerm) is routed** (tested 2026-10-25, Claude Code 2.1.308). In my testing, though, an `ANTHROPIC_BASE_URL` already set in the environment won over the settings file, even though the docs say the settings file should win. Shells started by the Claude desktop app (including its Terminal panel) inherit `ANTHROPIC_BASE_URL=https://api.anthropic.com`, so plain `claude` there goes direct. `claude-rotator run` works everywhere because it sets the variable itself.
- **The Claude desktop app's Code tab is not routed, and this tool doesn't support routing it.** According to the [docs](https://code.claude.com/docs/en/llm-gateway-connect#desktop-app), the desktop app ignores `ANTHROPIC_BASE_URL` and `settings.json`. It only uses a gateway through its *third-party inference* configuration (Developer → Configure Third-Party Inference), which relaunches the whole app into "Claude Desktop on 3P" mode:
  - your claude.ai sign-in isn't used, and cloud environments and Remote Control are unavailable;
  - the app sends a fixed gateway API key instead of your subscription login, so making it work would mean disguising API-configured traffic as subscription traffic;
  - the app fails to start its sessions whenever the proxy is down.

  Desktop Code-tab usage still counts toward the signed-in account's limits, and the proxy sees it through its usage checks.
- **claude.ai chat** (desktop or web) can never be routed. It still counts toward the same 5h/weekly limits, and the proxy sees that through its usage checks.
- **To undo it by hand,** delete the entries, or restore the `.bak-rotator` copies.

### Background service and menu bar app

```bash
claude-rotator install          # proxy + menu bar app, both start at every login, + client settings
claude-rotator install --no-menubar
claude-rotator uninstall        # removes both and the client settings (credentials and config are kept)
```

`install` sets up two LaunchAgents in `~/Library/LaunchAgents`:

| Agent | What it runs | Restarts |
|---|---|---|
| `com.user.claude-rotator` | `<python> -m claude_rotator serve`, logging to `~/.claude-rotator/serve.log` | always (`KeepAlive`) |
| `com.user.claude-rotator.bar` | `~/Applications/Claude Rotator.app` | only after a crash; choosing **Quit** keeps it closed until the next login |

Both start whenever you log in, so after every restart of the Mac. The agent stores the absolute path of the Python that ran `install`. Re-run `install` after moving the repository or reinstalling the tool; `status` warns you when the service runs a different installation.

**Sleep and wake.** Sleep pauses both processes and wake resumes them; nothing needs relaunching. On wake:
- `serve` notices the gap and fetches fresh usage straight away, retrying every minute until the network is back.
- The menu bar app redraws at once.

If the 07:00 ping falls while the Mac is asleep, it runs on wake, up to 09:00. To have the Mac awake for it:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 06:58:00
```

**Menu bar app.** This is a SwiftUI `MenuBarExtra` compiled locally by `install` from [`src/claude_rotator/menubar/ClaudeRotatorBar.swift`](src/claude_rotator/menubar/ClaudeRotatorBar.swift), which ships inside the package. It needs the Xcode command line tools (`xcode-select --install`), uses only Apple frameworks, and is signed ad hoc.
- **Menu bar:** shows the active account and its 5-hour usage (e.g. `work 12%`), or ⚠ `off` when the proxy isn't running.
- **Panel:** shows each account's 5-hour and weekly bars with reset times, the primary tag, request counts, rate-limit and dead-login warnings, and the next ping time.
- **Buttons:** Ping now, Restart (the service), Log, Folder (`~/.claude-rotator`) and Quit.
- **Credentials:** the app never reads them. It only reads `~/.claude-rotator/state.json` and calls `launchctl` and the CLI.

### Using it on another machine

Nothing personal lives in this repository. Credentials, account names and usage state stay in `~/.claude-rotator/` and the macOS Keychain on each machine. On a new machine:

```bash
git clone <your repo URL> && cd <repo>
uv tool install --editable .
claude-rotator add work --primary
claude-rotator add main
claude-rotator install
```

Each `add` creates a separate login session for that machine, so two machines never share or invalidate each other's refresh tokens. Revoking one machine's login doesn't affect the others. The tool needs macOS for Keychain storage; on Linux it falls back to a chmod-600 `~/.claude-rotator/tokens.json`.

Config: `~/.claude-rotator/config.json`. Only the keys you set are stored; everything else follows `DEFAULTS` in [`src/claude_rotator/config.py`](src/claude_rotator/config.py). Log: `~/.claude-rotator/serve.log`.

## Add-ons

Add-ons are separate packages under [`packages/`](packages/). The base install contains none of their code; each one is installed as an extra of `claude-rotator`, and `serve` picks up every installed add-on automatically.

| Extra | Package | What it does |
|---|---|---|
| `science` | [`claude-rotator-science`](packages/claude-rotator-science/README.md) | keeps each Claude Science conversation on one account, so its prompt cache isn't lost |

```bash
uv tool install --editable '.[science]'   # add it (or switch back with `uv tool install --editable .`)
claude-rotator install                    # restart the service so it loads the change
```

An add-on registers a factory under the `claude_rotator.addons` entry-point group; [`addons.py`](src/claude_rotator/addons.py) documents the interface. It can claim an inference request and hand the core its `Conversation` (which the core then pins like a Claude Code session), force an account, pass it through on the client's own login, or receive the token counts afterwards.

## Development

```bash
uv sync                  # creates .venv with the package, all add-ons (all editable) and the dev tools
uv run pytest            # unit tests, plus the proxy against a fake upstream; never touches the real Keychain
                         # (also runs each add-on's tests under packages/*/tests)
uv run ruff check
```

| Module | Responsibility |
|---|---|
| `config.py` | paths under `~/.claude-rotator`, `DEFAULTS`, config/state files |
| `credentials.py` | Keychain / token file, OAuth refresh, importing a login |
| `usage.py` | usage endpoint and rate-limit headers, the account score |
| `router.py` | account scores; the shared account for requests without a conversation id |
| `conversations.py` | one account per conversation: placement, cache TTLs, asking before a move |
| `notify.py` | macOS notifications and question dialogs |
| `measure.py` | the per-turn usage log and the `measure` fit |
| `proxy.py` | the local HTTP proxy |
| `priming.py` | the scheduled and automatic pings |
| `service.py` | LaunchAgents and the menu bar app build |
| `client_settings.py` | JSONC-preserving edits of the Claude Code and VS Code settings |
| `addons.py` | loading optional add-ons and the hook they implement |
| `cli.py` | the `claude-rotator` commands |

## Tested vs. unverified

Tested against a fake server (the proxy items are covered by `uv run pytest`):
- importing a login and cleaning up afterwards
- refreshing a token and saving the rotated refresh token
- switching accounts per request, and 429 failover
- passing other requests through untouched, and pinning a session to one account
- detecting a dead login, and keeping secrets out of logs

Tested against your real Keychain: writing, updating, reading and deleting an item.

Verified against Anthropic (2026-10-25, Claude Code 2.1.308):
- `/api/oauth/usage` works with the imported credentials for both accounts.
- Claude Code sends `/v1/messages` to `ANTHROPIC_BASE_URL`; the proxy swaps the token and the reply comes back.
- Rate-limit headers are `anthropic-ratelimit-unified-{5h,7d}-utilization` (a 0–1 fraction, two decimals) and `-{5h,7d}-reset` (unix seconds), as the parser assumes.
- Claude Code's `metadata.user_id` is JSON with a `session_id` that stays the same across turns; a session through the proxy keeps its cache (cache reads continue at the full context size after pinning).
- Claude Science conversations keep the same root `session_id` across turns, side tasks and frames, and stay fully cached through the proxy.

Not yet seen in practice: an actual token refresh against `platform.claude.com`. The first one happens when an access token nears expiry; check `serve.log` for "refreshed access token".

## Terms of service

Both subscriptions must genuinely be yours. Anthropic has said it acts against limit circumvention and account sharing. Automatic failover between accounts is in that grey zone and could get the accounts flagged, so use it at your own risk.
