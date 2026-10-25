import os
import tempfile

# Must be set before claude_rotator is imported: paths and the secret store are fixed at import.
os.environ["CLAUDE_ROTATOR_HOME"] = tempfile.mkdtemp(prefix="claude-rotator-test-")
os.environ["CLAUDE_ROTATOR_NO_KEYCHAIN"] = "1"
