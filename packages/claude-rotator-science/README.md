# claude-rotator-science

Optional add-on for [claude-rotator](../../README.md). It recognises Claude Science's requests, so the core keeps each Science conversation on one account.

> [!WARNING]
> Experimental, LLM-generated, and not reviewed by a human.

The base `claude-rotator` install doesn't include this package. Install it as an extra:

```bash
uv tool install --editable '.[science]'      # from the repository root
```

Once it's installed, `serve` loads it automatically through the `claude_rotator.addons` entry point, and the startup line in `serve.log` lists `add-ons ['science']`. To remove it again, run `uv tool install --editable .` without the extra, then `claude-rotator install` to restart the service.

## What it does

Claude Science requests are recognised by `X-Anthropic-Surface: operon`. Science puts the conversation id into `metadata.user_id`, as `session_id = <root>[:<frame>][:<lane>]`.

- **Pinning (`science_mode: "pin"`, the default).** Every request of a conversation (main agent, frames and side tasks such as the title generator) is handed to the core as one conversation. Placement, moving when the cache is cold, asking before a move and the large-context hint are the same as for Claude Code; see [One account per conversation](../../README.md#one-account-per-conversation).
- **Observe (`science_mode: "observe"`).** Science requests pass through on Science's own login, untouched; `serve.log` still shows them.

`claude-rotator status` lists Science conversations together with Claude Code sessions. Pins from version 0.1 (`science-pins.json`) are taken over into `conversation-pins.json` on first start.

An explicit `x-rotator-account` header (`claude-rotator run --account`) still wins over a pin.

## Settings (`~/.claude-rotator/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `science_mode` | `"pin"` | `"pin"` or `"observe"` |

The load settings (`pin_active_s`, `pin_load_scale`, …) are shared with Claude Code and live in the core.
