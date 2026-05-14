"""Agent Sandbox Helpers — pre-built patterns for common onchain and data queries.

Usage from sandbox:
    from helpers import get_web3, get_chain, explorer, defi_llama, coingecko
    w3 = get_web3("fraxtal")
    holders = explorer("fraxtal").token_holders("0x6e58...")
    tvl = defi_llama.protocol_tvl("aave-v3")
"""

import json
import os
from pathlib import Path
from typing import Any

import requests
from web3 import Web3

# ─── Chain Config ─────────────────────────────────────────────────────────────

_CHAINS_FILE = Path(__file__).parent / "chains.json"
_CHAINS: dict = {}

def _load_chains() -> dict:
    """Load chain config from chains.json (cached after first call)."""
    global _CHAINS
    if not _CHAINS:
        _CHAINS = json.loads(_CHAINS_FILE.read_text())
    return _CHAINS

def get_chain(name: str) -> dict:
    """Get chain config by name or alias. Raises KeyError if not found."""
    chains = _load_chains()
    name_lower = name.lower().strip()
    # Direct match
    if name_lower in chains:
        return chains[name_lower]
    # Alias match
    for chain_name, cfg in chains.items():
        if name_lower in cfg.get("aliases", []):
            return cfg
    raise KeyError(f"Unknown chain: {name}. Available: {', '.join(chains.keys())}")

def list_chains() -> list[str]:
    """List all available chain names."""
    return list(_load_chains().keys())

# ─── Web3 ─────────────────────────────────────────────────────────────────────

def get_web3(chain: str = "ethereum") -> Web3:
    """Get a connected Web3 instance for a chain. Tries RPCs in order."""
    cfg = get_chain(chain)
    for rpc in cfg["rpcs"]:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        try:
            w3.eth.block_number  # test connection
            return w3
        except Exception:
            continue
    raise ConnectionError(f"All RPCs failed for {chain}: {cfg['rpcs']}")

def get_token_address(chain: str, symbol: str) -> str:
    """Look up a known token address by symbol. Raises KeyError if not found."""
    cfg = get_chain(chain)
    tokens = cfg.get("tokens", {})
    symbol_upper = symbol.upper()
    if symbol_upper in tokens:
        entry = tokens[symbol_upper]
        return entry["address"] if isinstance(entry, dict) else entry
    raise KeyError(f"Token {symbol} not in chains.json for {chain}. Known: {list(tokens.keys())}")

def get_token_deploy_block(chain: str, symbol: str) -> int | None:
    """Look up a known token's deploy block. Returns None if not configured."""
    cfg = get_chain(chain)
    entry = cfg.get("tokens", {}).get(symbol.upper())
    if isinstance(entry, dict):
        return entry.get("deploy_block")
    return None

# ─── ERC-20 Helpers ───────────────────────────────────────────────────────────

# Minimal ERC-20 ABI for common reads
ERC20_ABI = json.loads('[{"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},{"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"type":"function"},{"constant":true,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]')

def token_info(chain: str, address: str) -> dict:
    """Get token name, symbol, decimals, totalSupply."""
    w3 = get_web3(chain)
    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)
    decimals = contract.functions.decimals().call()
    total = contract.functions.totalSupply().call()
    return {
        "name": contract.functions.name().call(),
        "symbol": contract.functions.symbol().call(),
        "decimals": decimals,
        "total_supply": total / (10 ** decimals),
        "total_supply_raw": total,
    }

def token_balance(chain: str, token_address: str, wallet: str) -> float:
    """Get a wallet's token balance (human-readable)."""
    w3 = get_web3(chain)
    contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    decimals = contract.functions.decimals().call()
    raw = contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call()
    return raw / (10 ** decimals)

def eth_balance(chain: str, wallet: str) -> float:
    """Get native token balance in ETH/frxETH/etc."""
    w3 = get_web3(chain)
    raw = w3.eth.get_balance(Web3.to_checksum_address(wallet))
    return raw / 1e18

def _find_contract_deploy_block(w3: Web3, address: str) -> int:
    """Binary search for the block where a contract was deployed."""
    lo, hi = 0, w3.eth.block_number
    addr = Web3.to_checksum_address(address)
    while lo < hi:
        mid = (lo + hi) // 2
        code = w3.eth.get_code(addr, block_identifier=mid)
        if len(code) > 0:
            hi = mid
        else:
            lo = mid + 1
    return lo


def token_holders_rpc(chain: str, contract: str, limit: int = 10,
                      batch_size: int = 50000, deploy_block: int | None = None) -> list[dict]:
    """Get top token holders by scanning Transfer events via RPC.
    If deploy_block is not provided, checks chains.json first, then binary searches.
    Uses large batch sizes and auto-halves if RPC rejects the range."""
    w3 = get_web3(chain)
    addr = Web3.to_checksum_address(contract)
    ct = w3.eth.contract(address=addr, abi=ERC20_ABI)
    decimals = ct.functions.decimals().call()
    transfer_topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    latest = w3.eth.block_number
    # Use provided deploy_block, or look up in chains.json, or binary search as last resort
    if deploy_block is None:
        # Check chains.json for known tokens
        cfg = get_chain(chain)
        for sym, entry in cfg.get("tokens", {}).items():
            if isinstance(entry, dict) and entry.get("address", "").lower() == contract.lower():
                deploy_block = entry.get("deploy_block")
                break
    if deploy_block is None:
        deploy_block = _find_contract_deploy_block(w3, addr)
    print(f"Contract deployed at block {deploy_block}, scanning {latest - deploy_block} blocks...")
    balances: dict[str, int] = {}
    start = deploy_block
    while start <= latest:
        end = min(start + batch_size, latest)
        try:
            logs = w3.eth.get_logs({
                "address": addr,
                "topics": [transfer_topic],
                "fromBlock": start,
                "toBlock": end,
            })
        except Exception as e:
            # RPC rejected range — halve batch size and retry
            print(f"Warning: RPC error at blocks {start}-{end}: {type(e).__name__}: {e}")
            if batch_size > 1000:
                batch_size = batch_size // 2
                continue
            break
        for log_entry in logs:
            from_addr = "0x" + log_entry["topics"][1].hex()[-40:]
            to_addr = "0x" + log_entry["topics"][2].hex()[-40:]
            value = int(log_entry["data"].hex(), 16)
            balances[from_addr] = balances.get(from_addr, 0) - value
            balances[to_addr] = balances.get(to_addr, 0) + value
        start = end + 1
    # Sort and format
    total_positive = sum(v for v in balances.values() if v > 0)
    holders = []
    for a, raw in sorted(balances.items(), key=lambda x: x[1], reverse=True):
        if raw <= 0:
            continue
        bal = raw / (10 ** decimals)
        pct = (raw / total_positive * 100) if total_positive > 0 else 0
        holders.append({"address": a, "balance": round(bal, 2), "share_pct": round(pct, 2)})
        if len(holders) >= limit:
            break
    return holders

# ─── Block Explorer API ──────────────────────────────────────────────────────

class ExplorerAPI:
    """Wrapper for Etherscan-compatible block explorer APIs."""

    def __init__(self, chain: str):
        cfg = get_chain(chain)
        self.chain_id = cfg["chain_id"]
        self.chain = chain

    def _get(self, **params) -> Any:
        """Make an explorer API call via Etherscan V2 unified endpoint.
        Uses chainid parameter for cross-chain support."""
        params["chainid"] = self.chain_id
        params["apikey"] = os.environ.get("ETHERSCAN_API_KEY", "")
        resp = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "0" and "No transactions found" not in data.get("message", ""):
            msg = data.get("result", data.get("message", "unknown error"))
            raise RuntimeError(f"Explorer API error: {msg}")
        return data.get("result", [])

    def token_holders(self, contract: str, limit: int = 25, max_pages: int = 50) -> list[dict]:
        """Get top token holders by paginating ALL token transfers and aggregating balances.
        Returns list of {address, balance, share_pct}.

        The free-tier Etherscan V2 `tokenholderlist` action requires Pro access.
        This method works around that by pulling ALL historical Transfer events via
        the free `tokentx` endpoint and computing current balances from the deltas.

        Pagination: 10,000 transfers per page, up to max_pages (default 50 = 500k
        transfers). For tokens with more history, raise max_pages or use
        token_holders_rpc() which scans events directly via eth_getLogs.
        """
        balances: dict[str, int] = {}
        decimals = 18
        for page in range(1, max_pages + 1):
            transfers = self._get(
                module="account", action="tokentx",
                contractaddress=contract,
                startblock=0, endblock=99999999,
                page=page, offset=10000, sort="asc"
            )
            if not isinstance(transfers, list) or not transfers:
                break  # no more pages
            if page == 1:
                decimals = int(transfers[0].get("tokenDecimal", 18))
            for tx in transfers:
                value = int(tx.get("value", 0))
                from_addr = tx.get("from", "").lower()
                to_addr = tx.get("to", "").lower()
                balances[from_addr] = balances.get(from_addr, 0) - value
                balances[to_addr] = balances.get(to_addr, 0) + value
            if len(transfers) < 10000:
                break  # last page (partial)
        # Sort by balance, filter out zero/negative, return top N
        total_positive = sum(v for v in balances.values() if v > 0)
        holders = []
        for addr, raw_bal in sorted(balances.items(), key=lambda x: x[1], reverse=True):
            if raw_bal <= 0:
                continue
            bal = raw_bal / (10 ** decimals)
            pct = (raw_bal / total_positive * 100) if total_positive > 0 else 0
            holders.append({"address": addr, "balance": round(bal, 2), "share_pct": round(pct, 2)})
            if len(holders) >= limit:
                break
        return holders

    def token_transfers(self, contract: str, address: str = None,
                        start_block: int = 0, limit: int = 100) -> list[dict]:
        """Get token transfer events."""
        params = dict(module="account", action="tokentx",
                      contractaddress=contract, startblock=start_block,
                      endblock=99999999, page=1, offset=limit, sort="desc")
        if address:
            params["address"] = address
        return self._get(**params)

    def txlist(self, address: str, start_block: int = 0, limit: int = 50) -> list[dict]:
        """Get normal transactions for an address."""
        return self._get(module="account", action="txlist", address=address,
                         startblock=start_block, endblock=99999999,
                         page=1, offset=limit, sort="desc")

    def contract_abi(self, address: str) -> list:
        """Get verified contract ABI."""
        result = self._get(module="contract", action="getabi", address=address)
        return json.loads(result) if isinstance(result, str) else result

def explorer(chain: str) -> ExplorerAPI:
    """Get an ExplorerAPI instance for a chain."""
    return ExplorerAPI(chain)

# ─── DeFi Llama ──────────────────────────────────────────────────────────────

class DeFiLlama:
    """Wrapper for DeFi Llama API (no auth needed)."""
    BASE = "https://api.llama.fi"

    def _get(self, path: str) -> Any:
        resp = requests.get(f"{self.BASE}{path}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def protocol_tvl(self, slug: str) -> dict:
        """Get protocol TVL and chain breakdown."""
        data = self._get(f"/protocol/{slug}")
        return {
            "name": data.get("name"),
            "tvl": data.get("currentChainTvls", {}),
            "total_tvl": sum(v for k, v in data.get("currentChainTvls", {}).items()
                            if not k.endswith("-borrowed") and not k.endswith("-staking")),
        }

    def chain_tvl(self, chain: str) -> float:
        """Get total TVL for a chain."""
        chains = self._get("/v2/chains")
        for c in chains:
            if c.get("name", "").lower() == chain.lower():
                return c.get("tvl", 0)
        return 0

    def stablecoins(self) -> list[dict]:
        """Get top stablecoins by market cap."""
        data = self._get("/stablecoins")
        result = []
        for s in data.get("peggedAssets", [])[:20]:
            result.append({
                "name": s.get("name"),
                "symbol": s.get("symbol"),
                "mcap": s.get("circulating", {}).get("peggedUSD", 0),
            })
        return result

    def yields(self, pool_id: str = None) -> list[dict]:
        """Get yield pools. If pool_id given, returns that specific pool."""
        if pool_id:
            return self._get(f"/yields/pool/{pool_id}")
        data = self._get("/pools")
        return data.get("data", [])[:50]

defi_llama = DeFiLlama()

# ─── CoinGecko ───────────────────────────────────────────────────────────────

class CoinGecko:
    """Wrapper for CoinGecko API (free tier, no auth)."""
    BASE = "https://api.coingecko.com/api/v3"

    def _get(self, path: str, params: dict = None) -> Any:
        resp = requests.get(f"{self.BASE}{path}", params=params or {}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def price(self, ids: str, vs: str = "usd") -> dict:
        """Get current price. ids: comma-separated CoinGecko IDs."""
        return self._get("/simple/price", {"ids": ids, "vs_currencies": vs,
                                            "include_24hr_change": "true",
                                            "include_market_cap": "true"})

    def search(self, query: str) -> list[dict]:
        """Search for a coin by name/symbol."""
        data = self._get("/search", {"query": query})
        return [{"id": c["id"], "name": c["name"], "symbol": c["symbol"],
                 "market_cap_rank": c.get("market_cap_rank")}
                for c in data.get("coins", [])[:10]]

coingecko = CoinGecko()
