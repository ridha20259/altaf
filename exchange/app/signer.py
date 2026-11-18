"""ALTAF signer: the only process that holds the 24 words.
It can only (1) move deposit-address funds to the fixed hot wallet and (2) call openServer/closeServer
on the ALTF contract. Destinations and contracts come from its own env file, never from the caller."""
import hmac, json, os, time
from decimal import Decimal as D
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from bip_utils import Bip44, Bip44Coins, Bip44Changes, Bip39SeedGenerator

E = os.environ.get
MNEMONIC = E("ALTAF_MNEMONIC", "")
TOKEN = E("SIGNER_TOKEN", "")
HOT = {"TRON": E("SIGNER_HOT_TRON", ""), "BSC": E("SIGNER_HOT_BSC", "")}
CONTRACTS = {c.strip() for c in E("SIGNER_CONTRACTS", "").split(",") if c.strip()}
ALTF = E("SIGNER_ALTF_CONTRACT", "")
TRON_API, TRONGRID_KEY = E("TRON_API", "https://api.trongrid.io"), E("TRONGRID_KEY", "")
BSC_RPC = E("BSC_RPC", "https://bsc-dataseed.binance.org")
OPERATOR_INDEX = 9000
MAX_OPENS_PER_DAY = int(E("SIGNER_MAX_OPENS_PER_DAY", "2"))
LOG = Path(E("SIGNER_LOG", "/var/lib/altaf-signer/signer.log"))
_opens = []
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _log(**kw):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"t": int(time.time()), **kw}) + "\n")
    except Exception:
        pass


def _auth(tok):
    if not TOKEN or not hmac.compare_digest(tok or "", TOKEN):
        raise HTTPException(401, "bad token")


def derive(chain, index):
    coin = Bip44Coins.TRON if chain == "TRON" else Bip44Coins.BINANCE_SMART_CHAIN
    acc = Bip44.FromSeed(Bip39SeedGenerator(MNEMONIC).Generate(), coin).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
    return acc.PublicKey().ToAddress(), acc.PrivateKey().Raw().ToHex()


def _tron():
    from tronpy import Tron
    from tronpy.providers import HTTPProvider
    return Tron(HTTPProvider(TRON_API, api_key=TRONGRID_KEY or None))


def _send(chain, priv, frm, contract, decimals, to, amount):
    if chain == "TRON":
        from tronpy.keys import PrivateKey
        cl = _tron()
        if not contract:
            txn = cl.trx.transfer(frm, to, int(amount * 10**6))
        else:
            txn = cl.get_contract(contract).functions.transfer(to, int(amount * 10**decimals)).with_owner(frm).fee_limit(60_000_000)
        r = txn.build().sign(PrivateKey(bytes.fromhex(priv))).broadcast()
        res = r.wait(timeout=90)
        if res.get("receipt", {}).get("result", "SUCCESS") not in ("SUCCESS", None):
            raise RuntimeError(str(res)[:300])
        return r.txid, D(res.get("fee", 0)) / D(10**6)
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(BSC_RPC, request_kwargs={"timeout": 20}))
    frm, to = w3.to_checksum_address(frm), w3.to_checksum_address(to)
    gp, nonce = w3.eth.gas_price, w3.eth.get_transaction_count(frm)
    if not contract:
        tx = {"to": to, "value": int(amount * 10**18), "gas": 21000, "gasPrice": gp, "nonce": nonce, "chainId": 56}
    else:
        abi = [{"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"}]
        tx = w3.eth.contract(address=w3.to_checksum_address(contract), abi=abi).functions.transfer(to, int(amount * 10**decimals)).build_transaction(
            {"from": frm, "gas": 90000, "gasPrice": gp, "nonce": nonce, "chainId": 56})
    h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key=priv).raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rc.status != 1:
        raise RuntimeError("tx failed")
    return h.hex(), D(rc.gasUsed * gp) / D(10**18)


def check_sweep(b):
    """Returns (chain, index, address, contract, decimals, amount, hot) or raises HTTPException."""
    chain = str(b.get("chain", ""))
    if chain not in HOT or not HOT[chain]:
        raise HTTPException(400, "chain not configured")
    try:
        index, amount = int(b.get("index")), D(str(b.get("amount")))
    except Exception:
        raise HTTPException(400, "bad index/amount")
    if index < 0 or index == OPERATOR_INDEX or amount <= 0:
        raise HTTPException(403, "index not allowed")
    contract = str(b.get("contract") or "")
    if contract and contract not in CONTRACTS:
        raise HTTPException(403, "token contract not allowed")
    if b.get("to") and b["to"] != HOT[chain]:
        raise HTTPException(403, "destination must be the hot wallet")
    addr, _ = derive(chain, index)
    if b.get("address") and b["address"] != addr:
        raise HTTPException(400, "address/index mismatch")
    return chain, index, addr, contract, int(b.get("decimals") or 0), amount, HOT[chain]


@app.get("/health")
def health():
    return {"ok": bool(MNEMONIC and TOKEN), "hot": HOT, "altf": ALTF}


@app.post("/sweep")
def sweep(b: dict, x_signer_token: str = Header("")):
    _auth(x_signer_token)
    chain, index, addr, contract, dec, amount, hot = check_sweep(b)
    _, priv = derive(chain, index)
    try:
        txid, gas = _send(chain, priv, addr, contract, dec, hot, amount)
    except Exception as e:
        _log(op="sweep_fail", chain=chain, index=index, err=str(e)[:200])
        raise HTTPException(502, str(e)[:300])
    _log(op="sweep", chain=chain, index=index, contract=contract, amount=str(amount), txid=txid)
    return {"txid": txid, "gas": str(gas)}


@app.post("/operator")
def operator(b: dict, x_signer_token: str = Header("")):
    _auth(x_signer_token)
    fn, server = str(b.get("fn", "")), str(b.get("server", ""))
    if fn not in ("openServer", "closeServer"):
        raise HTTPException(403, "function not allowed")
    if not ALTF:
        raise HTTPException(400, "ALTF contract not set in signer")
    now = time.time()
    if fn == "openServer":
        _opens[:] = [t for t in _opens if now - t < 86400]
        if len(_opens) >= MAX_OPENS_PER_DAY:
            raise HTTPException(429, "too many openServer calls today")
    from tronpy.keys import PrivateKey
    tron = _tron()
    op_addr, priv = derive("TRON", OPERATOR_INDEX)
    c = tron.get_contract(ALTF)
    c.abi = json.loads((Path(__file__).resolve().parent.parent / "token" / "Altaf.json").read_text())["abi"]
    tx = getattr(c.functions, fn)(server).with_owner(op_addr).fee_limit(30_000_000).build().sign(PrivateKey(bytes.fromhex(priv)))
    res = tx.broadcast().wait(timeout=60)
    if res.get("receipt", {}).get("result") != "SUCCESS":
        _log(op=fn + "_fail", server=server)
        raise HTTPException(502, f"{fn} failed")
    if fn == "openServer":
        _opens.append(now)
    _log(op=fn, server=server, txid=tx.txid)
    return {"txid": tx.txid}
