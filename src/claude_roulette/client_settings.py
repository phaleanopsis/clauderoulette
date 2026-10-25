"""Point Claude Code (CLI and VS Code extension) at the proxy by editing their settings files.

The files are JSONC (VS Code allows comments and trailing commas), so they're edited as
text: only the value of one top-level key is replaced, and everything else, including
comments and formatting, is kept byte for byte. The first edit leaves a `.bak-roulette`
copy of the original next to the file.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .config import open_private, proxy_key
from .proxy import KEY_PREFIX

PROXY_VAR = "ANTHROPIC_BASE_URL"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
VSCODE_KEY = "claudeCode.environmentVariables"
_EDITOR_APPS = ("Code", "Code - Insiders", "Cursor", "VSCodium")


def vscode_settings_files() -> list[Path]:
    """User settings of every installed VS Code flavour."""
    base = Path.home() / "Library" / "Application Support"
    return [base / app / "User" / "settings.json" for app in _EDITOR_APPS if (base / app / "User").is_dir()]


def proxy_base(cfg: dict) -> str:
    return f"http://127.0.0.1:{cfg['proxy_port']}"


def proxy_url(cfg: dict) -> str:
    """The base URL clients use: it carries the proxy key, which the proxy requires."""
    return f"{proxy_base(cfg)}{KEY_PREFIX}{proxy_key()}"


def _ours(value, base: str) -> bool:
    """Whether a base URL points at this proxy, with any key or none (as set by older versions)."""
    return isinstance(value, str) and (value == base or value.startswith(base + "/"))


# --------------------------------------------------------------------------- value transforms

def with_claude_env(env, url: str):
    return {**(env if isinstance(env, dict) else {}), PROXY_VAR: url}


def without_claude_env(env, base: str):
    if not isinstance(env, dict) or not _ours(env.get(PROXY_VAR), base):
        return env  # not ours (e.g. set to something else by hand): leave it
    rest = {k: v for k, v in env.items() if k != PROXY_VAR}
    return rest or None


def with_vscode_env(items, url: str):
    items = list(items) if isinstance(items, list) else []
    entry = {"name": PROXY_VAR, "value": url}
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get("name") == PROXY_VAR:
            items[i] = entry
            return items
    return items + [entry]


def without_vscode_env(items, base: str):
    if not isinstance(items, list):
        return items
    rest = [i for i in items
            if not (isinstance(i, dict) and i.get("name") == PROXY_VAR and _ours(i.get("value"), base))]
    return rest or None


# --------------------------------------------------------------------------- JSONC text editing

def _blank(text: str) -> str:
    """text with comments and trailing commas turned into spaces; offsets stay the same."""
    out = list(text)
    i, n, in_str = 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    clean = "".join(out)
    # Second pass: trailing commas (only outside strings, now that comments are gone).
    i, in_str = 0, False
    while i < n:
        c = clean[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < n and clean[j].isspace():
                j += 1
            if j < n and clean[j] in "]}":
                out[i] = " "
        i += 1
    return "".join(out)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i].isspace():
        i += 1
    return i


def _members(clean: str) -> tuple[int, int, dict[str, tuple[int, int, int]]]:
    """(index of the top-level '{', index of its '}', {key: (key_start, value_start, value_end)})."""
    dec = json.JSONDecoder()
    open_at = i = _skip_ws(clean, 0)
    if clean[i:i + 1] != "{":
        raise ValueError("top level is not an object")
    members: dict[str, tuple[int, int, int]] = {}
    i = _skip_ws(clean, i + 1)
    while clean[i:i + 1] != "}":
        key_start = i
        key, i = dec.raw_decode(clean, i)
        i = _skip_ws(clean, i)
        if not isinstance(key, str) or clean[i:i + 1] != ":":
            raise ValueError(f"malformed object near offset {key_start}")
        value_start = _skip_ws(clean, i + 1)
        _, value_end = dec.raw_decode(clean, value_start)
        members[key] = (key_start, value_start, value_end)
        i = _skip_ws(clean, value_end)
        if clean[i:i + 1] == ",":
            i = _skip_ws(clean, i + 1)
        elif clean[i:i + 1] != "}":
            raise ValueError(f"expected ',' or '}}' at offset {i}")
    if clean[_skip_ws(clean, i + 1):].strip():
        raise ValueError("trailing content after the top-level object")
    return open_at, i, members


def _line_indent(text: str, i: int) -> str:
    start = text.rfind("\n", 0, i) + 1
    return text[start:i] if not text[start:i].strip() else ""


def _dump(value, indent: str) -> str:
    """Pretty JSON; a list of small objects keeps one object per line, as VS Code users write it."""
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        inner = f",\n{indent}  ".join(json.dumps(v) for v in value)
        return f"[\n{indent}  {inner}\n{indent}]"
    return json.dumps(value, indent=2).replace("\n", "\n" + indent)


def edit_top_level(text: str, key: str, fn: Callable) -> str:
    """Set top-level `key` to fn(current value or None); fn returning None removes the key."""
    if not text.strip():
        text = "{}\n"
    clean = _blank(text)
    open_at, close_at, members = _members(clean)
    dec = json.JSONDecoder()
    if key in members:
        key_start, value_start, value_end = members[key]
        old = dec.raw_decode(clean, value_start)[0]
        new = fn(old)
        if new == old:
            return text  # don't reformat a value that's already right
        if new is not None:
            return text[:value_start] + _dump(new, _line_indent(text, key_start)) + text[value_end:]
        # Remove the member together with one separating comma.
        after = _skip_ws(clean, value_end)
        if clean[after:after + 1] == ",":  # not the last member: the next one moves up
            return text[:key_start] + text[_skip_ws(clean, after + 1):]
        before = clean[:key_start].rstrip()
        if before.endswith(","):  # the last member: drop the comma in front of it
            return text[:len(before) - 1] + text[value_end:]
        return text[:open_at + 1] + "\n" + text[close_at:]  # the only member
    new = fn(None)
    if new is None:
        return text
    if members:
        first_key = min(m[0] for m in members.values())
        indent = _line_indent(text, first_key) or "  "
        last_end = max(m[2] for m in members.values())
        return text[:last_end] + f",\n{indent}{json.dumps(key)}: {_dump(new, indent)}" + text[last_end:]
    return text[:open_at + 1] + f"\n  {json.dumps(key)}: {_dump(new, '  ')}\n" + text[close_at:]


def update_file(path: Path, key: str, fn: Callable) -> str:
    """Apply edit_top_level to a settings file; returns what happened, for the install log."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        text = ""
    try:
        new = edit_top_level(text, key, fn)
    except ValueError as e:
        return f"skipped, could not parse it ({e})"
    if new == text:
        return "already up to date"
    if text:
        backup = path.with_name(path.name + ".bak-roulette")
        if not backup.exists():  # keep the pre-roulette original across reinstalls
            backup.write_text(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-roulette")
    tmp.unlink(missing_ok=True)
    with open(tmp, "w", opener=open_private) as f:  # owner-only: the proxy URL carries its key
        f.write(new)
    tmp.replace(path)
    return "updated" if text else "created"


def connect_clients(cfg: dict, claude: bool = True, vscode: bool = True) -> list[str]:
    url = proxy_url(cfg)
    shown = f"{proxy_base(cfg)}{KEY_PREFIX}…"  # the report may get pasted somewhere: leave the key out
    report = []
    if claude:
        status = update_file(CLAUDE_SETTINGS, "env", lambda v: with_claude_env(v, url))
        report.append(f"{CLAUDE_SETTINGS}: env.{PROXY_VAR} = {shown} ({status})")
    if vscode:
        for path in vscode_settings_files():
            status = update_file(path, VSCODE_KEY, lambda v: with_vscode_env(v, url))
            report.append(f"{path}: {VSCODE_KEY} {PROXY_VAR} = {shown} ({status})")
    return report


def disconnect_clients(cfg: dict) -> list[str]:
    base = proxy_base(cfg)
    report = []
    if CLAUDE_SETTINGS.exists():
        status = update_file(CLAUDE_SETTINGS, "env", lambda v: without_claude_env(v, base))
        report.append(f"{CLAUDE_SETTINGS}: removed env.{PROXY_VAR} ({status})")
    for path in vscode_settings_files():
        if path.exists():
            status = update_file(path, VSCODE_KEY, lambda v: without_vscode_env(v, base))
            report.append(f"{path}: removed {PROXY_VAR} from {VSCODE_KEY} ({status})")
    return report
