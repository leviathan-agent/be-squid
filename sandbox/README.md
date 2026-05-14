# Agent Sandbox

Isolated Docker environment for the agent code execution.

## Setup

```bash
# 1. Build the image
docker build -t agent-sandbox sandbox/

# 2. Set up the network with allowlisted hosts
sudo sandbox/setup-network.sh

# 3. Test it
sandbox/run-sandbox.sh "print('hello from sandbox')"
sandbox/run-sandbox.sh "from web3 import Web3; print(Web3.is_address('0x0000000000000000000000000000000000000000'))"
```

## What's inside

Python 3.12 with: web3, requests, pandas, matplotlib, eth-abi

## What's NOT inside

- No wallet key, bot token, or Telegram session
- No curl, wget, or git
- No access to agent.db or host filesystem

## Network

Outbound restricted to RPCs, block explorers, and data APIs only.
Edit `allowed-hosts.txt` and re-run `sudo setup-network.sh` to add endpoints.

## Resource limits

- 512MB RAM, 1 CPU, 120s timeout, 100MB tmpfs scratch
- Read-only filesystem, non-root user, no-new-privileges
- PID limit: 64 (fork bomb protection)

## Troubleshooting

If sandbox API calls fail, allowlisted IPs may have rotated (CDN/failover).
Re-run `sudo setup-network.sh` to refresh DNS resolutions.
