# claude-roulette

Use two (or more) Claude subscriptions in the same setup to get the most out of your token limits.

> [!WARNING]
> **Experimental, LLM-generated, and not reviewed by a human.**
>
> This code was written by an LLM (Claude). No human has read it line by line.
> It handles credentials; read it before trusting it with anything you would mind losing.

## How it works

```
 claude (terminal / VS Code)
   │  ANTHROPIC_BASE_URL=http://127.0.0.1:8789/k/<key>
   ▼
 local proxy ──► picks the account with more available capacity ──► api.anthropic.com
   ▲
   └── one login per account, stored in the macOS Keychain
```

A local proxy receives every message Claude Code sends and forwards it on the account that has the most usable capacity left. Each conversation stays on one account, so its prompt cache keeps working. When an account fills up, you get a dialog asking whether to move to the other one.

Logins are stored in the macOS Keychain, separate from your normal Claude Code login, which is never read or changed. Tokens are never logged or written to disk.

A menu bar app shows each account's 5-hour and weekly usage. Every morning at 07:00 the less-used account gets a tiny Haiku prompt so its 5-hour window is already running when you start work.

## Install

claude-roulette runs on macOS. Skip any of these steps you've already done:

```bash
xcode-select --install                  # Xcode command line tools (builds the menu bar app)
brew install uv                         # the Python tool manager; needs Homebrew: https://brew.sh
brew install --cask claude-code         # the Claude Code command line interface
```

Install claude-roulette:

```bash
uv tool install claude-roulette
```

Add your Claude accounts and start the proxy:

```bash
claude-roulette add work --primary      # a Claude window opens: /login as this account, then /exit
claude-roulette add personal            # same for the second account
claude-roulette install                 # starts the proxy and menu bar app, routes Claude Code through it
claude-roulette status                  # check that both accounts show up
```

That's it: every new `claude` session in the terminal or VS Code now goes through the proxy.

**Update:** `uv tool upgrade claude-roulette && claude-roulette install` (until you run `install`, the new version may refuse requests from settings written by an older one)
**Remove:** `claude-roulette uninstall` (removes the service, menu bar app and settings entries; your logins and config are kept), then `uv tool uninstall claude-roulette`. Keep that order: settings left pointing at the port send Claude Code's login and conversations to whatever program listens there next.

### What works

| Client | Routed? |
|---|---|
| `claude` in the terminal | ✅ |
| Claude Code in VS Code, Cursor, VSCodium | ✅ |
| Claude desktop app, Code tab | ❌ (ignores `ANTHROPIC_BASE_URL`) |
| claude.ai chat (web or desktop) | ❌ (still counted: the proxy sees its usage) |
| Claude Science | ✅ (`install` turns this on when Claude Science is installed) |

Shells opened from the Claude desktop app inherit their own `ANTHROPIC_BASE_URL`, so plain `claude` there goes direct. Use `claude-roulette run` instead.

## Configuration

Settings live in `~/.claude-roulette/config.json`. Only the keys you set are stored; everything else uses the defaults. Run `claude-roulette install` afterwards to restart the proxy.

The ones you're most likely to change:

| Key | Default | What it does |
|---|---|---|
| `addons` | `[]` | Optional add-ons. `install` adds `"science"` (routes [Claude Science](#details)) if Claude Science is installed and you haven't set this key; `[]` turns it off |
| `account_caps` | `{}` | Keep capacity free on an account, e.g. for the chat app: `{"personal": {"session": 80, "weekly": 90}}` |
| `primary_bias` | `1.2` | How strongly the `--primary` account is preferred |
| `schedule` | `["07:00"]` | Times to send the morning ping (`[]` turns it off) |
| `pin_warn_pct` | `85` | 5h usage at which you're asked whether to move a conversation |
| `pin_hint_tokens` | `150000` | Context size at which you're asked once whether to continue (`0` turns it off) |
| `code_mode` | `"pin"` | `"pin"`: one account per conversation; `"off"`: switch freely |

All keys and their defaults are in [`config.py`](src/claude_roulette/config.py).

### Commands

| Command | |
|---|---|
| `add NAME [--primary]` / `remove NAME` | add or remove an account |
| `status` | usage per account and recent conversations |
| `install [--no-menubar] [--no-claude-settings] [--no-vscode-settings]` / `uninstall` | background service, menu bar app, client settings |
| `run [--account NAME]` | start `claude` through the proxy, optionally forced onto one account |
| `prime [--account NAME]` | send the morning ping now |
| `serve --debug` | run the proxy in the foreground |
| `measure` | estimate how much each token type counts toward the limits |

## Details

<details>
<summary><b>How an account is chosen</b></summary>

Every account gets a score:

- **Active 5h window:** unused % ÷ hours until reset, so capacity that's about to expire is used first.
- **No active window:** a flat 10.
- **Weekly pacing:** multiplied by how far the account is behind an even pace through its week (up to `weekly_factor_max`, 50×). An account with lots left just before its weekly reset is drained first.
- **Primary:** multiplied by `primary_bias`.
- **Excluded:** at ≥ 95 % of its 5h window, ≥ 98 % of its week, rate-limited, or with an expired login.

A 429 from Anthropic marks the account blocked and the request is retried once on the other.
</details>

<details>
<summary><b>Why each conversation stays on one account</b></summary>

Every request re-sends the whole conversation, so nothing is lost when it changes account, but the prompt cache is per account. The first turn after a move re-sends the context uncached, about 10× the cost of a cached turn. So the proxy decides per conversation ([`conversations.py`](src/claude_roulette/conversations.py)):

| Situation | Account |
|---|---|
| New conversation | best score, discounted by the conversations already running there |
| Subagent | its parent's |
| Cache warm | stays; if the account fills up, you're asked first |
| Cache cold (idle past its TTL) | free to move to the best account |

When it asks, your message is held until you answer (up to 2 minutes), and the answer decides where that message goes:

| Trigger | Choices |
|---|---|
| Conversation of 20k+ tokens, account past 85 % of its 5h window | Switch and send on B / Send on current / Cancel |
| Account full | Switch and send on B / Cancel |
| Context passes 150k tokens (once) | Send anyway / Cancel |

*Cancel* only stops this message; nothing reaches Anthropic. To start fresh, type `/clear` (or `/compact` to keep a summary). `claude-roulette status` lists recent conversations with their account, size and cache state.
</details>

<details>
<summary><b>Credentials</b></summary>

`add NAME` starts Claude Code with a temporary, empty config folder. You `/login` and `/exit`; the tool copies the new login into its own Keychain item (service `claude-roulette`) and deletes the temporary item and folder. Each account therefore has its own login session, separate from Claude Code's, and the two never fight over a refresh token.

- Secrets are passed to `security` on stdin, never on a command line where `ps` could see them.
- They're never logged or written to `state.json`.
- Tokens are refreshed before they expire and the new refresh token is saved back.
- An account whose login is rejected (`invalid_grant`) is skipped until you run `add` again.
- On Linux, logins fall back to a chmod-600 `~/.claude-roulette/tokens.json`.
- Tokens are only ever sent to `api.anthropic.com` and Anthropic's token endpoint; neither is a setting. With `CLAUDE_ROULETTE_HOME` set (meant for tests), the Keychain is not used at all.

**What the Keychain doesn't protect against:** the items are written by Apple's `security` tool, and macOS lets that tool read them back without asking. So any program running as you can read these logins, just as it can read Claude Code's own. Treat malware on your account as having all of them.

Each machine gets its own logins, so two machines never invalidate each other's.
</details>

<details>
<summary><b>What <code>install</code> changes</b></summary>

- **LaunchAgents** in `~/Library/LaunchAgents`: `com.user.claude-roulette` (the proxy, always restarted) and `com.user.claude-roulette.bar` (the menu bar app). Both start at login.
- **`~/.claude/settings.json`:** adds `"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8789/k/<key>"}` and makes the file readable by you only.
- **VS Code user settings** (and Insiders, Cursor, VSCodium if present): adds the same variable to `claudeCode.environmentVariables`.

Only that one key changes; comments and formatting are kept. The original is backed up as `settings.json.bak-roulette`. Files that can't be parsed are skipped and reported. `uninstall` removes the entries if they still point at this proxy.

`<key>` is a random secret in `~/.claude-roulette/proxy-key`. The proxy refuses any request without it, so other programs that can reach the port (containers via `host.docker.internal`, other users on the Mac, web pages) can't use your accounts. `run`, `status` and `install` also check that whatever answers on the port knows the key before trusting it.

**While the proxy is down, sessions that use these settings fail** with a connection error. Setting only `ANTHROPIC_BASE_URL` is a [documented setup](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways) that keeps your claude.ai login and subscription billing.

`install` records the path of the Python that ran it, so re-run `install` after reinstalling the tool.

After sleep, the proxy fetches fresh usage straight away. A 07:00 ping missed while asleep runs on wake, up to 09:00. To wake the Mac for it: `sudo pmset repeat wakeorpoweron MTWRFSU 06:58:00`.
</details>

<details>
<summary><b>Files</b></summary>

| Path | |
|---|---|
| `~/.claude-roulette/config.json` | your settings |
| `~/.claude-roulette/state.json` | usage per account (read by the menu bar app) |
| `~/.claude-roulette/conversation-pins.json` | which conversation is on which account (kept 30 days) |
| `~/.claude-roulette/usage-log.jsonl` | token counts per turn for `measure` (no content) |
| `~/.claude-roulette/serve.log` | proxy log |
</details>

<details>
<summary><b>Claude Science</b></summary>

Claude Science requests are recognised by `X-Anthropic-Surface: operon`, and the conversation id is read from `metadata.user_id` (`session_id = <root>[:<frame>][:<lane>]`). `install` adds `"addons": ["science"]` to `config.json` when it finds `Claude Science.app` and you haven't set `addons` yourself. With it on, every request of a conversation (main agent, frames, side tasks such as the title generator) is pinned to one account, exactly like a Claude Code session. `status` lists Science conversations with all others.

Set `"science_mode": "observe"` to let Science requests through on Science's own login instead; they still show up in `serve.log`. An explicit `claude-roulette run --account` still wins over a pin.

Add-ons live in the package as separate modules; [`addons.py`](src/claude_roulette/addons.py) documents the interface.
</details>

<details>
<summary><b>Development</b></summary>

```bash
uv tool install --editable .            # `claude-roulette` runs this checkout
uv sync           # .venv with the package, all add-ons and dev tools
uv run pytest     # never touches the real Keychain
uv run ruff check
```

launchd can't read files in `~/Documents`, so an editable install of a checkout there can't run the background service. Keep the checkout elsewhere, or use a non-editable install (`uv tool install --reinstall .`).

Tested against a fake upstream: login import and cleanup, token refresh, per-request switching, 429 failover, pass-through, pinning, dead-login detection, secrets kept out of logs.

Verified against Anthropic (2026-10-25, Claude Code 2.1.308): the usage endpoint, token swapping, the rate-limit headers, and that sessions keep their cache through the proxy. Not yet seen in practice: a real token refresh (look for "refreshed access token" in `serve.log`).
</details>

## Terms of service

Both subscriptions must genuinely be yours. Anthropic has said it acts against limit circumvention and account sharing. Automatic failover between accounts is in that grey zone and could get the accounts flagged. Use it at your own risk.
