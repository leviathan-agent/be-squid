"""Tests for the VOTING_ENABLED flag — the Phase 4 voting kill switch.

Context: the classification-tier LLM (Sonnet, pinned via CLAUDE_BIN) is
empirically unsafe as a vote judge — evaluate_comment_quality() returned -1
three times out of three on a genuinely insightful, well-sourced comment, and
batch_evaluate_comments() frequently false-positives its own task prompt as a
prompt-injection attempt and falls through to that same broken individual-eval
path. VOTING_ENABLED defaults to False so v0 runs as a pure COMMENTER and never
casts an unjustified downvote on a real contributor.

Covers:
  - _env_flag() default semantics: VOTING_ENABLED defaults to False, the
    inverse of COMMENT_ONLY/DRY_RUN's "False = full steam ahead" polarity —
    here False means voting stays OFF.
  - With voting disabled (the default): none of batch_evaluate_articles /
    evaluate_article_quality / batch_evaluate_comments / evaluate_comment_quality
    / ln.vote() are ever called (poisoned to prove it), for both a top-level
    "yap" content-type item AND a per-article yap under a news article, while
    gate_comment/craft_comment/commenting still proceed normally.
  - With VOTING_ENABLED=1: original vote behavior is intact — both article
    votes and yap votes are cast.

Follows the conventions in test_comment_gate.py (_prepare_phase4_harness-style
end-to-end Phase 4 harness) and test_comment_only_dry_run.py (_reload_agent
for constants resolved at import time).
"""

import asyncio
import importlib.util
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).parent.parent / "ln-agent.py"

# Syntactically valid but otherwise meaningless secp256k1 scalar — fine for
# Account.from_key() since nothing here ever hits the real LN API.
DUMMY_PRIVATE_KEY = "0x" + "11" * 32


def _reload_agent(set_env: dict | None = None, unset_env: list | None = None):
    """Import a fresh copy of ln-agent.py under a custom environment, restoring
    the previous environment afterward regardless of success or failure.
    Mirrors test_comment_only_dry_run.py's helper of the same name."""
    set_env = set_env or {}
    unset_env = unset_env or []
    touched = set(set_env) | set(unset_env)
    saved = {k: os.environ.get(k) for k in touched}
    try:
        for k in unset_env:
            os.environ.pop(k, None)
        os.environ.update(set_env)
        spec = importlib.util.spec_from_file_location("agent_reloaded_voting", AGENT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ─── VOTING_ENABLED default / import-time semantics ──────────────────────────

def test_voting_enabled_default_is_false(agent):
    """Session-scoped `agent` fixture imports with no VOTING_ENABLED env var
    set — must default to False (voting disabled), not True."""
    assert agent.VOTING_ENABLED is False


def test_voting_enabled_defaults_false_when_env_unset():
    mod = _reload_agent(unset_env=["VOTING_ENABLED"])
    assert mod.VOTING_ENABLED is False


def test_voting_enabled_explicit_zero_stays_false():
    mod = _reload_agent(set_env={"VOTING_ENABLED": "0"})
    assert mod.VOTING_ENABLED is False


def test_voting_enabled_can_be_turned_on_via_env():
    mod = _reload_agent(set_env={"VOTING_ENABLED": "1"})
    assert mod.VOTING_ENABLED is True


# ─── Phase 4 end-to-end harness ──────────────────────────────────────────────

def _make_articles(n: int, start_id: int = 1) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return [
        {"id": i, "headline": f"Headline {i}", "content_type": "news", "tags": [],
         "created_at": now_iso, "author": {}}
        for i in range(start_id, start_id + n)
    ]


def _make_yap_item(article_id: int) -> dict:
    """A top-level feed item of content_type == 'yap' — distinct from a
    per-article comment; this is the OTHER vote call site in Phase 4."""
    return {"id": article_id, "headline": "A yap surfaced in the feed",
            "content_type": "yap", "tags": [],
            "created_at": datetime.now(timezone.utc).isoformat(), "author": {}}


def _prepare_phase4_harness(agent, monkeypatch, tmp_path, articles,
                             yaps_by_article=None, max_comments=10):
    """Shared Phase-4 end-to-end harness: COMMENT_ONLY + DRY_RUN, a real AgentDB
    against a tmp file, Telegram poisoned out, LN read/auth faked. Mirrors the
    harness in test_comment_gate.py::_prepare_phase4_harness, extended with
    per-article yap fixtures for exercising the yap-vote call sites.

    Returns (dry_run_log_path, db_path, RealAgentDB).
    """
    monkeypatch.setattr(agent, "COMMENT_ONLY", True)
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "MAX_COMMENTS_PER_CYCLE", max_comments)
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

    yaps_by_article = yaps_by_article or {}
    monkeypatch.setattr(
        agent.LNClient, "get_yaps",
        lambda self, article_id: yaps_by_article.get(article_id, []),
    )
    monkeypatch.setattr(
        agent.LNClient, "get_recent_articles",
        lambda self, per_page=20, status="approved": articles,
    )

    # Gating/crafting are not under test here — keep them trivially SUBSTANCE
    # so every news article posts a comment, proving commenting is unaffected.
    monkeypatch.setattr(agent, "gate_comment", lambda headline, tags: "SUBSTANCE")
    monkeypatch.setattr(
        agent, "craft_comment",
        lambda headline, tags, article_url="", **_kw: "A sufficiently long analysis comment for voting tests.",
    )

    async def _instant_sleep(*a, **kw):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    # Neutralize the operator-alerting startup check: unrelated to what these
    # tests exercise, and would otherwise shell out to the real CLAUDE_BIN and
    # write to the real agent.db (via alerts.py's default db path) every run.
    monkeypatch.setattr(agent, "check_provider_startup_viable", lambda: None)

    return dry_run_log, db_path, RealAgentDB


def _read_dry_run_lines(dry_run_log):
    if not dry_run_log.exists():
        return []
    return [json.loads(l) for l in dry_run_log.read_text().strip().splitlines() if l.strip()]


def _poison(agent, monkeypatch, name):
    """Replace a module-level function with one that fails the test if called."""
    def _boom(*a, **kw):
        raise AssertionError(f"{name} must not be called when VOTING_ENABLED is False")
    monkeypatch.setattr(agent, name, _boom)


# ─── VOTING_ENABLED=False: no vote-related function is ever called ──────────

def test_voting_disabled_never_calls_vote_functions_but_still_comments(agent, monkeypatch, tmp_path):
    """The budget-win case: with voting disabled, none of the classifier
    functions or ln.vote() are called at all (poisoned here to prove it,
    covering BOTH the top-level yap-item vote site and the per-article yap
    vote site) while gating/commenting proceed exactly as before."""
    monkeypatch.setattr(agent, "VOTING_ENABLED", False)

    articles = _make_articles(2) + [_make_yap_item(100)]
    yaps_by_article = {
        1: [{"id": 501, "text": "Great analysis, well sourced.", "author": {"id": 1, "username": "someone"}}],
    }
    dry_run_log, db_path, RealAgentDB = _prepare_phase4_harness(
        agent, monkeypatch, tmp_path, articles, yaps_by_article=yaps_by_article,
    )

    for fn_name in ("batch_evaluate_articles", "evaluate_article_quality",
                    "batch_evaluate_comments", "evaluate_comment_quality"):
        _poison(agent, monkeypatch, fn_name)

    def poisoned_vote(self, *a, **kw):
        raise AssertionError("ln.vote() must not be called when VOTING_ENABLED is False")

    monkeypatch.setattr(agent.LNClient, "vote", poisoned_vote)

    asyncio.run(agent.run_agent())

    lines = _read_dry_run_lines(dry_run_log)
    assert not any(l["action"] == "vote" for l in lines)

    post_yap_entries = [l for l in lines if l["action"] == "post_yap"]
    posted_ids = {e["args"]["content_id"] for e in post_yap_entries}
    assert posted_ids == {1, 2}  # both news articles still commented on normally

    check_db = RealAgentDB(db_path)
    try:
        row = check_db._execute(
            "SELECT articles_voted, articles_commented FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["articles_voted"] == 0
        assert row["articles_commented"] == 2
    finally:
        check_db.close()


def test_voting_disabled_still_fetches_yaps_for_reply_walking(agent, monkeypatch, tmp_path):
    """The entangled bit: ln.get_yaps() must still be called even with voting
    disabled — walk_replies_and_respond() depends on it — only the vote
    collection/casting built from those yaps is skipped."""
    monkeypatch.setattr(agent, "VOTING_ENABLED", False)

    articles = _make_articles(1)
    get_yaps_calls = []

    def tracked_get_yaps(self, article_id):
        get_yaps_calls.append(article_id)
        return []

    dry_run_log, db_path, RealAgentDB = _prepare_phase4_harness(agent, monkeypatch, tmp_path, articles)
    monkeypatch.setattr(agent.LNClient, "get_yaps", tracked_get_yaps)

    for fn_name in ("batch_evaluate_articles", "evaluate_article_quality",
                    "batch_evaluate_comments", "evaluate_comment_quality"):
        _poison(agent, monkeypatch, fn_name)
    monkeypatch.setattr(agent.LNClient, "vote",
                         lambda self, *a, **kw: (_ for _ in ()).throw(
                             AssertionError("ln.vote() must not be called")))

    asyncio.run(agent.run_agent())

    assert get_yaps_calls == [1]  # still fetched, despite voting being off


# ─── VOTING_ENABLED=True: original vote behavior is intact ──────────────────

def test_voting_enabled_casts_votes_on_articles_and_yaps(agent, monkeypatch, tmp_path):
    """With VOTING_ENABLED=1, votes are cast for all three Phase 4 vote call
    sites: a news article, a top-level yap-content-type item, and a
    per-article yap comment — while commenting still proceeds too."""
    monkeypatch.setattr(agent, "VOTING_ENABLED", True)

    articles = _make_articles(1) + [_make_yap_item(100)]
    yaps_by_article = {
        1: [{"id": 501, "text": "Solid, well-sourced take.", "author": {"id": 1, "username": "someone"}}],
    }
    dry_run_log, db_path, RealAgentDB = _prepare_phase4_harness(
        agent, monkeypatch, tmp_path, articles, yaps_by_article=yaps_by_article,
    )

    monkeypatch.setattr(agent, "batch_evaluate_articles", lambda arts: {})
    monkeypatch.setattr(agent, "evaluate_article_quality", lambda headline, tags: 1)
    monkeypatch.setattr(agent, "batch_evaluate_comments", lambda comments: {})
    monkeypatch.setattr(agent, "evaluate_comment_quality", lambda text, headline: 1)

    asyncio.run(agent.run_agent())

    lines = _read_dry_run_lines(dry_run_log)
    vote_entries = [l for l in lines if l["action"] == "vote"]
    voted_ids = {e["args"]["item_id"] for e in vote_entries}
    assert voted_ids == {1, 100, 501}  # article + yap-type item + per-article yap

    post_yap_entries = [l for l in lines if l["action"] == "post_yap"]
    assert {e["args"]["content_id"] for e in post_yap_entries} == {1}  # commenting unaffected

    check_db = RealAgentDB(db_path)
    try:
        row = check_db._execute(
            "SELECT articles_voted, articles_commented FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # Pre-existing (unrelated to VOTING_ENABLED) quirk: the per-article
        # batch-yap-vote loop calls ln.vote() but never increments the in-memory
        # `voted` counter — only the top-level yap-item and news-article vote
        # branches do. So 3 real votes are cast (proven above via dry_run.log)
        # but the run-summary counter only reflects 2. Not this task's bug to
        # fix; asserting the real, pre-existing value here.
        assert row["articles_voted"] == 2
        assert row["articles_commented"] == 1
    finally:
        check_db.close()
