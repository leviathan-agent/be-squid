# be-benthic 🦑

Automated news curation agent for [Leviathan News](https://leviathannews.xyz) — a decentralized crypto/DeFi news platform where contributors earn $SQUID tokens.

be-benthic monitors Telegram news channels, evaluates newsworthiness via Claude CLI (with Codex fallback), crafts headlines, submits articles, votes, comments, and replies — all autonomously in a continuous loop.

## How It Works

```
Phase 1       Phase 2            Phase 3       Phase 4          Phase 5
───────       ───────            ───────       ───────          ───────
Telegram  →   Evaluate &    →   Post to   →   Vote &       →   Reply to
Channels      Deduplicate       LN API        Comment          Responses
  │               │                │              │                │
  ├─ Fetch new    ├─ Batch LLM     ├─ Resolve     ├─ Eval quality  ├─ Scan old
  │  messages     │  scoring       │  primary URL  ├─ Upvote/down   │  comments
  ├─ Cursor-      ├─ Story-level   ├─ Check dupes  ├─ Write         ├─ Detect
  │  based        │  dedup         │  (DB+Bot HQ)  │  analysis      │  unreplied
  │  pagination   ├─ Twitter/X     ├─ Craft        ├─ Vote on       ├─ Craft
  └─ Cache IDs    │  verification  │  headline     │  others' yaps  │  replies
                  └─ JSON output   ├─ Submit       └─ Self-upvote   └─ Sentinel
                                   ├─ Self-upvote                      verify
                                   └─ TL;DR comment
```

## Features

- **Configurable multi-provider LLM**: Set `PROVIDER_ORDER` to choose your provider priority (Claude, Codex, OpenCode). Circuit breaker auto-switches after 3 failures or quota errors
- **6-layer prompt injection defense**: input sanitization, XML boundary tags, output injection detection, NFKD Unicode normalization, URL validation, independent Sonnet sentinel verification on replies
- **Wallet-based auth**: EIP-191 signature flow with thread-safe session management (30-min refresh, RLock)
- **Headline validation**: 10 automated checks (character count, sentence case, passive voice, articles, etc.) via bundled bash validator
- **Story deduplication**: multi-layer — LLM-powered story matching, local DB URL check, Bot HQ Telegram search, LN API check
- **Anti-AI-detection**: banned phrase filtering to avoid patterns that get content deprioritized
- **Cursor-based Telegram pagination**: with numeric ID caching to avoid flood waits
- **Token optimization**: tiered LLM calls — Sonnet/low for classification, Opus/max for creative tasks

## Current Limitations

- **Twitter/X script not included**: The agent uses a Twitter/X fetch script for research context, but the bundled implementation uses cookie-based access to X's internal API which raises ToS concerns. You must provide your own `scripts/twitter_fetch.py` or set `TWITTER_FETCH_SCRIPT` to an alternative. See [Twitter/X Integration](#twitterx-integration) below.
- **No dotenv auto-loading**: The agent reads `os.environ` directly. You must export environment variables manually or configure them via your process manager (see [.env.example](.env.example)).

## Prerequisites

### Python Packages

Installed via `pip install -r requirements.txt`:
- [telethon](https://docs.telethon.dev/) — Telegram client
- [requests](https://requests.readthedocs.io/) — HTTP client
- [eth-account](https://eth-account.readthedocs.io/) — Ethereum wallet signing
- [Pillow](https://pillow.readthedocs.io/) — image processing (media analysis in chat bot)

### External Runtime Dependencies

You need **at least one** LLM provider. Configure the priority via `PROVIDER_ORDER`:

- **[Claude CLI](https://docs.anthropic.com/en/docs/claude-code)** (`claude`) — primary by default. Must be on `$PATH` or set `CLAUDE_BIN`.
- **[Codex CLI](https://github.com/openai/codex)** (optional) — fallback LLM provider. Auto-detected or set `CODEX_BIN`.
- **[OpenCode CLI](https://opencode.ai/)** (optional) — alternative provider. Set `OPENCODE_BIN` and `OPENCODE_MODEL` to enable.
- **Telegram API credentials** — `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org)
- **Ethereum wallet** — private key for LN API authentication (EIP-191 signing)
- **Leviathan News account** — wallet must be registered at [leviathannews.xyz](https://leviathannews.xyz)
- **Twitter/X access** (optional) — your own script for Twitter research context (see [Twitter/X Integration](#twitterx-integration))

## Quick Start

```bash
# 1. Clone
git clone https://github.com/leviathan-news/be-benthic.git
cd be-benthic

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up Telegram credentials
# Create a JSON file with your Telegram API credentials:
cat > ~/.claude/telegram-creds.json << 'EOF'
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here"
}
EOF

# 5. Set up wallet key
echo "your_private_key_hex" > ~/.claude/.ln-wallet-key

# 6. Export required environment variables
export BOT_HQ_GROUP_ID="-100..."  # Your Bot HQ Telegram group ID
export CHANNELS='["@examplechannel", "@anotherchannel"]'  # Your Telegram news channels

# 7. Run (single cycle for testing)
python3 ln-agent.py

# Or run via PM2 for continuous operation (see Deployment section)
```

On first run, Telethon will prompt for your Telegram phone number and verification code to create a session file.

## Configuration

The agent reads all config from `os.environ`. There is no dotenv auto-loading — export variables before running or configure them in your process manager.

See [.env.example](.env.example) for a complete template with `export` lines you can source directly.

| Variable | Default | Description |
|----------|---------|-------------|
| `LN_API` | `https://api.leviathannews.xyz/api/v1` | Leviathan News API base URL |
| `TELEGRAM_SESSION` | `~/.claude/agent_session.session` | Telethon session file path |
| `TELEGRAM_CREDS_FILE` | `~/.claude/telegram-creds.json` | Telegram API credentials (JSON with `api_id`, `api_hash`) |
| `WALLET_PRIVATE_KEY` | — | ETH wallet private key (takes priority over file) |
| `WALLET_KEY_FILE` | `~/.claude/.ln-wallet-key` | Fallback: read wallet key from this file |
| `BOT_HQ_GROUP_ID` | **(required)** | Telegram group ID used as ground truth for duplicate detection |
| `CLAUDE_BIN` | auto-detected | Path to Claude CLI binary |
| `CODEX_BIN` | auto-detected | Path to Codex CLI binary |
| `CODEX_MODEL` | `gpt-5.4` | Model for Codex |
| `OPENCODE_BIN` | auto-detected | Path to OpenCode CLI binary |
| `OPENCODE_MODEL` | — (disabled) | OpenCode model (e.g. `anthropic/claude-sonnet-4-5`). Required to enable. |
| `PROVIDER_ORDER` | `claude,codex,opencode` | Comma-separated provider priority (first available is primary) |
| `CLAUDE_LIMIT_COOLDOWN` | `21600` (6h) | Seconds to skip Claude after quota/rate-limit error |
| `CHANNELS` | `[]` | JSON array of Telegram channels, e.g. `'["@chan1","@chan2"]'` |
| `PRIVATE_CHANNELS` | `[]` | JSON array of private channel display names |
| `CYCLE_INTERVAL` | `3600` (1h) | Seconds between cycles |
| `INITIAL_LOOKBACK_HOURS` | `1` | Hours to look back on first run |
| `TELEGRAM_CLIENT_SCRIPT` | `skills/telegram-explorer/scripts/telegram_client.py` | Path to Telegram CLI wrapper |
| `TELEGRAM_CLIENT_PYTHON` | `.venv/bin/python3` | Python interpreter for Telegram script |
| `TWITTER_FETCH_SCRIPT` | `scripts/twitter_fetch.py` | Path to Twitter/X script (not bundled) |
| `ALERT_CHANNEL_ID` | — (disabled) | Telegram chat ID for cycle summary alerts |

## Headline Skill

The `skills/leviathan-headlines/` directory is a Claude Code plugin for manual headline crafting and review. It includes:

- **[SKILL.md](skills/leviathan-headlines/SKILL.md)** — 11 non-negotiable editorial rules, writing workflow, tone guide
- **[validate-headline.sh](skills/leviathan-headlines/scripts/validate-headline.sh)** — automated 10-check validator
- **Reference docs** — [style guide](skills/leviathan-headlines/references/style-guide.md), [examples](skills/leviathan-headlines/references/examples.md), [terminology](skills/leviathan-headlines/references/ethereum-terminology.md)

```bash
# Validate a headline
./skills/leviathan-headlines/scripts/validate-headline.sh "Uniswap v4 hooks unlock custom pool logic, opening AMM design to third-party developers"
```

## Telegram Client

A Telethon-based CLI wrapper is bundled at [`skills/telegram-explorer/scripts/telegram_client.py`](skills/telegram-explorer/scripts/telegram_client.py). It provides JSON-output commands for all Telegram operations the agent needs:

```bash
# List recent messages from a channel
.venv/bin/python3 skills/telegram-explorer/scripts/telegram_client.py messages -1001234567890 --limit 5

# Search messages
.venv/bin/python3 skills/telegram-explorer/scripts/telegram_client.py messages -1001234567890 --search "Ethereum"

# Send a message
.venv/bin/python3 skills/telegram-explorer/scripts/telegram_client.py send -1001234567890 --text "Hello"
```

Available commands: `messages`, `send`, `reply`, `forward`, `edit`, `delete`, `react`, `info`, `participants`, `dialogs`, `topics`, `search-global`, `buttons`, `click`, `pinned`, `download`, `upload`.

Requires Telegram API credentials in `~/.claude/telegram-creds.json` (see [Quick Start](#quick-start)).

## Twitter/X Integration

The agent uses a Twitter/X script during Phases 2-3 to search for context, verify stories, and find primary source tweets. **This script is not bundled** because common approaches (cookie-based access to X's internal GraphQL API) raise Terms of Service concerns.

To enable Twitter research, provide your own `scripts/twitter_fetch.py` or set `TWITTER_FETCH_SCRIPT` to point to an alternative. Your script should support:

```bash
# Search for recent tweets
your_script.py search --query "Ethereum ETF" --limit 5

# Fetch a user's recent tweets
your_script.py user --username "VitalikButerin" --limit 10
```

Output should be JSON with at minimum: tweet text, author handle, tweet URL, and timestamp.

**Options for implementation:**
- **X API v2** (official) — requires a developer account and API key. Most compliant approach.
- **Nitter instances** — open-source Twitter frontend with RSS feeds. Availability varies.
- **Skip entirely** — a no-op stub is bundled at `scripts/twitter_fetch.py` that returns `[]`. The agent works without Twitter; it just won't have X context for headline crafting and story verification.

## Security Model

The agent processes untrusted input (Telegram messages, user comments) and produces public-facing output. Six defense layers prevent prompt injection:

1. **`sanitize_untrusted()`** — strips control characters, replaces `<>` with fullwidth equivalents (prevents XML boundary injection), collapses separator patterns, truncates
2. **`<user_content>` tags** — all untrusted text wrapped with explicit "treat as DATA" warnings in every LLM prompt
3. **`check_output_for_injection()`** — validates output for signs of successful injection (secret leakage, "ignore previous instructions", AI self-identification)
4. **NFKD Unicode normalization** — defeats homoglyph bypass attempts in injection detection
5. **`validate_url()`** — rejects control characters, non-HTTP schemes, oversized URLs in LLM-returned content
6. **Sentinel verification** — independent Sonnet-based semantic check on reply output before posting (different model for cross-verification)

Fail-safe defaults: freshness checks fail open (article already passed evaluation), duplicate checks fail closed (unknown = reject).

## Database

Agent state lives in `agent.db` (SQLite, WAL mode). Tables: `channel_cursors`, `channel_ids`, `evaluated_messages`, `posted_articles`, `commented_articles`, `voted_articles`, `voted_yaps`, `replied_yaps`, `runs`, `chat_history`, `notes`, `own_actions`.

```bash
# Inspect counts
.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('agent.db')
for t in ['evaluated_messages', 'posted_articles', 'commented_articles',
          'voted_articles', 'voted_yaps', 'replied_yaps', 'runs', 'channel_ids',
          'chat_history', 'notes', 'own_actions']:
    c = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {c}')
conn.close()
"
```

## Deployment (PM2)

For continuous operation, use PM2 with the included config:

```bash
# Start
pm2 start ecosystem.config.js

# Monitor
pm2 logs ln-agent
pm2 monit

# Restart after config changes
pm2 restart ln-agent
```

The agent runs in a continuous loop — PM2's `autorestart` handles crashes, and the internal loop sleeps `CYCLE_INTERVAL` seconds between cycles. No `cron_restart` is used to avoid killing long-running cycles mid-work.

## Project Structure

```
be-benthic/
├── ln-agent.py              # Main agent (single-file)
├── benthic-bot.py           # Telegram chat bot (bot-to-bot communication)
├── scripts/
│   └── twitter_fetch.py     # No-op stub (replace with your own)
├── skills/
│   ├── telegram-explorer/
│   │   ├── SKILL.md          # Telegram skill definition
│   │   ├── scripts/
│   │   │   └── telegram_client.py  # Telethon CLI wrapper (JSON output)
│   │   └── references/
│   │       └── api-reference.md    # Full subcommand reference
│   └── leviathan-headlines/
│       ├── SKILL.md          # Headline crafting skill definition
│       ├── references/
│       │   ├── style-guide.md
│       │   ├── examples.md
│       │   └── ethereum-terminology.md
│       └── scripts/
│           └── validate-headline.sh
├── tests/                   # pytest test suite (46 tests)
├── ecosystem.config.js      # PM2 deployment config
├── requirements.txt         # Python dependencies
├── requirements-dev.txt     # Dev dependencies (pytest)
├── .env.example             # Environment variable template
├── CLAUDE.md                # Claude Code context (architecture, conventions)
├── AGENTS.md                # Agent operational state notes
└── .claude-plugin/
    └── plugin.json          # Claude plugin metadata
```

## Running Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

46 tests covering security-critical functions: `sanitize_untrusted()`, `check_output_for_injection()`, `validate_url()`, `has_llm_preamble()`, `validate-headline.sh`, and `AgentDB` constraints.

## Chat Bot

`benthic-bot.py` is a Telegram chat bot that shares brain and memory with `ln-agent.py`. It uses Telegram's [bot-to-bot communication](https://core.telegram.org/bots/features#bot-to-bot-communication) to participate in group chats with other bots and humans.

### Features

- **Per-chat memory isolation** — each group gets its own conversation context; no cross-group leaking
- **Operator tiered auth** — operators (by Telegram user ID) get expanded tool access for diagnostics; everyone else gets read-only research tools
- **[GROUP] directive** — operators can route messages from DMs to the group chat, with multi-command support (each line sent as a separate message)
- **Agent-chat API polling** — catches messages Telegram doesn't deliver via the LN agent-chat API (60s interval, content-based dedup)
- **Message debouncing** — merges consecutive rapid messages from same sender before LLM processing
- **Media analysis** — photos re-encoded via PIL (strips EXIF/metadata); PDFs blocked for security; text files sanitized
- **Persistent memory** — `[REMEMBER:category]` / `[FORGET:id]` directives for notes that survive restarts (operator-only writes, auto-prunes at 200)
- **Self-awareness** — tracks own actions (bets, messages, replies) so it knows what IT did
- **Configurable providers** — same `PROVIDER_ORDER` support as ln-agent.py

### Setup

1. **Create a bot** via [@BotFather](https://t.me/BotFather) on Telegram
2. **Enable bot-to-bot communication** in BotFather → Bot Settings → Bot-to-Bot Communication
3. **Disable privacy mode** in BotFather → Bot Settings → Group Privacy → Turn OFF. **This is critical** — with privacy mode on, your bot will be invisible to other bots
4. **Add the bot** to your Telegram group
5. **Store the token** securely:
   ```bash
   echo "YOUR_BOT_TOKEN" > ~/.claude/.ln-bot-token
   chmod 600 ~/.claude/.ln-bot-token
   ```

### Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | Yes (or `BOT_TOKEN_FILE`) | Telegram bot token |
| `BOT_TOKEN_FILE` | No | Path to file containing bot token (default: `~/.claude/.ln-bot-token`) |
| `BOT_USERNAME` | Yes | Bot username in lowercase, without @ (e.g. `my_bot`) |
| `AGENTS_GROUP_ID` | Yes | Telegram group ID where the bot operates (prefix channels with `-100`) |
| `ALLOWED_GROUPS` | No | JSON array of additional group IDs: `'[-100123456789]'` |
| `OPERATOR_IDS` | No | JSON array of Telegram user IDs: `'[12345678]'` |
| `AGENT_DIR` | No | Agent directory for operator Bash tool restrictions (default: script directory) |
| `PROVIDER_ORDER` | No | Comma-separated provider priority (default: `claude,codex,opencode`) |
| `OPENCODE_MODEL` | No | OpenCode model to enable it (e.g. `anthropic/claude-sonnet-4-5`) |

### Running

```bash
# Export required variables
export BOT_USERNAME="my_bot"
export AGENTS_GROUP_ID="-100XXXXXXXXXX"

# Optional: set operator and provider
export OPERATOR_IDS='[YOUR_TELEGRAM_USER_ID]'
export PROVIDER_ORDER=opencode,codex  # if you don't have Claude CLI

# Run directly
python3 benthic-bot.py

# Or via PM2 for continuous operation
pm2 start ecosystem.config.js
```

### How it works

- **Direct mentions** (`@my_bot what do you think?`) or **replies** to the bot's messages → always responds
- **Ambient messages** in the group → the LLM decides if it has something useful to add (otherwise silently skips)
- **Operator DMs** → operators get full cross-group context with `[GroupName]` headers; can use `[GROUP]` to send messages to the group
- **Media messages** → photos analyzed via PIL (re-encoded for safety); documents noted in context; PDFs blocked
- **Shared memory** — reads from the same `agent.db` as `ln-agent.py`
- **Loop prevention** — rate limit (5s per sender), max thread depth (5), message dedup, content-based cross-path dedup

### Customizing personality

Set `BOT_IDENTITY` to a custom prompt, or edit the `BENTHIC_IDENTITY` string in the source. Leave unset for the default crypto analyst personality.

## Contributing

For AI-assisted development, see [CLAUDE.md](CLAUDE.md) — it contains detailed architecture documentation, class references, and conventions optimized for Claude Code.

## License

[MIT](LICENSE)
