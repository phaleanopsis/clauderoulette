import os
import tempfile

# Must be set before claude_roulette is imported: paths and the secret store are fixed at import.
os.environ["CLAUDE_ROULETTE_HOME"] = tempfile.mkdtemp(prefix="claude-roulette-test-")
os.environ["CLAUDE_ROULETTE_NO_KEYCHAIN"] = "1"
