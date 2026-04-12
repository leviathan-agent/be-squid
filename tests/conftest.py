"""Test configuration — sets env vars and imports the agent module.

ln-agent.py can't be imported normally due to the hyphen in the filename.
We use importlib.util to load it as a module named 'agent'.
Required env vars are set before import since the module reads them at load time.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Set required env vars BEFORE importing the agent module.
# These are required at import time (module crashes without them).
os.environ.setdefault("BOT_HQ_GROUP_ID", "-1001234567890")
os.environ.setdefault("CHANNELS", '["@test_channel"]')
# Prevent wallet key file read from failing during tests
os.environ.setdefault("WALLET_KEY_FILE", "/dev/null")

# Resolve path to ln-agent.py relative to this conftest
AGENT_PATH = Path(__file__).parent.parent / "ln-agent.py"


@pytest.fixture(scope="session")
def agent():
    """Import ln-agent.py as a module named 'agent' for the test session."""
    spec = importlib.util.spec_from_file_location("agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_db(agent, tmp_path):
    """Create a temporary AgentDB instance with a fresh SQLite database."""
    db_path = tmp_path / "test_agent.db"
    db = agent.AgentDB(db_path)
    yield db
    db.close()
