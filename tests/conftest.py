import os
import tempfile

# Must be set before claude_roulette is imported: paths and the secret store are fixed at import.
os.environ["CLAUDE_ROULETTE_HOME"] = tempfile.mkdtemp(prefix="claude-roulette-test-")
os.environ["CLAUDE_ROULETTE_NO_KEYCHAIN"] = "1"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def relogin_asks():
    """Never put a real dialog on screen: record relogin reminders instead."""
    from claude_roulette import notify
    from claude_roulette.proxy import ProxyHandler
    asked = []
    ProxyHandler.relogin = notify.Relogin(ask=lambda title, message, buttons, answered, timeout: asked.append(
        (message, answered)))
    return asked
