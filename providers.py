"""Provider-agnostic LLM dispatch layer.

Used by ln-agent.py, benthic-bot.py, and benthic_api.py. Defines:

- `Provider` protocol — what every LLM CLI wrapper looks like.
- `ClaudeProvider`, `CodexProvider`, `OpenCodeProvider` — concrete subprocess wrappers.
- `CircuitBreaker` — shared failure tracking with optional quota cooldown.
- `ProviderChain` — ordered list of providers; first non-empty result wins.

The chain order is driven by the `PROVIDER_ORDER` env var (comma-separated names
like `codex,claude,opencode`). Per-provider model / effort / tool defaults come
from constructor args at each consumer's startup, so a single env-var change is
enough to swap the primary brain — no code edits required.

Design notes:

- Each provider owns its own `CircuitBreaker`. Failures in one don't penalize others.
- Surrogate stripping happens once in `ProviderChain.ask` before dispatch — Claude's
  API breaks on unpaired surrogates so this is mandatory before any Claude call,
  and harmless for the other providers.
- `**_` swallows unknown kwargs in `ask()` so Codex/OpenCode accept (and ignore)
  Claude-only params like `model`, `effort`, `tools` without raising. This lets
  callers pass one kwarg dict that works for any chain ordering.
"""

from __future__ import annotations

import copy
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def strip_surrogates(text: str) -> str:
    """Remove unpaired surrogates — Claude's API JSON parser crashes on them."""
    return text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")


def build_provider_env(bin_path: str | Path) -> dict:
    """Inject the provider binary's directory onto PATH. PM2 and cron commonly
    strip PATH down to a minimal set that misses NVM, ~/.local/bin, etc."""
    parent = str(Path(bin_path).expanduser().parent)
    return {**os.environ, "PATH": f"{parent}:{os.environ.get('PATH', '')}"}


# Patterns that indicate Claude has hit a quota/rate limit and should be
# cooled down rather than retried. Stored as a single delimited string so
# the literal isn't a long sequence of short tokens that secret scanners
# mistake for a BIP-39 seed phrase.
_CLAUDE_LIMIT_PATTERN_BLOB = (
    "status code 501|http 501|error 501"
    "|usage limit|monthly usage|quota|credit balance"
    "|rate limit|too many requests|exhausted"
    "|payment required|billing|overloaded|hit your limit"
)
CLAUDE_LIMIT_PATTERNS = tuple(p.strip() for p in _CLAUDE_LIMIT_PATTERN_BLOB.split("|") if p.strip())


def looks_like_claude_limit_error(stdout: str, stderr: str) -> bool:
    """Detect quota/rate-limit style failures that should trip a longer cooldown."""
    combined = f"{stdout}\n{stderr}".lower()
    return any(p in combined for p in CLAUDE_LIMIT_PATTERNS)


# ─── Circuit breaker ────────────────────────────────────────────────────────

class CircuitBreaker:
    """Thread-safe failure tracking with optional quota cooldown.

    Two states: consecutive-failure count (3 = breaker open) and an absolute
    cooldown timestamp (quota-style errors set this further out than failure
    counting alone would). `is_available()` returns False if either is in
    effect.
    """

    def __init__(self, max_failures: int = 3, name: str = "provider"):
        self.max_failures = max_failures
        self.name = name
        self._failures = 0
        self._unavailable_until = 0.0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        with self._lock:
            if self._unavailable_until > time.time():
                return False
            return self._failures < self.max_failures

    def cooldown_remaining(self) -> int:
        with self._lock:
            return max(0, int(self._unavailable_until - time.time()))

    def record_success(self):
        with self._lock:
            self._failures = 0

    def record_failure(self):
        with self._lock:
            self._failures += 1

    def open_cooldown(self, cooldown: int, reason: str = ""):
        with self._lock:
            until = time.time() + max(60, cooldown)
            self._failures = self.max_failures
            self._unavailable_until = max(self._unavailable_until, until)
        log.warning(f"{self.name} marked unavailable for {max(60, cooldown)}s: {reason[:200]}")

    def reset_failures(self):
        """Clear the consecutive-failure count but NOT the quota cooldown.
        Called at the start of each cycle in long-running agents — gives
        transiently-failed providers a fresh chance without ignoring a real
        quota lockout."""
        with self._lock:
            self._failures = 0


# ─── Provider protocol + implementations ────────────────────────────────────

class Provider(Protocol):
    """The interface every concrete provider must implement.

    `ask` returns the response string, or "" on failure. The caller can't
    distinguish "provider failed cleanly" from "model returned empty output",
    which is intentional — both should trigger fallback to the next provider.
    """
    name: str
    breaker: CircuitBreaker

    def ask(self, prompt: str, *, timeout: int, **kwargs) -> str: ...
    def is_available(self) -> bool: ...


# Default tier presets. Each provider can override or extend at construction.
# Tiers express *intent* ("I want a cheap classification call") without leaking
# any provider's model names into caller code. The Claude-specific "sonnet" and
# Codex-specific "gpt-5.5" stay inside their own provider's tier table.
#
# IMPORTANT: factories below use copy.deepcopy() so per-instance mutations of
# tiers["classification"]["effort"] can't leak back into these module-level
# defaults (each tier maps to a nested dict, so a shallow copy would alias it).
_CLAUDE_DEFAULT_TIERS: dict[str, dict] = {
    "classification": {"model": "sonnet", "effort": "low"},
    # "creative" intentionally omitted — falls through to construction defaults.
}
_CODEX_DEFAULT_TIERS: dict[str, dict] = {
    # No model override — classification stays on the construction model.
    # If a cheaper Codex tier ever ships, set {"model": "gpt-5.5-mini", ...} here.
    "classification": {"effort": "low"},
}
_OPENCODE_DEFAULT_TIERS: dict[str, dict] = {}


@dataclass
class ClaudeProvider:
    """Wraps the `claude` CLI. Retries with exponential backoff, detects
    quota errors to trip a longer cooldown, supports per-call overrides for
    model / effort / tools.

    Tier shortcut: pass `tier="classification"` to request the cheap preset
    (sonnet + low effort by default) without naming Claude-specific models in
    caller code. Per-call kwargs (`model=`, `effort=`, `tools=`) always win
    over tier presets; tier presets win over construction defaults.
    """

    bin: str
    default_model: str | None = None
    default_effort: str = "max"
    default_tools: str = ""
    cwd: str | None = None
    retries: int = 2
    quota_cooldown: int = 6 * 60 * 60
    name: str = "claude"
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_CLAUDE_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="Claude CLI")

    def is_available(self) -> bool:
        return self.breaker.is_available()

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, effort: str | None = None,
            tools: str | None = None, **_) -> str:
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        effective_model = (model if model is not None
                           else tier_cfg.get("model", self.default_model))
        effective_effort = (effort if effort is not None
                            else tier_cfg.get("effort", self.default_effort))
        effective_tools = (tools if tools is not None
                           else tier_cfg.get("tools", self.default_tools))
        # __none__ sentinel matches no real tool. Empty string and omitting the
        # flag both grant ALL tools in Claude CLI — that's almost never what we want.
        if effective_tools == "":
            effective_tools = "__none__"

        for attempt in range(self.retries + 1):
            cooldown = self.breaker.cooldown_remaining()
            if cooldown > 0:
                log.warning(f"Claude cooldown active ({cooldown}s remaining) — skipping")
                return ""
            if not self.breaker.is_available():
                log.warning("Claude CLI circuit breaker open — skipping")
                return ""

            try:
                cmd = [self.bin, "-p", "-",
                       "--effort", effective_effort,
                       "--allowedTools", effective_tools]
                if effective_model:
                    cmd.extend(["--model", effective_model])
                result = subprocess.run(
                    cmd, input=prompt, capture_output=True, text=True,
                    timeout=timeout, env=build_provider_env(self.bin),
                    cwd=self.cwd,
                )
                response = result.stdout.strip() if result.stdout else ""
                stderr_out = result.stderr.strip() if result.stderr else ""
                combined_lower = f"{response}\n{stderr_out}".lower()
                if (result.returncode != 0
                        or not response
                        or response.startswith("Error:")
                        or response == "Execution error"
                        or "max turns" in response.lower()
                        or "max turns" in combined_lower):
                    log.warning(f"Claude error (attempt {attempt+1}/{self.retries+1}): "
                                f"{(response or stderr_out)[:200]}")
                    if stderr_out:
                        log.warning(f"Claude stderr: {stderr_out[:500]}")
                    if looks_like_claude_limit_error(response, stderr_out):
                        self.breaker.open_cooldown(self.quota_cooldown,
                                                    response or stderr_out)
                        return ""
                    if attempt < self.retries:
                        time.sleep(5 * (attempt + 1))
                        continue
                    self.breaker.record_failure()
                    return ""
                self.breaker.record_success()
                return response
            except subprocess.TimeoutExpired:
                log.error(f"Claude CLI timed out (attempt {attempt+1}/{self.retries+1})")
                if attempt < self.retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                self.breaker.record_failure()
                return ""
            except Exception as e:
                log.error(f"Claude CLI error (attempt {attempt+1}/{self.retries+1}): {e}")
                if attempt < self.retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                self.breaker.record_failure()
                return ""
        return ""


@dataclass
class CodexProvider:
    """Wraps the `codex` CLI. Reasoning effort is set via `-c` config override
    because the CLI has no `--effort` flag. Wrapper callable lets each consumer
    inject its own preamble (loaded from a prompt template) without coupling
    providers.py to the prompt-loader machinery.

    Per-call `tier`, `model`, and `effort` kwargs override construction defaults.
    Default classification tier drops effort to "low" without changing the model.
    """

    bin: str
    model: str = "gpt-5.5"
    effort: str = "xhigh"
    cwd: str | None = None
    sandbox_bypass: bool = True
    add_dirs: list[str] = field(default_factory=list)
    wrapper: Callable[[str], str] | None = None
    name: str = "codex"
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_CODEX_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="Codex CLI")

    def is_available(self) -> bool:
        return self.breaker.is_available()

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, effort: str | None = None, **_) -> str:
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        effective_model = (model if model is not None
                           else tier_cfg.get("model", self.model))
        effective_effort = (effort if effort is not None
                            else tier_cfg.get("effort", self.effort))

        wrapped = self.wrapper(prompt) if self.wrapper else prompt
        output_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="codex-out-", suffix=".txt",
                                             delete=False) as tmp:
                output_path = tmp.name
            cmd = [self.bin, "exec", "--skip-git-repo-check", "--ephemeral"]
            if self.sandbox_bypass:
                cmd.append("--dangerously-bypass-approvals-and-sandbox")
            for d in self.add_dirs:
                cmd.extend(["--add-dir", str(Path(d).expanduser())])
            if self.cwd:
                cmd.extend(["-C", str(self.cwd)])
            cmd.extend([
                "-m", effective_model,
                # Codex CLI takes reasoning effort via -c (config override) — there is no --effort flag.
                "-c", f"model_reasoning_effort={effective_effort}",
                "-o", output_path,
                "-",
            ])
            result = subprocess.run(
                cmd, input=wrapped, capture_output=True, text=True,
                timeout=timeout, env=build_provider_env(self.bin),
                cwd=self.cwd,
            )
            response = ""
            if output_path and Path(output_path).exists():
                response = Path(output_path).read_text().strip()
            if not response and result.stdout:
                response = result.stdout.strip()
            if result.returncode != 0 or not response:
                stderr_out = result.stderr.strip() if result.stderr else ""
                log.error(f"Codex failed: {(stderr_out or result.stdout or '')[:500]}")
                self.breaker.record_failure()
                return ""
            self.breaker.record_success()
            return response
        except subprocess.TimeoutExpired:
            log.error("Codex timed out")
            self.breaker.record_failure()
            return ""
        except Exception as e:
            log.error(f"Codex error: {e}")
            self.breaker.record_failure()
            return ""
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except Exception:
                    pass


@dataclass
class OpenCodeProvider:
    """Wraps the `opencode` CLI in non-interactive `run` mode. Treated as
    unavailable when no model is configured — `OPENCODE_MODEL` defaults to ""
    so OpenCode silently sits out unless the operator opts in.

    OpenCode has no effort concept; the tier table only swaps the model.

    NOTE: `is_available()` requires `self.model` to be set at construction.
    A tier preset that supplies a model cannot resurrect an otherwise-unconfigured
    provider — the chain skips it before tier resolution runs. To opt OpenCode
    into the chain, set the `model` constructor arg (or `OPENCODE_MODEL` env)."""

    bin: str
    model: str = ""
    cwd: str | None = None
    wrapper: Callable[[str], str] | None = None
    name: str = "opencode"
    tiers: dict[str, dict] = field(default_factory=lambda: copy.deepcopy(_OPENCODE_DEFAULT_TIERS))
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self):
        self.breaker = CircuitBreaker(max_failures=3, name="OpenCode CLI")

    def is_available(self) -> bool:
        if not self.model:
            return False
        return self.breaker.is_available()

    def ask(self, prompt: str, *, timeout: int = 3600, tier: str | None = None,
            model: str | None = None, **_) -> str:
        tier_cfg = self.tiers.get(tier, {}) if tier else {}
        effective_model = (model if model is not None
                           else tier_cfg.get("model", self.model))
        if not effective_model:
            return ""
        wrapped = self.wrapper(prompt) if self.wrapper else prompt
        try:
            result = subprocess.run(
                [self.bin, "run", "--model", effective_model],
                input=wrapped, capture_output=True, text=True,
                timeout=timeout, env=build_provider_env(self.bin),
                cwd=self.cwd,
            )
            response = result.stdout.strip() if result.stdout else ""
            if result.returncode != 0 or not response:
                log.error(f"OpenCode failed: {(result.stderr or result.stdout or '')[:500]}")
                self.breaker.record_failure()
                return ""
            self.breaker.record_success()
            return response
        except subprocess.TimeoutExpired:
            log.error("OpenCode timed out")
            self.breaker.record_failure()
            return ""
        except FileNotFoundError:
            log.debug("OpenCode binary not found — skipping")
            return ""
        except Exception as e:
            log.error(f"OpenCode error: {e}")
            self.breaker.record_failure()
            return ""


# ─── Chain ───────────────────────────────────────────────────────────────────

class ProviderChain:
    """Ordered list of providers. Tries each in turn, returns the first
    non-empty response. Unknown provider names in the env order are logged
    and skipped — letting you remove a provider from the chain by editing
    PROVIDER_ORDER without code changes."""

    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self._by_name = {p.name: p for p in providers}

    @classmethod
    def from_env_order(cls, env_var: str, default: str,
                       providers: dict[str, Provider]) -> "ProviderChain":
        """Parse an env var like `codex,claude,opencode` into an ordered chain.
        Falls back to `default` if the env var is missing or empty."""
        order_raw = os.environ.get(env_var, default)
        names = [n.strip() for n in order_raw.split(",") if n.strip()]
        if not names:
            names = [n.strip() for n in default.split(",") if n.strip()]
        chain: list[Provider] = []
        for n in names:
            if n in providers:
                chain.append(providers[n])
            else:
                log.warning(f"Unknown provider '{n}' in {env_var} — skipping")
        return cls(chain)

    def ask(self, prompt: str, *, timeout: int = 3600, **kwargs) -> str:
        """Try each available provider until one returns a non-empty string."""
        prompt = strip_surrogates(prompt)
        # Surface typo'd tier names at debug so they're easy to spot in dev
        # without flooding production logs. Unknown tiers still fall through
        # to construction defaults (no crash).
        #
        # Only flag as "unknown" when at least one provider in the chain has a
        # non-empty tier table — otherwise we'd false-positive on chains made
        # entirely of providers that don't customize tiers (e.g. OpenCode-only).
        tier = kwargs.get("tier")
        if tier and any(p.tiers for p in self.providers) \
                and not any(tier in p.tiers for p in self.providers):
            log.debug(f"Unknown tier '{tier}' — no provider has a preset; "
                      f"falling through to construction defaults")
        attempted = False
        for p in self.providers:
            if not p.is_available():
                continue
            if attempted:
                log.warning(f"Falling back to {p.name} for LLM request")
            attempted = True
            result = p.ask(prompt, timeout=timeout, **kwargs)
            if result:
                return result
        return ""

    def reset_failures(self):
        """Reset transient failure counts on every provider. Use at the start
        of each long-running cycle so a previous cycle's transient errors don't
        keep a provider sidelined indefinitely."""
        for p in self.providers:
            p.breaker.reset_failures()

    def get(self, name: str) -> Provider | None:
        """Look up a provider by name. Returns None if not in the chain."""
        return self._by_name.get(name)

    def names(self) -> list[str]:
        """Names of providers in the chain, in order. Useful for startup log."""
        return [p.name for p in self.providers]
