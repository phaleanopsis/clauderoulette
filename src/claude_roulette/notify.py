"""macOS question dialogs, shown by the proxy while a request waits for the answer."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
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


class Relogin:
    """Tells the user that an account's login has died and how to add it back: on the first
    request after it dies, then at most every `relogin_remind_s` while it stays dead."""

    def __init__(self, ask=ask):
        self.ask = ask
        self.lock = threading.Lock()
        self.asked_at: dict[str, float] = {}
        self.showing: set[str] = set()

    def remind(self, dead: dict[str, str], cfg: dict) -> None:
        now = time.time()
        with self.lock:
            for name in set(self.asked_at) - set(dead):  # revived: remind straight away if it dies again
                del self.asked_at[name]
            due = [n for n in dead if n not in self.showing
                   and now - self.asked_at.get(n, -1e18) >= cfg["relogin_remind_s"]]
            for name in due:
                self.asked_at[name] = now
                self.showing.add(name)
        for name in due:
            self._ask(name, dead[name], cfg)

    def _ask(self, name: str, reason: str, cfg: dict) -> None:
        email = cfg["emails"].get(name)
        who = f"“{name}” ({email})" if email else f"“{name}”"
        command = f"claude-roulette add {name}"
        others = [n for n in cfg["accounts"] if n != name]
        meanwhile = ("Requests go to your other accounts meanwhile." if others
                     else "Requests can't be served until it's back.")
        message = (f"The login for {who} has expired. {meanwhile}\n\n{reason}\n\n"
                   f"To add it back, run this in Terminal and sign in as {email or name}:\n\n"
                   f"    {command}\n\nThe proxy picks it up within a few minutes; no restart needed.")

        def answered(button: str | None) -> None:
            with self.lock:
                self.showing.discard(name)
            if button == "Copy command":
                try:
                    subprocess.run(["pbcopy"], input=command, text=True, timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

        self.ask("Claude Roulette", message, ["Copy command", "Later"], answered, timeout=600)
