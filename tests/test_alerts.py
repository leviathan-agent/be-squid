"""Tests for alerts.py — operator Telegram alerting.

Covers: config resolution precedence, env-file parsing (only two keys),
operator_alert's exception-swallowing, episode-dedup transition logic, and
the ln-agent.py wiring (startup check, chain-exhausted runtime check,
DRY_RUN routing) via the shared `agent` fixture from conftest.py.

Tests that exercise `agent` always point `agent.DB_FILE` at a tmp_path first
— alerts.py's episode-dedup table lives in that same sqlite file, and the
wiring in ln-agent.py (_notify_transition) looks up `DB_FILE` fresh on every
call, so this is enough to keep tests from touching the real agent.db.
"""

import json
import shutil
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import alerts


# ─── Config resolution ──────────────────────────────────────────────────────

def test_config_env_vars_win(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_config_cache", None)
    monkeypatch.setenv(alerts.TOKEN_ENV, "env-token")
    monkeypatch.setenv(alerts.CHANNEL_ENV, "env-channel")
    # Env file exists too, with different values — env must still win.
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nFIREPANBOT_OPERATOR_CHANNEL_ID=file-channel\n"
    )
    monkeypatch.setenv(alerts.ENV_FILE_ENV, str(env_file))

    assert alerts.load_alert_config(force=True) == ("env-token", "env-channel")


def test_config_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_config_cache", None)
    monkeypatch.delenv(alerts.TOKEN_ENV, raising=False)
    monkeypatch.delenv(alerts.CHANNEL_ENV, raising=False)
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nFIREPANBOT_OPERATOR_CHANNEL_ID=file-channel\n"
    )
    monkeypatch.setenv(alerts.ENV_FILE_ENV, str(env_file))

    assert alerts.load_alert_config(force=True) == ("file-token", "file-channel")


def test_config_disabled_when_neither_present(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_config_cache", None)
    monkeypatch.delenv(alerts.TOKEN_ENV, raising=False)
    monkeypatch.delenv(alerts.CHANNEL_ENV, raising=False)
    monkeypatch.setenv(alerts.ENV_FILE_ENV, str(tmp_path / "does-not-exist.env"))

    assert alerts.load_alert_config(force=True) == (None, None)


def test_config_partial_env_vars_fall_through_to_file(monkeypatch, tmp_path):
    """Only one of the two env vars set is treated as 'not configured via
    env' — falls through to the file rather than half-using an env value."""
    monkeypatch.setattr(alerts, "_config_cache", None)
    monkeypatch.setenv(alerts.TOKEN_ENV, "env-token-only")
    monkeypatch.delenv(alerts.CHANNEL_ENV, raising=False)
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=file-token\nFIREPANBOT_OPERATOR_CHANNEL_ID=file-channel\n"
    )
    monkeypatch.setenv(alerts.ENV_FILE_ENV, str(env_file))

    assert alerts.load_alert_config(force=True) == ("file-token", "file-channel")


def test_config_cached_between_calls_without_force(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "_config_cache", None)
    monkeypatch.setenv(alerts.TOKEN_ENV, "first-token")
    monkeypatch.setenv(alerts.CHANNEL_ENV, "first-channel")
    assert alerts.load_alert_config() == ("first-token", "first-channel")

    # Changing env after the fact has no effect without force=True.
    monkeypatch.setenv(alerts.TOKEN_ENV, "second-token")
    assert alerts.load_alert_config() == ("first-token", "first-channel")
    assert alerts.load_alert_config(force=True) == ("second-token", "first-channel")


# ─── Env file parsing — only two keys ───────────────────────────────────────

def test_parse_env_file_reads_only_two_keys(tmp_path):
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=super-secret-anthropic-key\n"
        "TELEGRAM_BOT_TOKEN=my-bot-token\n"
        "FIREPANBOT_OPERATOR_CHANNEL_ID=-1009876543210\n"
        "FIREPANBOT_DAILY_USD_CAP_GLOBAL=50\n"
        "# a comment line\n"
        "\n"
    )
    parsed = alerts._parse_env_file(env_file)
    assert parsed == {
        "TELEGRAM_BOT_TOKEN": "my-bot-token",
        "FIREPANBOT_OPERATOR_CHANNEL_ID": "-1009876543210",
    }


def test_parse_env_file_handles_quotes_and_export_prefix(tmp_path):
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text(
        'export TELEGRAM_BOT_TOKEN="quoted-token"\n'
        "FIREPANBOT_OPERATOR_CHANNEL_ID='quoted-channel'\n"
    )
    parsed = alerts._parse_env_file(env_file)
    assert parsed == {
        "TELEGRAM_BOT_TOKEN": "quoted-token",
        "FIREPANBOT_OPERATOR_CHANNEL_ID": "quoted-channel",
    }


def test_parse_env_file_missing_file_returns_empty(tmp_path):
    assert alerts._parse_env_file(tmp_path / "nope.env") == {}


def test_parse_env_file_ignores_blank_values(tmp_path):
    env_file = tmp_path / "firepanbot.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=\nFIREPANBOT_OPERATOR_CHANNEL_ID=chan\n")
    assert alerts._parse_env_file(env_file) == {"FIREPANBOT_OPERATOR_CHANNEL_ID": "chan"}


# ─── operator_alert — exception swallowing + prefixing ──────────────────────

def test_operator_alert_no_config_returns_false(monkeypatch):
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": None, "channel": None})
    assert alerts.operator_alert("hello") is False


def test_operator_alert_disabled_returns_false(monkeypatch):
    monkeypatch.setenv(alerts.DISABLE_ENV, "1")
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    assert alerts.operator_alert("hello") is False


def test_operator_alert_swallows_network_exception(monkeypatch):
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        result = alerts.operator_alert("network is down")
    assert result is False  # never raises


def test_operator_alert_swallows_arbitrary_exception(monkeypatch):
    """Not just URLError — ANY exception from the send path must be swallowed."""
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    with patch("urllib.request.urlopen", side_effect=RuntimeError("weird failure")):
        result = alerts.operator_alert("whatever")
    assert result is False


def _fake_response(status=200):
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_operator_alert_success_returns_true(monkeypatch):
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    with patch("urllib.request.urlopen", return_value=_fake_response(200)):
        result = alerts.operator_alert("all good")
    assert result is True


def test_operator_alert_non_2xx_returns_false(monkeypatch):
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    with patch("urllib.request.urlopen", return_value=_fake_response(500)):
        result = alerts.operator_alert("all good")
    assert result is False


def test_operator_alert_prefixes_message_and_targets_channel(monkeypatch):
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    monkeypatch.setattr(alerts, "_config_cache", {"token": "tok", "channel": "chan"})
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["url"] = req.full_url
        return _fake_response(200)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        alerts.operator_alert("chain is dead")

    assert captured["body"]["text"] == f"{alerts.ALERT_PREFIX}chain is dead"
    assert captured["body"]["chat_id"] == "chan"
    assert captured["url"].endswith("/bottok/sendMessage")


# ─── Episode dedup ──────────────────────────────────────────────────────────

def test_transition_fail_fail_fires_once(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", True, "first", db_path=db, notify=calls.append)
    alerts.alert_state_transition("comp", True, "second", db_path=db, notify=calls.append)
    assert len(calls) == 1


def test_transition_fail_ok_fail_fires_three_times(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", True, "down", db_path=db, notify=calls.append)
    alerts.alert_state_transition("comp", False, "up", db_path=db, notify=calls.append)
    alerts.alert_state_transition("comp", True, "down again", db_path=db, notify=calls.append)
    assert len(calls) == 3


def test_transition_ok_ok_never_fires(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", False, "", db_path=db, notify=calls.append)
    alerts.alert_state_transition("comp", False, "", db_path=db, notify=calls.append)
    assert calls == []


def test_transition_separate_components_are_independent(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("provider-startup", True, "", db_path=db, notify=calls.append)
    alerts.alert_state_transition("provider-chain", True, "", db_path=db, notify=calls.append)
    assert len(calls) == 2  # different components — both are fresh ok->failing


def test_transition_message_content(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", True, "boom", db_path=db, notify=calls.append)
    alerts.alert_state_transition("comp", False, "fixed", db_path=db, notify=calls.append)
    assert calls[0] == "comp FAILING — boom"
    assert calls[1] == "comp RECOVERED — fixed"


def test_transition_message_omits_dash_with_no_detail(tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", True, "", db_path=db, notify=calls.append)
    assert calls[0] == "comp FAILING"


def test_transition_persists_across_reconnects(tmp_path):
    """State survives a fresh sqlite connection — i.e. a process restart."""
    db = tmp_path / "alert_state.db"
    calls = []
    alerts.alert_state_transition("comp", True, "", db_path=db, notify=calls.append)
    assert alerts.get_alert_state("comp", db_path=db) is True
    alerts.alert_state_transition("comp", True, "", db_path=db, notify=calls.append)
    assert len(calls) == 1  # still deduped after "restart"


def test_transition_notify_exception_does_not_propagate(tmp_path):
    db = tmp_path / "alert_state.db"

    def boom(_text):
        raise RuntimeError("notify blew up")

    fired = alerts.alert_state_transition("comp", True, "", db_path=db, notify=boom)
    assert fired is True  # the transition itself still "fired" despite notify raising


def test_transition_disabled_suppresses_notify_but_still_tracks_state(monkeypatch, tmp_path):
    db = tmp_path / "alert_state.db"
    calls = []
    monkeypatch.setenv(alerts.DISABLE_ENV, "1")
    fired = alerts.alert_state_transition("comp", True, "", db_path=db, notify=calls.append)
    assert calls == []
    assert fired is False

    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    # State was still recorded as failing even though the alert was suppressed:
    # calling again with is_failing=True is a no-op (no transition), proving
    # the DB write happened despite the kill switch.
    fired_again = alerts.alert_state_transition("comp", True, "", db_path=db, notify=calls.append)
    assert fired_again is False
    assert calls == []


def test_transition_defaults_to_operator_alert(monkeypatch, tmp_path):
    db = tmp_path / "alert_state.db"
    monkeypatch.delenv(alerts.DISABLE_ENV, raising=False)
    called = []
    monkeypatch.setattr(alerts, "operator_alert", lambda text: called.append(text) or True)
    alerts.alert_state_transition("comp", True, "no explicit notify", db_path=db)
    assert called == ["comp FAILING — no explicit notify"]


# ─── ln-agent.py wiring: startup check ───────────────────────────────────────

def test_startup_check_alerts_on_bogus_claude_bin(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/nonexistent/bogus-claude-binary")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    agent.check_provider_startup_viable()

    assert len(sent) == 1
    assert "provider-startup" in sent[0]
    assert "FAILING" in sent[0]


def test_startup_check_dedups_repeated_failure(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/nonexistent/bogus-claude-binary")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    agent.check_provider_startup_viable()
    agent.check_provider_startup_viable()
    agent.check_provider_startup_viable()

    assert len(sent) == 1  # one failure episode, not one alert per cycle


def test_startup_check_recovers_on_next_call(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    monkeypatch.setattr(agent, "CLAUDE_BIN", "/nonexistent/bogus-claude-binary")
    agent.check_provider_startup_viable()

    healthy_bin = shutil.which("true") or "/usr/bin/true"
    monkeypatch.setattr(agent, "CLAUDE_BIN", healthy_bin)
    agent.check_provider_startup_viable()

    assert len(sent) == 2
    assert "FAILING" in sent[0]
    assert "RECOVERED" in sent[1]


def test_startup_check_noop_when_claude_not_in_chain(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "CLAUDE_BIN", "/nonexistent/bogus-claude-binary")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)
    monkeypatch.setattr(agent._provider_chain, "get", lambda name: None)

    agent.check_provider_startup_viable()

    assert sent == []


# ─── ln-agent.py wiring: chain-exhausted runtime check (llm_ask) ────────────

def test_llm_ask_alerts_on_chain_exhausted(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "AGENT_SOUL", "")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    def fake_ask(prompt, timeout=3600, **kwargs):
        agent._provider_chain.last_exhausted = True
        agent._provider_chain.last_error = "no provider available: claude(cooldown=999s)"
        return ""

    monkeypatch.setattr(agent._provider_chain, "ask", fake_ask)

    result = agent.llm_ask("do the thing")

    assert result == ""
    assert len(sent) == 1
    assert "provider-chain" in sent[0]
    assert "FAILING" in sent[0]


def test_llm_ask_no_alert_when_attempted_but_empty(agent, monkeypatch, tmp_path):
    """A provider that ran and returned empty output is NOT chain exhaustion
    (see providers.py's ProviderChain.ask() docstring) — no alert fires."""
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "AGENT_SOUL", "")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    def fake_ask(prompt, timeout=3600, **kwargs):
        agent._provider_chain.last_exhausted = False
        agent._provider_chain.last_error = ""
        return ""

    monkeypatch.setattr(agent._provider_chain, "ask", fake_ask)

    result = agent.llm_ask("do the thing")

    assert result == ""
    assert sent == []


def test_llm_ask_recovers_on_next_success(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "AGENT_SOUL", "")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    def fake_ask_fail(prompt, timeout=3600, **kwargs):
        agent._provider_chain.last_exhausted = True
        agent._provider_chain.last_error = "dead"
        return ""

    def fake_ask_ok(prompt, timeout=3600, **kwargs):
        agent._provider_chain.last_exhausted = False
        agent._provider_chain.last_error = ""
        return "a real response"

    monkeypatch.setattr(agent._provider_chain, "ask", fake_ask_fail)
    agent.llm_ask("first call")
    monkeypatch.setattr(agent._provider_chain, "ask", fake_ask_ok)
    result = agent.llm_ask("second call")

    assert result == "a real response"
    assert len(sent) == 2
    assert "FAILING" in sent[0]
    assert "RECOVERED" in sent[1]


def test_llm_ask_classification_tier_never_alerts(agent, monkeypatch, tmp_path):
    """Classification-tier calls are cheap/frequent — excluded from alerting
    on purpose, even when the chain is genuinely exhausted."""
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")
    monkeypatch.setattr(agent, "AGENT_SOUL", "")
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    def fake_ask(prompt, timeout=3600, **kwargs):
        agent._provider_chain.last_exhausted = True
        agent._provider_chain.last_error = "dead"
        return ""

    monkeypatch.setattr(agent._provider_chain, "ask", fake_ask)

    agent.llm_ask("classify this", tier="classification")

    assert sent == []


# ─── ln-agent.py wiring: DRY_RUN routing ─────────────────────────────────────

def test_alert_notify_dry_run_routes_to_log_not_telegram(agent, monkeypatch, tmp_path):
    log_path = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "DRY_RUN_LOG", log_path)
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    agent._alert_notify("provider-chain FAILING — test detail")

    assert sent == []  # Telegram never touched under DRY_RUN
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "alert"
    assert entry["text"] == "provider-chain FAILING — test detail"
    assert "ts" in entry


def test_alert_notify_live_mode_calls_operator_alert(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "DRY_RUN", False)
    sent = []
    monkeypatch.setattr(agent.alerts, "operator_alert", lambda text: sent.append(text) or True)

    agent._alert_notify("hello")

    assert sent == ["hello"]


def test_notify_transition_end_to_end_dry_run(agent, monkeypatch, tmp_path):
    """Full path: _notify_transition -> alerts.alert_state_transition ->
    agent._alert_notify -> dry_run.log, with real dedup against agent.db."""
    log_path = tmp_path / "dry_run.log"
    monkeypatch.setattr(agent, "DRY_RUN", True)
    monkeypatch.setattr(agent, "DRY_RUN_LOG", log_path)
    monkeypatch.setattr(agent, "DB_FILE", tmp_path / "agent.db")

    fired1 = agent._notify_transition("provider-chain", True, "first failure")
    fired2 = agent._notify_transition("provider-chain", True, "still failing")
    fired3 = agent._notify_transition("provider-chain", False, "recovered")

    assert (fired1, fired2, fired3) == (True, False, True)
    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert all(l["action"] == "alert" for l in lines)
    assert "FAILING" in lines[0]["text"]
    assert "RECOVERED" in lines[1]["text"]
