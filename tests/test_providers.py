"""Tests for the provider-agnostic LLM dispatch layer."""

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from providers import (
    CircuitBreaker,
    ClaudeProvider,
    CodexProvider,
    OpenCodeProvider,
    ProviderChain,
    build_provider_env,
    looks_like_claude_limit_error,
    strip_surrogates,
)


# ─── CircuitBreaker ──────────────────────────────────────────────────────────


def test_breaker_available_until_max_failures():
    cb = CircuitBreaker(max_failures=3, name="t")
    assert cb.is_available()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_available()
    cb.record_failure()
    assert not cb.is_available()


def test_breaker_record_success_resets_count():
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.is_available()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_available()


def test_breaker_open_cooldown_blocks_until_elapsed():
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.open_cooldown(cooldown=60, reason="quota")
    assert not cb.is_available()
    assert cb.cooldown_remaining() > 0


def test_breaker_reset_failures_keeps_cooldown():
    """reset_failures clears transient errors but does NOT clear the quota cooldown."""
    cb = CircuitBreaker(max_failures=3, name="t")
    cb.open_cooldown(cooldown=60, reason="quota")
    cb.reset_failures()
    # Still unavailable because cooldown is independent of the failure count
    assert not cb.is_available()


# ─── Helpers ────────────────────────────────────────────────────────────────


def test_build_provider_env_includes_bin_parent():
    env = build_provider_env("/usr/local/bin/claude")
    assert "/usr/local/bin" in env["PATH"]


def test_strip_surrogates_removes_unpaired():
    s = "hello \udcc4 world"
    out = strip_surrogates(s)
    assert "\udcc4" not in out


def test_looks_like_claude_limit_error_detects_quota():
    assert looks_like_claude_limit_error("", "You hit your limit")
    assert looks_like_claude_limit_error("Error: rate limit exceeded", "")
    assert looks_like_claude_limit_error("status code 501", "")
    assert not looks_like_claude_limit_error("normal response", "")


# ─── ClaudeProvider ─────────────────────────────────────────────────────────


def test_claude_returns_empty_on_timeout():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_returns_empty_on_nonzero_exit():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_detects_quota_error_and_opens_cooldown():
    p = ClaudeProvider(bin="/bin/echo", retries=0, quota_cooldown=3600)
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "rate limit"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""
    # After a quota error, cooldown is open and breaker reports unavailable
    assert not p.is_available()
    assert p.breaker.cooldown_remaining() > 0


def test_claude_detects_soft_error_with_zero_exit():
    """Returncode 0 but stdout starts with 'Error:' is still a failure."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    mock_result = type("R", (), {"returncode": 0, "stdout": "Error: something broke", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_claude_success_returns_stdout_and_resets_breaker():
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    p.breaker.record_failure()
    p.breaker.record_failure()
    mock_result = type("R", (), {"returncode": 0, "stdout": "hello world", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == "hello world"
    # Success resets the consecutive-failure count
    assert p.is_available()


# ─── CodexProvider ──────────────────────────────────────────────────────────


def test_codex_returns_empty_on_failure():
    p = CodexProvider(bin="/bin/echo")
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "codex error"})()
    with patch("subprocess.run", return_value=mock_result):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_codex_returns_empty_on_timeout():
    p = CodexProvider(bin="/bin/echo")
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=1)):
        result = p.ask("test", timeout=1)
    assert result == ""


def test_codex_failure_records_breaker():
    p = CodexProvider(bin="/bin/echo")
    mock_result = type("R", (), {"returncode": 1, "stdout": "", "stderr": "err"})()
    with patch("subprocess.run", return_value=mock_result):
        for _ in range(3):
            p.ask("test", timeout=1)
    assert not p.is_available()


# ─── OpenCodeProvider ───────────────────────────────────────────────────────


def test_opencode_unavailable_when_not_configured():
    """An OpenCode provider with an empty model is always unavailable."""
    p = OpenCodeProvider(bin="/bin/echo", model="")
    assert not p.is_available()
    assert p.ask("test", timeout=1) == ""


def test_opencode_available_when_configured():
    p = OpenCodeProvider(bin="/bin/echo", model="some/model")
    assert p.is_available()


# ─── ProviderChain ──────────────────────────────────────────────────────────


def test_chain_returns_first_non_empty():
    """The chain returns whatever the first available provider produces."""
    primary = ClaudeProvider(bin="/bin/echo", retries=0)
    secondary = CodexProvider(bin="/bin/echo")
    chain = ProviderChain([primary, secondary])
    mock_result = type("R", (), {"returncode": 0, "stdout": "primary won", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = chain.ask("test", timeout=1)
    assert result == "primary won"


def test_chain_falls_through_on_empty_primary():
    """When the primary returns empty (e.g. subprocess failed), the next provider runs."""
    primary = ClaudeProvider(bin="/bin/echo", retries=0)
    secondary = CodexProvider(bin="/bin/echo")
    chain = ProviderChain([primary, secondary])

    claude_fail = type("R", (), {"returncode": 1, "stdout": "", "stderr": "fail"})()
    codex_ok = type("R", (), {"returncode": 0, "stdout": "codex won", "stderr": ""})()

    # subprocess.run is called twice: once for Claude (fails), once for Codex (succeeds)
    with patch("subprocess.run", side_effect=[claude_fail, codex_ok]):
        result = chain.ask("test", timeout=1)
    assert result == "codex won"


def test_chain_from_env_order_respects_env():
    claude = ClaudeProvider(bin="/bin/echo")
    codex = CodexProvider(bin="/bin/echo")
    os.environ["_TEST_ORDER"] = "codex,claude"
    try:
        chain = ProviderChain.from_env_order(
            "_TEST_ORDER", default="claude,codex",
            providers={"claude": claude, "codex": codex})
        assert chain.names() == ["codex", "claude"]
    finally:
        del os.environ["_TEST_ORDER"]


def test_chain_from_env_order_falls_back_to_default():
    claude = ClaudeProvider(bin="/bin/echo")
    codex = CodexProvider(bin="/bin/echo")
    # Env var is unset
    os.environ.pop("_NONEXISTENT_TEST_ENV", None)
    chain = ProviderChain.from_env_order(
        "_NONEXISTENT_TEST_ENV", default="codex,claude",
        providers={"claude": claude, "codex": codex})
    assert chain.names() == ["codex", "claude"]


def test_chain_from_env_order_skips_unknown():
    claude = ClaudeProvider(bin="/bin/echo")
    os.environ["_TEST_UNKNOWN_ORDER"] = "ghost,claude,phantom"
    try:
        chain = ProviderChain.from_env_order(
            "_TEST_UNKNOWN_ORDER", default="claude",
            providers={"claude": claude})
        # Unknown names are logged + dropped
        assert chain.names() == ["claude"]
    finally:
        del os.environ["_TEST_UNKNOWN_ORDER"]


def test_chain_ask_skips_unavailable_providers():
    """Providers reporting unavailable (e.g. unconfigured OpenCode) are silently skipped."""
    opencode = OpenCodeProvider(bin="/bin/echo", model="")  # unavailable
    claude = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([opencode, claude])

    mock_result = type("R", (), {"returncode": 0, "stdout": "claude won", "stderr": ""})()
    with patch("subprocess.run", return_value=mock_result):
        result = chain.ask("test", timeout=1)
    assert result == "claude won"


def test_chain_reset_failures_resets_all():
    p1 = ClaudeProvider(bin="/bin/echo")
    p2 = CodexProvider(bin="/bin/echo")
    p1.breaker.record_failure()
    p2.breaker.record_failure()
    chain = ProviderChain([p1, p2])
    chain.reset_failures()
    # Both providers' transient failure counts are cleared
    assert p1.is_available()
    assert p2.is_available()


def test_chain_strips_surrogates_before_dispatch():
    """The chain strips unpaired surrogates so downstream providers don't see them."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([p])

    captured = []

    def fake_run(*args, **kwargs):
        captured.append(kwargs.get("input", ""))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    with patch("subprocess.run", side_effect=fake_run):
        chain.ask("hello \udcc4 world", timeout=1)
    assert captured
    assert "\udcc4" not in captured[0]


# ─── Tier resolution ───────────────────────────────────────────────────────


def _captured_subprocess_args():
    """Return a (calls, fake_run) pair. fake_run records each subprocess.run
    invocation's positional args (command list)."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    return calls, fake_run


def test_claude_tier_classification_uses_sonnet_low():
    """tier='classification' on Claude resolves to sonnet/low by default."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification")
    assert calls
    cmd = calls[0]
    # --effort low expected
    assert "--effort" in cmd and "low" in cmd
    # --model sonnet expected (tier preset injects it)
    assert "--model" in cmd and "sonnet" in cmd


def test_claude_per_call_overrides_tier():
    """Explicit per-call model/effort kwargs beat the tier preset."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification", model="opus", effort="high")
    cmd = calls[0]
    assert "opus" in cmd
    assert "high" in cmd


def test_claude_no_tier_falls_through_to_defaults():
    """No tier and no per-call kwargs → construction defaults are used."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "max" in cmd


def test_codex_tier_classification_uses_low_effort():
    """tier='classification' on Codex drops effort to low (model unchanged)."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="classification")
    cmd = calls[0]
    # -c model_reasoning_effort=low expected
    assert any("model_reasoning_effort=low" in c for c in cmd)
    # default model preserved
    assert "gpt-5.5" in cmd


def test_codex_per_call_model_overrides_construction():
    """Per-call model kwarg overrides the construction model."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, model="gpt-5.6")
    cmd = calls[0]
    assert "gpt-5.6" in cmd
    # effort still xhigh (default)
    assert any("model_reasoning_effort=xhigh" in c for c in cmd)


def test_codex_no_tier_uses_construction_defaults():
    """With no tier and no overrides, Codex uses construction-time defaults."""
    p = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1)
    cmd = calls[0]
    assert "gpt-5.5" in cmd
    assert any("model_reasoning_effort=xhigh" in c for c in cmd)


def test_chain_forwards_tier_kwarg():
    """ProviderChain.ask passes `tier=` through to the underlying provider."""
    codex = CodexProvider(bin="/bin/echo", model="gpt-5.5", effort="xhigh")
    chain = ProviderChain([codex])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        chain.ask("test", timeout=1, tier="classification")
    cmd = calls[0]
    assert any("model_reasoning_effort=low" in c for c in cmd)


def test_unknown_tier_silently_falls_through_to_defaults():
    """An unknown tier name doesn't error — it just doesn't apply any preset."""
    p = ClaudeProvider(bin="/bin/echo", default_effort="max", retries=0)
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        p.ask("test", timeout=1, tier="this_tier_does_not_exist")
    cmd = calls[0]
    assert "max" in cmd


def test_tier_mutation_is_isolated_between_instances():
    """Mutating one provider's tier dict must not bleed into other instances
    or into the module-level default. Catches the shallow-copy aliasing trap."""
    from providers import _CLAUDE_DEFAULT_TIERS
    original_default = _CLAUDE_DEFAULT_TIERS["classification"]["effort"]

    p1 = ClaudeProvider(bin="/bin/echo")
    p1.tiers["classification"]["effort"] = "high"

    p2 = ClaudeProvider(bin="/bin/echo")
    assert p2.tiers["classification"]["effort"] == original_default
    assert _CLAUDE_DEFAULT_TIERS["classification"]["effort"] == original_default


def test_chain_forwards_tier_and_overrides_together():
    """ProviderChain.ask passes tier AND per-call overrides through, with the
    per-call kwargs winning over the tier preset."""
    p = ClaudeProvider(bin="/bin/echo", retries=0)
    chain = ProviderChain([p])
    calls, fake = _captured_subprocess_args()
    with patch("subprocess.run", side_effect=fake):
        chain.ask("test", timeout=1, tier="classification",
                  model="opus", effort="high")
    cmd = calls[0]
    # Per-call kwargs beat the classification preset (sonnet/low)
    assert "opus" in cmd
    assert "high" in cmd
    assert "sonnet" not in cmd
