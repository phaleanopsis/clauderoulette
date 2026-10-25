import json

import pytest

from claude_roulette import client_settings as cs
from claude_roulette.client_settings import edit_top_level

URL = "http://127.0.0.1:8789"

VSCODE = """{
  "settingsSync.ignoredSettings": [
    "r.rterm.mac"
  ],
    //R Options
    "r.bracketedPaste": true, /* block, with "quotes" and a // inside */
    "url": "https://example.com//not-a-comment",
    "list": [1, 2,],
}
"""


def parse(text):
    return json.loads(cs._blank(text))


def test_blank_keeps_offsets_and_strings():
    clean = cs._blank(VSCODE)
    assert len(clean) == len(VSCODE)
    assert "//not-a-comment" in clean
    assert "R Options" not in clean and "block" not in clean


def test_adds_vscode_key_and_keeps_comments():
    out = edit_top_level(VSCODE, cs.VSCODE_KEY, lambda v: cs.with_vscode_env(v, URL))
    assert "//R Options" in out and "/* block" in out
    assert parse(out)[cs.VSCODE_KEY] == [{"name": "ANTHROPIC_BASE_URL", "value": URL}]
    assert out.startswith(VSCODE[:VSCODE.index('    "list"')])  # nothing before the insertion changed


def test_updates_existing_entry_and_keeps_others():
    text = '{\n  "claudeCode.environmentVariables": [\n    {"name": "FOO", "value": "1"},\n' \
           '    {"name": "ANTHROPIC_BASE_URL", "value": "http://old"}\n  ]\n}\n'
    out = edit_top_level(text, cs.VSCODE_KEY, lambda v: cs.with_vscode_env(v, URL))
    assert parse(out)[cs.VSCODE_KEY] == [{"name": "FOO", "value": "1"},
                                         {"name": "ANTHROPIC_BASE_URL", "value": URL}]


def test_idempotent():
    once = edit_top_level(VSCODE, cs.VSCODE_KEY, lambda v: cs.with_vscode_env(v, URL))
    assert edit_top_level(once, cs.VSCODE_KEY, lambda v: cs.with_vscode_env(v, URL)) == once


@pytest.mark.parametrize("text", [VSCODE, "{}", "", '{"a": 1}', '{\n  "a": 1, // c\n  "b": 2\n}'])
def test_add_then_remove_round_trips(text):
    added = edit_top_level(text, cs.VSCODE_KEY, lambda v: cs.with_vscode_env(v, URL))
    removed = edit_top_level(added, cs.VSCODE_KEY, lambda v: cs.without_vscode_env(v, URL))
    assert parse(removed) == (parse(text) if text else {})
    if text:
        assert removed.replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


def test_remove_middle_member():
    text = '{\n  "a": 1,\n  "env": {"ANTHROPIC_BASE_URL": "' + URL + '"},\n  "b": 2\n}\n'
    out = edit_top_level(text, "env", lambda v: cs.without_claude_env(v, URL))
    assert out == '{\n  "a": 1,\n  "b": 2\n}\n'


def test_claude_env_merges_and_only_removes_our_value():
    text = '{\n  "env": {\n    "FOO": "1"\n  },\n  "model": "opus"\n}\n'
    out = edit_top_level(text, "env", lambda v: cs.with_claude_env(v, URL))
    assert parse(out)["env"] == {"FOO": "1", "ANTHROPIC_BASE_URL": URL}
    assert parse(out)["model"] == "opus"
    back = edit_top_level(out, "env", lambda v: cs.without_claude_env(v, URL))
    assert parse(back)["env"] == {"FOO": "1"}
    other = '{"env": {"ANTHROPIC_BASE_URL": "https://gateway.example"}}'
    assert edit_top_level(other, "env", lambda v: cs.without_claude_env(v, URL)) == other


def test_unparseable_file_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]")
    assert cs.update_file(path, "env", lambda v: cs.with_claude_env(v, URL)).startswith("skipped")
    assert path.read_text() == "[1, 2]"


def test_update_file_backs_up_original_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"model": "opus"}')
    assert cs.update_file(path, "env", lambda v: cs.with_claude_env(v, URL)) == "updated"
    assert cs.update_file(path, "env", lambda v: cs.with_claude_env(v, URL)) == "already up to date"
    assert cs.update_file(path, "env", lambda v: cs.with_claude_env(v, "http://127.0.0.1:1")) == "updated"
    assert (tmp_path / "settings.json.bak-roulette").read_text() == '{"model": "opus"}'
    assert cs.update_file(tmp_path / "new" / "settings.json", "env",
                          lambda v: cs.with_claude_env(v, URL)) == "created"


def test_uninstall_removes_our_url_with_or_without_a_key():
    for value in (URL, URL + "/k/abc"):
        text = json.dumps({"env": {"ANTHROPIC_BASE_URL": value, "FOO": "1"}})
        assert parse(edit_top_level(text, "env", lambda v: cs.without_claude_env(v, URL)))["env"] == {"FOO": "1"}
        items = [{"name": "ANTHROPIC_BASE_URL", "value": value}]
        assert cs.without_vscode_env(items, URL) is None
    other = json.dumps({"env": {"ANTHROPIC_BASE_URL": URL + "0"}})  # port 87890: not ours
    assert edit_top_level(other, "env", lambda v: cs.without_claude_env(v, URL)) == other
