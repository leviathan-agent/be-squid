#!/usr/bin/env python3
"""builder — long-running daemon that consumes the build_tasks queue.

Architecture
------------
- Polls SQLite (agent.db) every BUILD_POLL_INTERVAL seconds for status='pending'.
- One task at a time (serial). Avoids contention, simplifies crash recovery.
- For each task: spawns Codex CLI in a per-task workdir under BUILD_ROOT/<id>/
  with full filesystem access in that dir and full network. Codex receives the brief
  plus a wrapper prompt that names the deliverables (working code, README, push to
  <BUILD_GITHUB_ORG>/<repo_name> via github_client.sh repo push).
- Hard timeout: 6 hours per task. Aim is closer to 30-90 minutes; the wall is just
  to prevent runaway loops from burning the whole window.
- On success: parses the workdir's RESULT.txt for a repo URL, posts a Telegram
  completion message threaded back to the originating chat/message.
- On failure: captures error, posts a failure message with the log path.
- On startup: sweeps stale 'running' rows whose PIDs are gone (process died across
  restart) and marks them 'failed' so the queue doesn't deadlock.

Runs as a PM2 process alongside the agent and bot. Required env:
  BUILD_GITHUB_ORG       GitHub org/user to publish repos under
  BUILD_GIT_USER_NAME    git author name for the initial commit
  BUILD_GIT_USER_EMAIL   git author email for the initial commit
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

HOME = Path.home()
# Default to the script's own directory so the daemon works out of the box.
# Override AGENT_DIR if you keep the agent code somewhere else.
BASE_DIR = Path(os.environ.get("AGENT_DIR", str(Path(__file__).parent)))
DB_FILE = Path(os.environ.get("AGENT_DB", str(BASE_DIR / "agent.db")))
BUILD_ROOT = Path(os.environ.get("BUILD_ROOT", "/tmp/agent-builds"))
LOG_ROOT = Path(os.environ.get("BUILD_LOG_ROOT", "/tmp/agent-build-logs"))
GITHUB_CLIENT = BASE_DIR / "github_client.sh"

# Where built repos are published. Required — no sensible default.
BUILD_GITHUB_ORG = os.environ.get("BUILD_GITHUB_ORG", "").strip()
# git author identity for the initial commit on each built repo.
BUILD_GIT_USER_NAME = os.environ.get("BUILD_GIT_USER_NAME", "Agent Builder")
BUILD_GIT_USER_EMAIL = os.environ.get("BUILD_GIT_USER_EMAIL", "agent@example.com")

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
# Default to the strongest reasoning tier; override via env if you need lower cost.
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_EFFORT = os.environ.get("CODEX_EFFORT", "xhigh")
TASK_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT", str(6 * 3600)))     # 6 hours
POLL_INTERVAL = int(os.environ.get("BUILD_POLL_INTERVAL", "10"))       # 10 seconds

TG_TOKEN_FILE = HOME / ".claude/.ln-bot-token"
TG_BOT_TOKEN = TG_TOKEN_FILE.read_text().strip() if TG_TOKEN_FILE.exists() else ""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("benthic-builder")

BUILD_ROOT.mkdir(parents=True, exist_ok=True)
LOG_ROOT.mkdir(parents=True, exist_ok=True)

# ─── DB helpers ──────────────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_FILE), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def ensure_table() -> None:
    """Create build_tasks if it doesn't exist. The bot's _ensure_chat_table also
    creates this, but the daemon needs to be standalone-runnable without bot
    presence (e.g. fresh deployment, or builder starts before bot)."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS build_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_by INTEGER NOT NULL DEFAULT 0,
            chat_id INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            request_text TEXT,
            brief TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            pid INTEGER,
            work_dir TEXT,
            log_path TEXT,
            repo_url TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL
        )""")
        c.commit()


def claim_next_task() -> dict | None:
    """Atomically pick the oldest pending task and mark it running. Returns the row dict.
    The dict is enriched with the new workdir/log_path/status/started_at values that
    were just written — sqlite3.Row holds a snapshot from before the UPDATE."""
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM build_tasks WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        now = datetime.now(timezone.utc).isoformat()
        workdir = BUILD_ROOT / str(row["id"])
        log_path = LOG_ROOT / f"{row['id']}.log"
        c.execute(
            """UPDATE build_tasks
               SET status='running', started_at=?, work_dir=?, log_path=?
               WHERE id = ?""",
            (now, str(workdir), str(log_path), row["id"]),
        )
        c.commit()
        result = dict(row)
        result["status"] = "running"
        result["started_at"] = now
        result["work_dir"] = str(workdir)
        result["log_path"] = str(log_path)
        return result


def record_pid(task_id: int, pid: int) -> None:
    with db() as c:
        c.execute("UPDATE build_tasks SET pid=? WHERE id = ?", (pid, task_id))
        c.commit()


def finish_task(task_id: int, status: str, repo_url: str | None = None,
                error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute(
            """UPDATE build_tasks
               SET status=?, finished_at=?, repo_url=?, error=?
               WHERE id = ?""",
            (status, now, repo_url, error, task_id),
        )
        c.commit()


def sweep_orphans() -> None:
    """On startup, mark 'running' rows whose subprocess is gone as 'failed'."""
    with db() as c:
        rows = c.execute("SELECT id, pid FROM build_tasks WHERE status = 'running'").fetchall()
    for r in rows:
        pid = r["pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True  # exists but owned by someone else
        if not alive:
            log.warning("Sweeping orphan task #%s (pid %s gone)", r["id"], pid)
            finish_task(r["id"], "failed", error="builder restarted; subprocess gone")


# ─── Telegram ────────────────────────────────────────────────────────────────

def tg_send(chat_id: int, text: str, reply_to: int | None = None) -> None:
    if not TG_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        log.warning("tg_send HTTP %s: %s", e.code, e.read()[:300])
    except Exception as e:
        log.warning("tg_send failed: %s", e)


# ─── Codex prompt template ──────────────────────────────────────────────────

PROMPT = """You are an autonomous senior engineer shipping a project on behalf of the operator.
You have full write access in your workdir and full network. Be calm, methodical, and
finish without asking for permission. Aim for a working MVP, not perfection.

## PROJECT BRIEF (verbatim from the operator)

{brief}

## ENVIRONMENT

- Working directory: {workdir}  (already empty, this is your sandbox)
- Repo name: {repo_name}  (will publish as {github_org}/{repo_name})
- GitHub wrapper: {github_client}
  Use `--operator` for all calls. The wrapper handles auth and credential setup.
- DO NOT touch anything outside the working directory except the GitHub remote.
- DO NOT modify ~/.claude/, ~/server/, or any system path.
- DO NOT print or commit secrets, API keys, or wallet keys.
- The repo will be PUBLIC. Assume everything you push is world-readable.

## DELIVERABLES (in order)

1. Build the project under {workdir}
   - Working code: a runnable main entry point or CLI
   - Comprehensive README.md (setup, usage, architecture, brief excerpt)
   - .gitignore appropriate for the chosen language
   - Dependency manifest (requirements.txt / package.json / etc.)
   - Use .env.example for configuration; never commit real values
2. Initialize git in {workdir}
   - `git init`
   - `git -C {workdir} branch -M main`
   - `git -C {workdir} add .`
   - `git -C {workdir} -c user.name='{git_user_name}' -c user.email='{git_user_email}' commit -m 'Initial implementation: {repo_name}'`
3. Create and push the GitHub repo:
   - `{github_client} --operator repo push {repo_name} {workdir} --description "<one-line summary>"`
   The wrapper prints the canonical https URL on its last line.
4. Write the repo URL to {workdir}/RESULT.txt on its own line.
5. If anything fails unrecoverably, write a short reason to {workdir}/ERROR.txt
   (one paragraph) and exit non-zero.

## SCOPE GUARDRAILS

- Time budget: {timeout_hours}h hard limit. Real target: under 90 minutes.
- Prefer Python or Node, whichever the brief implies. Don't introduce new languages.
- Keep external dependencies minimal. Use the standard library when reasonable.
- When the brief lists features, ship a working minimum of each — depth over breadth.

When RESULT.txt contains the public repo URL, your job is done.
"""


# ─── Task runner ────────────────────────────────────────────────────────────

def build_env() -> dict:
    """Environment for the codex subprocess.
    Adds Node/Codex/gh paths but does NOT inject GitHub tokens — those live inside
    github_client.sh and are read on-demand.
    """
    env = os.environ.copy()
    extra_paths = [
        str(HOME / ".nvm/versions/node/v24.14.1/bin"),
        str(HOME / ".local/bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    env["PATH"] = ":".join([p for p in [env.get("PATH", ""), *extra_paths] if p])
    env["HOME"] = str(HOME)
    return env


def parse_result(workdir: Path) -> tuple[str | None, str | None]:
    """Return (repo_url, error) by reading RESULT.txt / ERROR.txt in workdir."""
    repo_url = None
    error = None
    result_file = workdir / "RESULT.txt"
    error_file = workdir / "ERROR.txt"
    if result_file.exists():
        text = result_file.read_text().strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                repo_url = line
                break
    if error_file.exists():
        error = error_file.read_text().strip()[:1500]
    return repo_url, error


def run_task(task: dict) -> None:
    tid = task["id"]
    workdir = Path(task["work_dir"])
    log_path = Path(task["log_path"])
    workdir.mkdir(parents=True, exist_ok=True)

    prompt = PROMPT.format(
        brief=task["brief"],
        workdir=str(workdir),
        repo_name=task["repo_name"],
        github_client=str(GITHUB_CLIENT),
        github_org=BUILD_GITHUB_ORG,
        git_user_name=BUILD_GIT_USER_NAME,
        git_user_email=BUILD_GIT_USER_EMAIL,
        timeout_hours=TASK_TIMEOUT // 3600,
    )

    log.info("Starting build #%s: %s", tid, task["repo_name"])
    log.info("  workdir: %s", workdir)
    log.info("  log:     %s", log_path)

    # Codex CLI takes reasoning effort via -c (config override) — there is no --effort flag.
    cmd = [
        CODEX_BIN, "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        "-m", CODEX_MODEL,
        "-c", f"model_reasoning_effort={CODEX_EFFORT}",
        "-",
    ]

    error: str | None = None
    started_at = time.time()

    with open(log_path, "w") as logfile:
        logfile.write(f"=== build_tasks #{tid} starting {datetime.now(timezone.utc).isoformat()} ===\n")
        logfile.write(f"=== repo_name: {task['repo_name']} ===\n")
        logfile.write(f"=== workdir:   {workdir} ===\n")
        logfile.write(f"=== timeout:   {TASK_TIMEOUT}s ===\n")
        logfile.write(f"=== brief len: {len(task['brief'])} chars ===\n")
        logfile.write(f"=== cmd:       {' '.join(cmd)} ===\n\n")
        logfile.flush()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                env=build_env(),
                cwd=str(workdir),
                text=True,
            )
            record_pid(tid, proc.pid)
            log.info("  pid:     %s", proc.pid)
            try:
                proc.communicate(input=prompt, timeout=TASK_TIMEOUT)
            except subprocess.TimeoutExpired:
                elapsed = int(time.time() - started_at)
                log.warning("#%s timed out after %ss — sending SIGTERM", tid, elapsed)
                logfile.write(f"\n=== TIMEOUT after {elapsed}s; sending SIGTERM ===\n")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                error = f"hard timeout after {elapsed}s"
        except FileNotFoundError as e:
            error = f"codex binary not found: {e}"
            log.error("#%s setup failed: %s", tid, error)
        except Exception as e:
            error = f"subprocess setup failed: {e}"
            log.exception("#%s setup failed", tid)

        elapsed = int(time.time() - started_at)
        logfile.write(f"\n=== finished at {datetime.now(timezone.utc).isoformat()} (elapsed: {elapsed}s) ===\n")

    repo_url, parsed_error = parse_result(workdir)
    if not error and parsed_error:
        error = parsed_error

    if repo_url and not error:
        finish_task(tid, "done", repo_url=repo_url)
        notify_done(task, repo_url)
        log.info("#%s DONE: %s", tid, repo_url)
    else:
        if not error:
            error = "no RESULT.txt produced; check log"
        finish_task(tid, "failed", error=error)
        notify_failed(task, error, log_path)
        log.warning("#%s FAILED: %s", tid, error)


def notify_done(task: dict, repo_url: str) -> None:
    text = (
        f"✅ Build #{task['id']} done — {task['repo_name']}\n"
        f"Repo: {repo_url}\n\n"
        "Operator: sign + POST submit when ready (signature for the relevant bounty, "
        "or skip if this wasn't a bounty)."
    )
    tg_send(task["chat_id"], text, reply_to=task.get("message_id"))


def notify_failed(task: dict, error: str, log_path: Path) -> None:
    text = (
        f"❌ Build #{task['id']} failed — {task['repo_name']}\n"
        f"Reason: {error[:500]}\n"
        f"Log: {log_path}"
    )
    tg_send(task["chat_id"], text, reply_to=task.get("message_id"))


# ─── Main loop ──────────────────────────────────────────────────────────────

def main() -> None:
    if not BUILD_GITHUB_ORG:
        sys.exit("ERROR: BUILD_GITHUB_ORG env var is required — set it to the "
                 "GitHub org/user repos should be published under.")
    log.info("builder starting")
    log.info("  DB:            %s", DB_FILE)
    log.info("  build root:    %s", BUILD_ROOT)
    log.info("  log root:      %s", LOG_ROOT)
    log.info("  poll interval: %ss", POLL_INTERVAL)
    log.info("  task timeout:  %ss (%sh)", TASK_TIMEOUT, TASK_TIMEOUT // 3600)
    log.info("  codex:         %s (model: %s, effort: %s)", CODEX_BIN, CODEX_MODEL, CODEX_EFFORT)
    log.info("  github org:    %s", BUILD_GITHUB_ORG)
    log.info("  git author:    %s <%s>", BUILD_GIT_USER_NAME, BUILD_GIT_USER_EMAIL)
    ensure_table()
    sweep_orphans()

    while True:
        try:
            task = claim_next_task()
            if task:
                run_task(task)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("shutting down (KeyboardInterrupt)")
            break
        except Exception:
            log.exception("unexpected error in main loop")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
