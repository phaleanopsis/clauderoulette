"""claude-rotator: run several Claude subscription accounts side by side.

How credentials work: `add` runs Claude Code once in a throwaway config dir so you can
/login as the account. The resulting OAuth credential (access + refresh token) is moved
into this tool's own Keychain item and the throwaway copy is deleted, so this tool is
the only holder of that refresh chain and never races Claude Code's own login.
Claude Code itself stays logged in as usual and keeps all data in ~/.claude; the proxy
only replaces the bearer token on /v1/messages requests.

Stdlib only. Secrets never go on a command line or into logs.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("claude-rotator")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
