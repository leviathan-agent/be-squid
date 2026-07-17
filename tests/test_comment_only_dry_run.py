"""Tests for COMMENT_ONLY mode, DRY_RUN mode, MAX_COMMENTS_PER_CYCLE, and the
CYCLE_INTERVAL env wiring.

Several of the flags under test (COMMENT_ONLY, CHANNELS-required-ness,
MAX_COMMENTS_PER_CYCLE, CYCLE_INTERVAL) are module-level constants resolved once
at import time. The session-scoped `agent` fixture in conftest.py only reflects
whatever env it first imported with, so tests that need a *different* value use
`_reload_agent()` below to import a fresh copy of ln-agent.py under a custom
environment — the same technique conftest.py itself uses.
"""

import asyncio
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

AGENT_PATH = Path(__file__).parent.parent / "ln-agent.py"

# Syntactically valid but otherwise meaningless secp256k1 scalar — fine for
# Account.from_key() since nothing here ever hits the real LN API.
DUMMY_PRIVATE_KEY = "0x" + "11" * 32


def _reload_agent(set_env: dict | None = None, unset_env: list | None = None):
    """Import a fresh copy of ln-agent.py under a custom environment, restoring
    the previous environment afterward regardless of success or failure."""
    set_env = set_env or {}
    unset_env = unset_env or []
    touched = set(set_env) | set(unset_env)
    saved = {k: os.environ.get(k) for k in touched}
    try:
        for k in unset_env:
            os.environ.pop(k, None)
        os.environ.update(set_env)
        spec = importlib.util.spec_from_file_location("agent_reloaded", AGENT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ─── _env_flag helper ────────────────────────────────────────────────────────

def test_env_flag_truthy_values(agent, monkeypatch):
    for v in ("1", "true", "True", "YES", "on", " 1 "):
        monkeypatch.setenv("_TEST_FLAG", v)
        assert agent._env_flag("_TEST_FLAG") is True


def test_env_flag_falsy_and_default(agent, monkeypatch):
    monkeypatch.setenv("_TEST_FLAG", "0")
    assert agent._env_flag("_TEST_FLAG") is False
    monkeypatch.delenv("_TEST_FLAG", raising=False)
    assert agent._env_flag("_TEST_FLAG") is False
    assert agent._env_flag("_TEST_FLAG", default=True) is True


# ─── COMMENT_ONLY startup gating ─────────────────────────────────────────────

def test_comment_only_does_not_require_channels_or_bot_hq():
    """CHANNELS/BOT_HQ_GROUP_ID must not be required at import time when
    COMMENT_ONLY=1 — this is the startup path the task explicitly calls out."""
    mod = _reload_agent(
        set_env={"COMMENT_ONLY": "1", "WALLET_KEY_FILE": "/dev/null"},
        unset_env=["CHANNELS", "BOT_HQ_GROUP_ID"],
    )
    assert mod.COMMENT_ONLY is True
    assert mod.CHANNELS == []
    assert mod.BOT_HQ is None


def test_channels_still_required_when_not_comment_only():
    """Preserves original behavior: CHANNELS is required unless COMMENT_ONLY=1."""
    with pytest.raises(SystemExit):
        _reload_agent(
            set_env={"WALLET_KEY_FILE": "/dev/null"},
            unset_env=["CHANNELS", "COMMENT_ONLY"],
        )


# ─── MAX_COMMENTS_PER_CYCLE / CYCLE_INTERVAL env wiring ──────────────────────

def test_max_comments_per_cycle_default_and_override():
    mod_default = _reload_agent(unset_env=["MAX_COMMENTS_PER_CYCLE"])
    assert mod_default.MAX_COMMENTS_PER_CYCLE == 5
    mod_override = _reload_agent(set_env={"MAX_COMMENTS_PER_CYCLE": "2"})
    assert mod_override.MAX_COMMENTS_PER_CYCLE == 2


def test_cycle_interval_default_and_override():
    mod_default = _reload_agent(unset_env=["CYCLE_INTERVAL"])
    assert mod_default.CYCLE_INTERVAL == 60 * 60
    mod_override = _reload_agent(set_env={"CYCLE_INTERVAL": "120"})
    assert mod_override.CYCLE_INTERVAL == 120


# ─── load_credentials(require_telegram=False) ────────────────────────────────

def test_load_credentials_skips_telegram_check_when_not_required(agent, monkeypatch):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", DUMMY_PRIVATE_KEY)
    api_id, api_hash, wallet_key = agent.load_credentials(require_telegram=False)
    assert api_id is None
    assert api_hash is None
    assert wallet_key == DUMMY_PRIVATE_KEY


# ─── DRY_RUN — LNClient write methods ────────────────────────────────────────

def _make_client(agent):
    return agent.LNClient(DUMMY_PRIVATE_KEY)


def test_lnclient_vote_dry_run_no_network_and_logs(agent, monkeypatch, tmp_path):
    log_path = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "DRY_RUN_LOG", log_path)
    client = _make_client(agent)
    client.session = MagicMock()

    client.vote(42, weight=1, label="article")

    client.session.post.assert_not_called()
    client.session.get.assert_not_called()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["action"] == "vote"
    assert entry["args"] == {"item_id": 42, "weight": 1, "label": "article"}
    assert "ts" in entry


def test_lnclient_post_yap_dry_run_no_network_and_logs(agent, monkeypatch, tmp_path):
    log_path = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "DRY_RUN_LOG", log_path)
    client = _make_client(agent)
    client.session = MagicMock()

    client.post_yap(7, "Great analysis of the market.", tags=["analysis"])

    client.session.post.assert_not_called()
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "post_yap"
    assert entry["args"]["content_id"] == 7
    assert entry["would_post_text"] == "Great analysis of the market."


def test_lnclient_submit_article_dry_run_fake_success(agent, monkeypatch, tmp_path):
    log_path = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "DRY_RUN_LOG", log_path)
    client = _make_client(agent)
    client.session = MagicMock()

    result = client.submit_article("https://example.com/story", "A headline")

    client.session.post.assert_not_called()
    # Callers do `result.get("article_id")` and treat a truthy value as success —
    # fake it minimally, marking the id as "dry-run".
    assert result["article_id"] == "dry-run"
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "submit_article"
    assert entry["would_post_text"] == "A headline"


def test_lnclient_live_mode_still_hits_network(agent, monkeypatch):
    """Sanity check the inverse: DRY_RUN=False takes the real network path."""
    monkeypatch.setattr(agent, "DRY_RUN", False)
    client = _make_client(agent)
    client._auth_time = time.time()  # skip re-auth branch inside _refresh_if_stale
    client.session = MagicMock()
    client.session.post.return_value = MagicMock(ok=True)

    client.vote(1, weight=1)

    client.session.post.assert_called_once()


# ─── MAX_COMMENTS_PER_CYCLE cap + COMMENT_ONLY + DRY_RUN, end to end ─────────

def test_run_agent_comment_only_dry_run_respects_max_comments_cap(agent, monkeypatch, tmp_path):
    """Full run_agent() cycle with COMMENT_ONLY + DRY_RUN, network/LLM mocked out.

    Verifies together:
      - Phase 1 never runs: TelegramClient is never constructed.
      - Phase 4 caps new-article comments at MAX_COMMENTS_PER_CYCLE (excess
        articles are left uncommented, not marked, for a future cycle).
      - DRY_RUN means none of the "commented" articles are recorded in agent.db,
        so a later live run could still post the real comments.
    """
    monkeypatch.setattr(agent, "COMMENT_ONLY", True)
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "MAX_COMMENTS_PER_CYCLE", 3)
    monkeypatch.setenv("WALLET_PRIVATE_KEY", DUMMY_PRIVATE_KEY)

    dry_run_log = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN_LOG", dry_run_log)

    db_path = tmp_path / "test_agent.db"
    RealAgentDB = agent.AgentDB
    monkeypatch.setattr(agent, "AgentDB", lambda: RealAgentDB(db_path))

    class PoisonTelegramClient:
        def __init__(self, *a, **kw):
            raise AssertionError("TelegramClient must not be constructed in COMMENT_ONLY mode")

    monkeypatch.setattr(agent, "TelegramClient", PoisonTelegramClient)

    def fake_authenticate(self):
        self._auth_time = time.time()
        self.user_id = 999

    monkeypatch.setattr(agent.LNClient, "authenticate", fake_authenticate)
    monkeypatch.setattr(agent.LNClient, "has_our_comment", lambda self, article_id: False)
    monkeypatch.setattr(agent.LNClient, "get_yaps", lambda self, article_id: [])

    # Neutralize the operator-alerting startup check: unrelated to what this
    # test exercises, and would otherwise shell out to the real CLAUDE_BIN and
    # write to the real agent.db (via alerts.py's default db path) every run.
    monkeypatch.setattr(agent, "check_provider_startup_viable", lambda: None)

    now_iso = datetime.now(timezone.utc).isoformat()
    fake_articles = [
        {"id": i, "headline": f"Headline {i}", "content_type": "news", "tags": [],
         "created_at": now_iso, "author": {}}
        for i in range(1, 9)  # 8 candidates, cap of 3
    ]
    monkeypatch.setattr(
        agent.LNClient, "get_recent_articles",
        lambda self, per_page=20, status="approved": fake_articles,
    )

    # Neutralize LLM calls not under test here.
    monkeypatch.setattr(agent, "batch_evaluate_articles", lambda articles: {})
    monkeypatch.setattr(agent, "evaluate_article_quality", lambda headline, tags: 0)
    # Gate routing is exercised separately in test_comment_gate.py — here every
    # candidate routes to the standard analysis register so the cap-and-dry-run
    # behavior under test is unaffected by gate decisions.
    monkeypatch.setattr(agent, "gate_comment", lambda headline, tags: "SUBSTANCE")
    monkeypatch.setattr(
        agent, "craft_comment",
        lambda headline, tags, article_url="", **_kw: "A sufficiently long dry-run analysis comment.",
    )

    # The Phase 4/5 loops rate-limit themselves with real asyncio.sleep() calls —
    # collapse those so the test runs in well under a second.
    async def _instant_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    asyncio.run(agent.run_agent())

    lines = [json.loads(l) for l in dry_run_log.read_text().strip().splitlines()]
    post_yap_entries = [l for l in lines if l["action"] == "post_yap"]
    assert len(post_yap_entries) == 3  # capped, not all 8
    assert not any(l["action"] in ("vote", "submit_article") for l in lines)

    check_db = RealAgentDB(db_path)
    try:
        # DRY_RUN must not record any of these as commented — a later live run
        # should still be free to post the real comments.
        assert not any(check_db.was_commented(a["id"]) for a in fake_articles)
        row = check_db._execute(
            "SELECT articles_commented FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["articles_commented"] == 3
    finally:
        check_db.close()
