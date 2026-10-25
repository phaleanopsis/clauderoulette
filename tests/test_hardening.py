import stat

import pytest

from claude_roulette import cli, config, credentials, measure


@pytest.mark.parametrize("name", ["../victim", "a/b", "..", "", "x" * 33, 'a"b', "a\nb", "-x", "a b"])
def test_bad_account_names_are_rejected_before_touching_disk(name, monkeypatch):
    monkeypatch.setattr(cli.shutil, "rmtree", lambda *a, **k: pytest.fail("rmtree reached"))
    with pytest.raises(SystemExit):
        cli.main(["add", name])


@pytest.mark.parametrize("name", ["work", "main", "Work_2", "a-b"])
def test_good_account_names(name):
    assert config.check_account_name(name) == name


def test_keychain_write_refuses_injection():
    with pytest.raises(ValueError):
        credentials.kc_write("claude-roulette", 'x" -s "other', "secret")
    with pytest.raises(ValueError):
        credentials.kc_write("claude-roulette", "x\ndelete-keychain", "secret")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_runtime_files_are_private():
    config.HOME.chmod(0o755)  # as left by older versions
    config.save_json(config.TOKENS_FILE, {"a": "secret"})
    measure.record("a", "claude-haiku", {"input": 1}, {})
    with config.file_lock():
        pass
    assert mode(config.HOME) == 0o700
    for f in (config.TOKENS_FILE, measure.LOG_FILE, config.LOCK_FILE):
        assert mode(f) == 0o600, f


def test_menu_bar_app_gets_its_command_from_its_own_bundle():
    from claude_roulette import service

    info = service.app_info_plist()
    assert info[service.COMMAND_KEY] == service.cli_command()
    swift = (service.files("claude_roulette") / "menubar" / "ClaudeRouletteBar.swift").read_text()
    assert "state?.command" not in swift and service.COMMAND_KEY in swift


def test_run_refuses_unknown_account(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: {**config.DEFAULTS, "accounts": ["work"]})
    with pytest.raises(SystemExit, match="unknown account"):
        cli.main(["run", "--account", "nobody"])


def test_proxy_key_is_private_and_stable():
    from claude_roulette import client_settings

    key = config.proxy_key()
    assert len(key) >= 32 and config.proxy_key() == key
    assert mode(config.KEY_FILE) == 0o600
    assert client_settings.proxy_url({"proxy_port": 8789}) == f"http://127.0.0.1:8789/k/{key}"


def test_settings_file_holding_the_key_is_owner_only(tmp_path):
    from claude_roulette import client_settings as cs

    path = tmp_path / "settings.json"
    path.write_text("{}")
    path.chmod(0o644)
    assert cs.update_file(path, "env", lambda v: cs.with_claude_env(v, "http://127.0.0.1:1/k/x")) == "updated"
    assert mode(path) == 0o600


def test_custom_home_never_reaches_the_keychain(monkeypatch):
    monkeypatch.delenv("CLAUDE_ROULETTE_NO_KEYCHAIN")
    monkeypatch.setattr(credentials.sys, "platform", "darwin")
    monkeypatch.setattr(credentials.shutil, "which", lambda _: "/usr/bin/security")
    assert not credentials._has_keychain()


def test_usage_probe_ignores_an_upstream_setting(monkeypatch):
    from claude_roulette import usage

    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        raise usage.urllib.error.URLError("stop")

    monkeypatch.setattr(usage, "access_token", lambda name: "t")
    monkeypatch.setattr(usage.urllib.request, "urlopen", fake_urlopen)
    usage.probe("a", {**config.DEFAULTS, "upstream": "http://attacker.example"})
    assert seen == ["https://api.anthropic.com/api/oauth/usage"]


def test_run_refuses_a_port_held_by_something_else(monkeypatch):
    monkeypatch.setattr(cli, "proxy_status", lambda cfg: "other")
    with pytest.raises(SystemExit, match="something other than the claude-roulette proxy"):
        cli.main(["run"])
