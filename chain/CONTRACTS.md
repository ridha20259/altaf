# Smart contracts on ALTAF Network

ALTAF Network runs the Ethereum Virtual Machine (go-ethereum `core/vm`, Shanghai + Cancun rules), so Solidity contracts work unchanged.

| | |
|---|---|
| Chain id | 41343 |
| Value unit | 1 wei = 0.000001 ALTF (6 decimals) |
| Gas | bought with the tx fee: gas limit = fee (units) × 100, the whole fee is charged; half to the miner, half burned |
| Addresses | an ALTAF address `A...` wraps a 20-byte EVM address (see `GET /contract/{address}` → `evm_address`) |

## Deploy and use a contract

```bash
solc --optimize --bin examples/Token.sol            # compile
printf '%s%064x' "$(cat Token.bin)" 1000 > t.hex     # bytecode + constructor argument
altaf-wallet deploy https://altafnet.187-126-9-68.sslip.io key.json t.hex
altaf-wallet receipt https://altafnet.187-126-9-68.sslip.io <txid>      # -> contract address
altaf-wallet read https://altafnet.187-126-9-68.sslip.io <contract> 70a08231<32-byte evm address>
altaf-wallet call https://altafnet.187-126-9-68.sslip.io key.json <contract> <calldata-hex> [ALTF] [fee]
```

Node API: `POST /tx` (field `data`, empty `to` = deploy), `GET /receipt/{txid}`, `GET /contract/{address}`, `POST /call` `{to, data, from?, value?}`.
