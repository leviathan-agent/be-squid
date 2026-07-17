"""Operator alerting via Telegram — self-contained, stdlib-only.

be-squid runs unattended on a Mac Mini under a supervisor (PM2) that just
restarts a crashed process — it can't tell the operator "the LLM provider
chain is dead" on its own. This module gives ln-agent.py a narrow, dependable
way to do that: send exactly one Telegram message when a component
transitions from healthy to failing, and exactly one more when it recovers.
No per-cycle spam, no missed recoveries.

Design decisions:

- Config resolution: SQUID_ALERT_BOT_TOKEN / SQUID_ALERT_CHANNEL_ID env vars
  win; otherwise parsed from ALERT_ENV_FILE (a firepanbot-style .env, default
  ~/dev/firepan/tools/firepanbot/.env), reading ONLY TELEGRAM_BOT_TOKEN and
  FIREPANBOT_OPERATOR_CHANNEL_ID from it. Neither present -> alerting is
  silently disabled (logged once, never raises).

- SQUID_ prefix, not ALERT_*: ln-agent.py's .env.example already documents an
  (unimplemented) ALERT_CHANNEL_ID for a different, future "cycle summary"
  feature. Reusing that exact name here would collide with that reservation
  the moment someone wires the other feature up, so this module's env vars
  are namespaced SQUID_ALERT_BOT_TOKEN / SQUID_ALERT_CHANNEL_ID instead.

- Episode dedup lives in its OWN tiny sqlite table/connection, NOT AgentDB:
  ln-agent.py can't be `import`ed normally (hyphen in the filename — see
  tests/conftest.py, which loads it via importlib.util.spec_from_file_location
  under the module name "agent"). Having alerts.py reach back into AgentDB
  would mean either duplicating that importlib dance or creating a real
  circular-import hazard (ln-agent.py imports alerts at module scope, so
  alerts importing AgentDB back out of ln-agent.py would try to load a
  still-initializing module). A ~15-line standalone table sidesteps all of
  that and keeps this file importable in isolation, as the tests do — it
  still lives in the same `agent.db` file, just via its own connection.

- DRY_RUN routing is NOT this module's job: alerts.py has no idea what
  DRY_RUN or dry_run.log are. `alert_state_transition()` takes an optional
  `notify` callable (defaults to `operator_alert`); ln-agent.py supplies its
  own DRY_RUN-aware notify function at the call site. Keeps this module
  reusable and testable without needing ln-agent.py's globals.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

ALERT_PREFIX = "\U0001f991 be-squid: "  # squid emoji
TELEGRAM_API_BASE = "https://api.telegram.org"

TOKEN_ENV = "SQUID_ALERT_BOT_TOKEN"
CHANNEL_ENV = "SQUID_ALERT_CHANNEL_ID"
ENV_FILE_ENV = "ALERT_ENV_FILE"
DISABLE_ENV = "ALERT_DISABLE"
DEFAULT_ENV_FILE = "~/dev/firepan/tools/firepanbot/.env"
_ENV_FILE_KEYS = ("TELEGRAM_BOT_TOKEN", "FIREPANBOT_OPERATOR_CHANNEL_ID")

_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "agent.db"

_config_lock = threading.Lock()
_config_cache: dict | None = None  # {"token": str | None, "channel": str | None}
_startup_logged = False

_state_lock = threading.Lock()


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def alerts_disabled() -> bool:
    """Master kill switch — ALERT_DISABLE=1 suppresses every alert (Telegram
    or DRY_RUN log) regardless of config. Does not affect state tracking:
    transitions are still recorded, so re-enabling picks up cleanly."""
    return _truthy(os.environ.get(DISABLE_ENV, ""))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Extract ONLY the two keys we care about from a firepanbot-style .env
    file (simple KEY=VALUE lines, optional quotes, optional `export ` prefix).
    Mirrors the `load_key()` shell helper in gerrithall-watch.sh. Never logs
    values. Returns {} on any read error (missing file, permission denied,
    directory, etc.) — that's the normal "not on this machine" case."""
    found: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return found
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key not in _ENV_FILE_KEYS:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            found[key] = value
    return found


def load_alert_config(force: bool = False) -> tuple[str | None, str | None]:
    """Resolve (bot_token, channel_id). Env vars win; else ALERT_ENV_FILE;
    else disabled. Cached after the first call within a process — pass
    force=True to re-resolve (used by tests that flip env vars mid-run)."""
    global _config_cache, _startup_logged
    with _config_lock:
        if _config_cache is not None and not force:
            return _config_cache["token"], _config_cache["channel"]

        token = os.environ.get(TOKEN_ENV, "").strip()
        channel = os.environ.get(CHANNEL_ENV, "").strip()
        source = "env" if (token and channel) else None

        if not (token and channel):
            env_file = Path(os.environ.get(ENV_FILE_ENV, DEFAULT_ENV_FILE)).expanduser()
            parsed = _parse_env_file(env_file)
            file_token = parsed.get("TELEGRAM_BOT_TOKEN", "")
            file_channel = parsed.get("FIREPANBOT_OPERATOR_CHANNEL_ID", "")
            if file_token and file_channel:
                token, channel = file_token, file_channel
                source = f"file:{env_file}"

        _config_cache = {"token": token or None, "channel": channel or None}

        if force:
            _startup_logged = False
        if not _startup_logged:
            _startup_logged = True
            if source:
                log.info(f"alerts: operator alerting enabled (source: {source})")
            else:
                env_file_shown = os.environ.get(ENV_FILE_ENV, DEFAULT_ENV_FILE)
                log.info(
                    f"alerts: operator alerting DISABLED — no {TOKEN_ENV}/{CHANNEL_ENV} "
                    f"env vars and no usable env file at {env_file_shown} "
                    f"(set those env vars, or point {ENV_FILE_ENV} at a firepanbot "
                    f".env, to enable)"
                )
        return _config_cache["token"], _config_cache["channel"]


def operator_alert(text: str) -> bool:
    """Best-effort Telegram send to the operator. Never raises — every
    failure (missing config, network error, bad response) is logged and
    swallowed. Returns True only on an apparently-successful send."""
    if alerts_disabled():
        log.info(f"alerts: ALERT_DISABLE set — suppressing alert: {text[:80]}")
        return False
    try:
        token, channel_id = load_alert_config()
        if not token or not channel_id:
            log.info(f"alerts: no config resolved — suppressing alert: {text[:80]}")
            return False
        payload = json.dumps({
            "chat_id": channel_id,
            "text": f"{ALERT_PREFIX}{text}",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            ok = 200 <= status < 300
            if not ok:
                log.warning(f"alerts: unexpected Telegram status {status}")
            return ok
    except Exception as e:
        # An alert failure must NEVER propagate and crash the agent loop.
        log.warning(f"alerts: operator_alert failed ({type(e).__name__}): {e}")
        return False


# ─── Episode dedup (own tiny sqlite table — see module docstring) ───────────

def _state_db(db_path: str | Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _DEFAULT_DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS alert_state (
        component TEXT PRIMARY KEY,
        failing INTEGER NOT NULL,
        ts TEXT NOT NULL
    )""")
    conn.commit()
    return conn


def get_alert_state(component: str, db_path: str | Path | None = None) -> bool:
    """True if `component` is currently recorded as failing. Mainly useful
    for tests/inspection — alert_state_transition() does its own locked
    read-then-write internally and doesn't call this."""
    with _state_lock:
        conn = _state_db(db_path)
        try:
            row = conn.execute(
                "SELECT failing FROM alert_state WHERE component = ?", (component,)
            ).fetchone()
            return bool(row[0]) if row else False
        finally:
            conn.close()


def alert_state_transition(component: str, is_failing: bool, detail: str = "",
                            db_path: str | Path | None = None,
                            notify: Callable[[str], None] | None = None) -> bool:
    """Record `component`'s current failing/ok state; fire an alert ONLY on a
    state TRANSITION (ok->failing sends the failure alert, failing->ok sends
    the recovery alert). Returns True if a transition fired (whether or not
    the underlying send succeeded — that's on `notify`/`operator_alert` to
    report via logging).

    Safe to call every cycle with the same (component, is_failing) pair:
    repeated calls with an unchanged state are no-ops after the first.

    `notify` defaults to `operator_alert`. Callers that need different
    routing (e.g. ln-agent.py's DRY_RUN mode, which writes to dry_run.log
    instead of hitting Telegram) pass their own callable.
    """
    notify = notify or operator_alert

    with _state_lock:
        conn = _state_db(db_path)
        try:
            row = conn.execute(
                "SELECT failing FROM alert_state WHERE component = ?", (component,)
            ).fetchone()
            was_failing = bool(row[0]) if row else False
            conn.execute(
                "INSERT INTO alert_state (component, failing, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(component) DO UPDATE SET failing=excluded.failing, ts=excluded.ts",
                (component, int(is_failing), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    if is_failing == was_failing:
        return False

    if alerts_disabled():
        log.info(f"alerts: ALERT_DISABLE set — suppressing {component} transition")
        return False

    if is_failing:
        message = f"{component} FAILING" + (f" — {detail}" if detail else "")
    else:
        message = f"{component} RECOVERED" + (f" — {detail}" if detail else "")

    try:
        notify(message)
    except Exception as e:
        # Never let a notify() failure (Telegram down, dry_run.log unwritable,
        # a broken test double, whatever) propagate into the caller's cycle.
        log.warning(f"alerts: notify() raised for {component} ({type(e).__name__}): {e}")
    return True
