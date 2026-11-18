# ALTAF

ALTAF is a crypto ecosystem with three parts:

| Part | Folder | Stack |
|---|---|---|
| **ALTAF Exchange** — spot, futures, P2P, convert, Earn, Launchpad, Token Factory, listings, copy trading, bots, ALTAF Pay, governance, API keys, WebSocket market data, proof of reserves | `exchange/` | Python (FastAPI, SQLAlchemy), vanilla JS SPA |
| **ALTF token** — TRC20 on TRON, fixed 21,000,000 supply, partner-server locks | `exchange/token/Altaf.sol` | Solidity 0.8.20 |
| **ALTAF Network** — native proof-of-work chain (CPU + SHA-256 lanes), node, wallet, miner, stratum | `chain/` | Go 1.23 |
| **Android app** | `android/` | Java WebView |

- Exchange: https://altaf.187-126-9-68.sslip.io
- Network explorer: https://altafnet.187-126-9-68.sslip.io
- ALTF contract (verified): [`TVjAVxeT6VVZNbSNMmoi8j3ZBFqDBN5y37`](https://tronscan.org/#/contract/TVjAVxeT6VVZNbSNMmoi8j3ZBFqDBN5y37/code)
- Whitepaper: [`docs/ALTAF-Whitepaper.pdf`](docs/ALTAF-Whitepaper.pdf)

## Run an ALTAF node (partners)

On a Linux x86_64 server with systemd:

```bash
curl -fsSL https://altafnet.187-126-9-68.sslip.io/install.sh | sudo bash
```

The node joins the network, syncs the chain and registers itself with its peers. Open TCP port 8545 in your provider's firewall.

## Build

```bash
# chain
cd chain && go build ./cmd/altafd ./cmd/altaf-wallet ./cmd/altaf-miner ./cmd/altaf-stratum
./regtest.sh <cpu-miner-address> <sha-miner-address>   # local 2-node test network

# exchange
cd exchange && python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app.main:app --port 8100
```

Configuration is read from environment variables (see `exchange/app/config.py`). No keys or secrets are included in this repository.

## License

MIT

## Bitcoin mining pool (partners with a bigger server)

```bash
curl -fsSL https://raw.githubusercontent.com/ridha20259/altaf/main/chain/dist/linux/install-bitcoind.sh | sudo bash   # pruned Bitcoin Core (~15 GB) + pool wallet + altaf-btcpool service
```

`altaf-btcpool` waits until the node is synced, then accepts ASIC miners (stratum v1, port 3334, username = the miner's Bitcoin address, e.g. their BTC deposit address on the ALTAF exchange). Found blocks and the PPLNS split (2 % pool fee) are written to `blocks.jsonl`. Payouts are automatic: once a block has 101 confirmations the pool sends every miner's share from the pool wallet in one `sendmany` (network fee split between recipients, minimum 10,000 sat, smaller amounts carry over, orphaned blocks pay nothing); payments are logged to `payouts.jsonl`.
