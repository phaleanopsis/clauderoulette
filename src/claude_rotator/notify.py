"""macOS question dialogs, shown by the proxy while a request waits for the answer."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable

from .config import log

ASK_TIMEOUT_S = 120


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask(title: str, message: str, buttons: list[str], answered: Callable[[str | None], None],
        timeout: float = ASK_TIMEOUT_S) -> None:
    """Show a dialog in the background; `answered` gets the button, or None on timeout/error."""
    log(f"ask: {message.splitlines()[0]} [{' / '.join(buttons)}]")
    if sys.platform != "darwin":
        answered(None)
        return
    script = (f"display dialog {_quote(message)} with title {_quote(title)} "
              f"buttons {{{', '.join(_quote(b) for b in buttons)}}} default button {_quote(buttons[0])} "
              f"giving up after {int(timeout)}")

    def run():
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                                 timeout=timeout + 30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        button = None
        if "gave up:true" not in out and "button returned:" in out:
            button = out.split("button returned:", 1)[1].split(",", 1)[0].strip()
        answered(button)

    threading.Thread(target=run, daemon=True).start()
