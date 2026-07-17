#!/bin/bash
# be-squid launcher — sources .env then execs the agent under the venv.
# Called by launchd (com.leviathan.be-squid.plist); also fine to run by hand.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="/opt/homebrew/bin:/usr/bin:/bin"

if [ ! -f .env ]; then
    echo "FATAL: .env missing (copy .env.squid.example and fill in)" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1091
source ./.env
set +a

mkdir -p "$HOME/logs"
exec .venv/bin/python ln-agent.py
