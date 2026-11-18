"""On-chain deposits and withdrawals: TRON (TRX, TRC20), BSC (BNB, BEP20), Ethereum (ETH, ERC20), Bitcoin, Solana, ALTAF."""
import logging, time, threading
from datetime import datetime
from decimal import Decimal
import httpx
from sqlalchemy import select
from bip_utils import Bip44, Bip44Coins, Bip44Changes, Bip39SeedGenerator, Bip39MnemonicGenerator
from .config import MASTER_MNEMONIC, TRONGRID_KEY, TRON_API, BSC_RPC, ETH_RPC, XPUB, HOT_KEY, SIGNER_URL, SIGNER_TOKEN
from .db import SessionLocal, DepositAddress, Deposit, Withdrawal, Asset, AssetNetwork, User, Sweep
from .ledger import move, setting, house_id, D, ZERO
from .db import WRITE_LOCK
from .engine import q, usd_price

log = logging.getLogger("chain")
CHAINS = ("TRON", "BSC", "ALTAF", "ETH", "BTC", "SOL")
EVM = ("BSC", "ETH")
CHAIN_ID = {"BSC": 56, "ETH": 1}
RPC = {"BSC": BSC_RPC, "ETH": ETH_RPC}
from . import altafnative as nat
from . import natives as nt


def _seed():
    if not MASTER_MNEMONIC:
        raise RuntimeError("ALTAF_MNEMONIC not set")
    return Bip39SeedGenerator(MASTER_MNEMONIC).Generate()


def _coin(chain):
    return {"TRON": Bip44Coins.TRON, "ETH": Bip44Coins.ETHEREUM}.get(chain, Bip44Coins.BINANCE_SMART_CHAIN)


def _acct(chain):
    """ETH uses account 1 so its deposit addresses never equal BSC ones (same key type)."""
    return 1 if chain == "ETH" else 0


def derive(chain, index):
    if chain == "ALTAF":
        a, k = nat.derive(index)
        return a, bytes(k).hex()
    if chain == "BTC":
        return nt.btc_derive(_seed(), index)
    if chain == "SOL":
        return nt.sol_derive(_seed(), index)
    acc = Bip44.FromSeed(_seed(), _coin(chain)).Purpose().Coin().Account(_acct(chain)).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
    return acc.PublicKey().ToAddress(), acc.PrivateKey().Raw().ToHex()


def account_xpub(chain):
    if chain == "BTC":
        return nt.btc_xpub(_seed())
    return Bip44.FromSeed(_seed(), _coin(chain)).Purpose().Coin().Account(_acct(chain)).PublicKey().ToExtended()


def derive_address(chain, index):
    """Deposit address from the public account key only (no private key on the main server)."""
    if chain == "ALTAF":
        return nat.derive(index)[0]
    if chain == "BTC" and XPUB.get("BTC"):
        return nt.btc_address_from_xpub(XPUB["BTC"], index)
    if chain == "SOL" and SIGNER_URL:
        return signer("/address", {"chain": "SOL", "index": index})["address"]
    if XPUB.get(chain):
        acc = Bip44.FromExtendedKey(XPUB[chain], _coin(chain)).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
        return acc.PublicKey().ToAddress()
    return derive(chain, index)[0]


def key_address(chain, priv_hex):
    if chain == "ALTAF":
        return nat.derive(0)[0]
    if chain == "TRON":
        from tronpy.keys import PrivateKey
        return PrivateKey(bytes.fromhex(priv_hex)).public_key.to_base58check_address()
    if chain == "BTC":
        return nt.btc_key_address(priv_hex)
    if chain == "SOL":
        return nt.sol_key_address(priv_hex)
    from eth_account import Account
    return Account.from_key(priv_hex).address


def signer(path, body):
    r = httpx.post(SIGNER_URL.rstrip("/") + path, json=body, headers={"X-Signer-Token": SIGNER_TOKEN}, timeout=200)
    if r.status_code != 200:
        raise RuntimeError(f"signer {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def get_address(db, uid, chain):
    row = db.scalars(select(DepositAddress).where(DepositAddress.user_id == uid, DepositAddress.chain == chain)).first()
    if row:
        return row
    idx = (db.scalars(select(DepositAddress.hd_index).where(DepositAddress.chain == chain).order_by(DepositAddress.hd_index.desc()).limit(1)).first() or 0) + 1
    addr = derive_address(chain, idx)
    row = DepositAddress(user_id=uid, chain=chain, address=addr, hd_index=idx)
    db.add(row)
    db.commit()
    return row


# -------------------- TRON --------------------
def _tron():
    from tronpy import Tron
    from tronpy.providers import HTTPProvider
    return Tron(HTTPProvider(TRON_API, api_key=TRONGRID_KEY or None))


def _trongrid(path, **params):
    h = {"TRON-PRO-API-KEY": TRONGRID_KEY} if TRONGRID_KEY else {}
    r = httpx.get(f"{TRON_API}{path}", params=params, headers=h, timeout=20)
    r.raise_for_status()
    return r.json()


def scan_tron(db, row: DepositAddress, nets):
    found = []
    try:
        j = _trongrid(f"/v1/accounts/{row.address}/transactions", limit=50, only_to="true", min_timestamp=row.last_scan)
        for tx in j.get("data", []):
            try:
                c = tx["raw_data"]["contract"][0]
                if c["type"] != "TransferContract" or tx.get("ret", [{}])[0].get("contractRet") != "SUCCESS":
                    continue
                v = c["parameter"]["value"]
                if v.get("owner_address") == _hot_hex():
                    continue
                amt = D(v["amount"]) / D(10**6)
                found.append(("TRX", amt, tx["txID"]))
            except Exception:
                continue
        j = _trongrid(f"/v1/accounts/{row.address}/transactions/trc20", limit=50, only_to="true", min_timestamp=row.last_scan)
        bycontract = {n.contract: n for n in nets if n.chain == "TRON" and n.contract}
        for tx in j.get("data", []):
            n = bycontract.get(tx.get("token_info", {}).get("address"))
            if not n or tx.get("to") != row.address:
                continue
            found.append((n.asset, D(tx["value"]) / D(10**n.decimals), tx["transaction_id"]))
        row.last_scan = int(time.time() * 1000) - 600000
    except Exception as e:
        log.warning("tron scan %s: %s", row.address, e)
    return found


def _hot_hex():
    from tronpy.keys import to_hex_address
    return to_hex_address(hot_key("TRON")[0])


# -------------------- BSC --------------------
def _w3(chain="BSC"):
    from web3 import Web3
    return Web3(Web3.HTTPProvider(RPC[chain], request_kwargs={"timeout": 20}))


ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "from", "type": "address"}, {"indexed": True, "name": "to", "type": "address"}, {"indexed": False, "name": "value", "type": "uint256"}], "name": "Transfer", "type": "event"},
]


def scan_bsc(db, row: DepositAddress, nets):
    """Balance-based detection (BSC and Ethereum): credit any new balance found at the address, then sweep."""
    found = []
    chain = row.chain
    try:
        w3 = _w3(chain)
        addr = w3.to_checksum_address(row.address)
        bnb = D(w3.eth.get_balance(addr)) / D(10**18)
        if bnb + _moved(db, row.address, NATIVE[chain]) > D("0.0005"):
            found.append((NATIVE[chain], bnb, f"bal-{int(time.time())}"))
        for n in nets:
            if n.chain != chain or not n.contract:
                continue
            c = w3.eth.contract(address=w3.to_checksum_address(n.contract), abi=ERC20_ABI)
            b = D(c.functions.balanceOf(addr).call()) / D(10**n.decimals)
            if b + _moved(db, row.address, n.asset) > 0:
                found.append((n.asset, b, f"bal-{int(time.time())}"))
    except Exception as e:
        log.warning("%s scan %s: %s", chain, row.address, e)
    return found


def scan_sol(db, row: DepositAddress, nets):
    try:
        bal = nt.sol_balance(row.address)
        if bal + _moved(db, row.address, "SOL") > D("0.001"):
            return [("SOL", bal, f"bal-{int(time.time())}")]
    except Exception as e:
        log.warning("sol scan %s: %s", row.address, e)
    return []


def scan_btc(db, row: DepositAddress, nets):
    try:
        return [("BTC", amt, txid) for txid, amt in nt.btc_deposits(row.address)]
    except Exception as e:
        log.warning("btc scan %s: %s", row.address, e)
        return []


def _moved(db, address, asset):
    """Net amount that left the address for non-deposit reasons (sweeps, gas burnt) minus gas we sent in."""
    t = ZERO
    for r in db.scalars(select(Sweep).where(Sweep.address == address, Sweep.asset == asset)):
        t += -D(r.amount) if r.kind == "gas_in" else D(r.amount)
    return t


def credit_deposits(db):
    if not setting(db, "deposits_enabled", bool):
        return
    nets = db.scalars(select(AssetNetwork).where(AssetNetwork.active == True)).all()
    for row in db.scalars(select(DepositAddress)).all():
        if row.chain == "ALTAF":
            try:
                found = [("ALTF", a, t) for a, t in nat.scan(row.address)]
            except Exception as e:
                log.warning("altaf scan %s: %s", row.address, e)
                found = []
        else:
            found = {"TRON": scan_tron, "BTC": scan_btc, "SOL": scan_sol}.get(row.chain, scan_bsc)(db, row, nets)
        for asset, amount, txid in found:
            net = next((n for n in nets if n.asset == asset and n.chain == row.chain), None)
            if not net or amount < D(net.min_deposit or 0):
                continue
            exists = db.scalars(select(Deposit).where(Deposit.chain == row.chain, Deposit.txid == txid, Deposit.address == row.address, Deposit.asset == asset)).first()
            if exists:
                continue
            if row.chain in ("BSC", "ETH", "SOL"):
                prev = sum((D(d.amount) for d in db.scalars(select(Deposit).where(Deposit.address == row.address, Deposit.asset == asset))), ZERO)
                amount = amount + _moved(db, row.address, asset) - prev
                if amount <= 0:
                    continue
            with WRITE_LOCK:
                db.add(Deposit(user_id=row.user_id, chain=row.chain, asset=asset, amount=amount, txid=txid, address=row.address))
                move(db, row.user_id, asset, d_avail=amount, kind="deposit", ref=txid)
                db.commit()
            log.info("credited %s %s to user %s", amount, asset, row.user_id)


# -------------------- Withdrawals --------------------
def hot_key(chain):
    if chain == "ALTAF":
        return derive("ALTAF", 0)
    if HOT_KEY.get(chain):
        return key_address(chain, HOT_KEY[chain]), HOT_KEY[chain]
    return derive(chain, 0)


def legacy_hot_index():
    """Index 0 was the hot wallet before the key split; keep sweeping it into the new hot wallet."""
    return 0 if HOT_KEY.get("TRON") or HOT_KEY.get("BSC") else None


def _sweep_from(chain, index, address, asset, net, to, amount):
    if chain == "ALTAF":
        return nat.send(index, to, amount), D("0.01")
    if SIGNER_URL:
        r = signer("/sweep", {"chain": chain, "index": index, "address": address, "asset": asset, "amount": str(amount),
                              "contract": net.contract if net else "", "decimals": net.decimals if net else 0, "to": to})
        return r["txid"], D(r["gas"])
    _, priv = derive(chain, index)
    return _send_from(chain, priv, address, asset, net, to, amount)


def send_tron(asset, net, to, amount):
    from tronpy.keys import PrivateKey
    addr, priv = hot_key("TRON")
    cl = _tron()
    pk = PrivateKey(bytes.fromhex(priv))
    if asset == "TRX":
        txn = cl.trx.transfer(addr, to, int(D(amount) * 10**6))
    else:
        c = cl.get_contract(net.contract)
        txn = c.functions.transfer(to, int(D(amount) * 10**net.decimals)).with_owner(addr).fee_limit(60_000_000)
    r = txn.build().sign(pk).broadcast()
    res = r.wait(timeout=60)
    if res.get("receipt", {}).get("result", "SUCCESS") not in ("SUCCESS", None):
        raise RuntimeError(str(res))
    return r.txid


def send_bsc(asset, net, to, amount, chain="BSC"):
    w3 = _w3(chain)
    addr, priv = hot_key(chain)
    addr = w3.to_checksum_address(addr)
    to = w3.to_checksum_address(to)
    nonce = w3.eth.get_transaction_count(addr)
    gas_price = w3.eth.gas_price
    if asset == NATIVE[chain]:
        tx = {"to": to, "value": int(D(amount) * 10**18), "gas": 21000, "gasPrice": gas_price, "nonce": nonce, "chainId": CHAIN_ID[chain]}
    else:
        c = w3.eth.contract(address=w3.to_checksum_address(net.contract), abi=ERC20_ABI)
        tx = c.functions.transfer(to, int(D(amount) * 10**net.decimals)).build_transaction(
            {"from": addr, "gas": 90000, "gasPrice": gas_price, "nonce": nonce, "chainId": CHAIN_ID[chain]})
    signed = w3.eth.account.sign_transaction(tx, private_key=priv)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rcpt.status != 1:
        raise RuntimeError("tx failed")
    return h.hex()


def send_hot(chain, asset, net, to, amount):
    if chain == "ALTAF":
        return nat.send(0, to, amount)
    if chain == "TRON":
        return send_tron(asset, net, to, amount)
    if chain in EVM:
        return send_bsc(asset, net, to, amount, chain)
    addr, priv = hot_key(chain)
    return (nt.btc_send if chain == "BTC" else nt.sol_send)(priv, addr, to, amount)[0]


def process_withdrawals(db):
    if not setting(db, "withdrawals_enabled", bool):
        return
    rows = db.scalars(select(Withdrawal).where(Withdrawal.status == "pending").order_by(Withdrawal.id)).all()
    for w in rows:
        net = db.scalars(select(AssetNetwork).where(AssetNetwork.asset == w.asset, AssetNetwork.chain == w.chain)).first()
        if not net:
            w.status, w.error = "failed", "network not configured"
            db.commit()
            continue
        try:
            have = hot_balance(w.chain, w.asset, net)
        except Exception as e:
            log.warning("hot balance %s: %s", w.chain, e)
            continue
        if have < D(w.amount):
            if w.error != "waiting for hot wallet":
                w.error = "waiting for hot wallet"
                db.commit()
            continue
        w.status = "sending"
        db.commit()
        try:
            txid = send_hot(w.chain, w.asset, net, w.address, w.amount)
            w.status, w.txid, w.processed_at = "sent", txid, datetime.utcnow()
            with WRITE_LOCK:
                move(db, w.user_id, w.asset, d_locked=-(D(w.amount) + D(w.fee)), kind="withdraw_sent", ref=str(w.id))
                if D(w.fee) > 0:
                    move(db, house_id(db, "fees"), w.asset, d_avail=D(w.fee), kind="withdraw_fee", ref=str(w.id))
                db.commit()
            log.info("withdrawal %s sent %s", w.id, txid)
        except Exception as e:
            log.exception("withdrawal %s failed", w.id)
            w.status, w.error = "failed", str(e)[:1000]
            with WRITE_LOCK:
                move(db, w.user_id, w.asset, d_locked=-(D(w.amount) + D(w.fee)), d_avail=D(w.amount) + D(w.fee), kind="withdraw_refund", ref=str(w.id))
                db.commit()


def hot_balance(chain, asset, net=None):
    return addr_balance(chain, hot_key(chain)[0], asset, net)


def addr_balance(chain, addr, asset, net=None, tries=3):
    for i in range(tries):
        try:
            return _addr_balance(chain, addr, asset, net)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _addr_balance(chain, addr, asset, net=None):
    if chain == "ALTAF":
        return nat.balance(addr)
    if chain == "TRON":
        cl = _tron()
        if asset == "TRX":
            try:
                return D(str(cl.get_account_balance(addr)))
            except Exception as e:
                if "not found" in str(e).lower():
                    return ZERO
                raise
        return D(cl.get_contract(net.contract).functions.balanceOf(addr)) / D(10**net.decimals)
    if chain == "BTC":
        return nt.btc_balance(addr)
    if chain == "SOL":
        return nt.sol_balance(addr)
    w3 = _w3(chain)
    a = w3.to_checksum_address(addr)
    if asset == NATIVE[chain]:
        return D(w3.eth.get_balance(a)) / D(10**18)
    return D(w3.eth.contract(address=w3.to_checksum_address(net.contract), abi=ERC20_ABI).functions.balanceOf(a).call()) / D(10**net.decimals)


def _send_from(chain, priv, frm, asset, net, to, amount):
    """Send from any HD address we control. Returns (txid, native gas spent)."""
    if chain == "TRON":
        from tronpy.keys import PrivateKey
        cl = _tron()
        if asset == "TRX":
            txn = cl.trx.transfer(frm, to, int(D(amount) * 10**6))
        else:
            txn = cl.get_contract(net.contract).functions.transfer(to, int(D(amount) * 10**net.decimals)).with_owner(frm).fee_limit(60_000_000)
        r = txn.build().sign(PrivateKey(bytes.fromhex(priv))).broadcast()
        res = r.wait(timeout=90)
        if res.get("receipt", {}).get("result", "SUCCESS") not in ("SUCCESS", None):
            raise RuntimeError(str(res)[:300])
        return r.txid, D(res.get("fee", 0)) / D(10**6)
    if chain == "BTC":
        return nt.btc_send(priv, frm, to, amount)
    if chain == "SOL":
        return nt.sol_send(priv, frm, to, amount)
    w3 = _w3(chain)
    frm, to = w3.to_checksum_address(frm), w3.to_checksum_address(to)
    gp = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(frm)
    if asset == NATIVE[chain]:
        tx = {"to": to, "value": int(D(amount) * 10**18), "gas": 21000, "gasPrice": gp, "nonce": nonce, "chainId": CHAIN_ID[chain]}
    else:
        c = w3.eth.contract(address=w3.to_checksum_address(net.contract), abi=ERC20_ABI)
        tx = c.functions.transfer(to, int(D(amount) * 10**net.decimals)).build_transaction({"from": frm, "gas": 90000, "gasPrice": gp, "nonce": nonce, "chainId": CHAIN_ID[chain]})
    h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key=priv).raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rc.status != 1:
        raise RuntimeError("tx failed")
    return h.hex(), D(rc.gasUsed * gp) / D(10**18)


NATIVE = {"TRON": "TRX", "BSC": "BNB", "ALTAF": "ALTF", "ETH": "ETH", "BTC": "BTC", "SOL": "SOL"}
GAS_TOKEN = {"TRON": D("30"), "BSC": D("0.0004"), "ALTAF": D("0.01"), "ETH": D("0.0006"), "BTC": ZERO, "SOL": ZERO}   # native needed to move one token transfer
GAS_NATIVE = {"TRON": D("1.1"), "BSC": D("0.00005"), "ALTAF": D("0.01"), "ETH": D("0.0002"), "BTC": ZERO, "SOL": ZERO}  # left behind when sweeping the native coin
SEND_ALL = ("BTC", "SOL")  # sweeps send the whole balance; the network fee comes out of it


def sweep(db):
    """Move deposit-address funds into the hot wallet, then send hot wallet excess over the cap to the cold wallet."""
    nets = db.scalars(select(AssetNetwork).where(AssetNetwork.active == True)).all()
    min_usd = setting(db, "sweep_min_usd", D)
    rows = list(db.scalars(select(DepositAddress)).all())
    li = legacy_hot_index()
    if li is not None:
        for c in ("TRON", "BSC"):
            if HOT_KEY.get(c):
                rows.append(DepositAddress(user_id=0, chain=c, address=derive_address(c, li), hd_index=li))
    for row in rows:
        chain = row.chain
        hot = hot_key(chain)[0]
        for n in [x for x in nets if x.chain == chain]:
            asset = n.asset
            try:
                bal = addr_balance(chain, row.address, asset, n)
                if bal <= 0 or bal * usd_price(db, asset) < min_usd:
                    continue
                if chain in SEND_ALL:
                    txid, gas = _sweep_from(chain, row.hd_index, row.address, asset, n, hot, bal)
                    db.add(Sweep(chain=chain, address=row.address, asset=asset, kind="out", amount=bal, txid=txid))
                elif asset == NATIVE[chain]:
                    amt = bal - GAS_NATIVE[chain]
                    if amt <= 0:
                        continue
                    txid, gas = _sweep_from(chain, row.hd_index, row.address, asset, n, hot, amt)
                    db.add(Sweep(chain=chain, address=row.address, asset=asset, kind="out", amount=amt + gas, txid=txid))
                else:
                    native = addr_balance(chain, row.address, NATIVE[chain])
                    need = GAS_TOKEN[chain]
                    if native < need:
                        top = need - native
                        hp = hot_key(chain)[1]
                        if hot_balance(chain, NATIVE[chain]) < top + GAS_NATIVE[chain]:
                            log.warning("sweep %s %s: hot wallet has no %s for gas", chain, asset, NATIVE[chain])
                            continue
                        gtx, _ = _send_from(chain, hp, hot, NATIVE[chain], None, row.address, top)
                        db.add(Sweep(chain=chain, address=row.address, asset=NATIVE[chain], kind="gas_in", amount=top, txid=gtx))
                        db.commit()
                    txid, gas = _sweep_from(chain, row.hd_index, row.address, asset, n, hot, bal)
                    db.add(Sweep(chain=chain, address=row.address, asset=asset, kind="out", amount=bal, txid=txid))
                    db.add(Sweep(chain=chain, address=row.address, asset=NATIVE[chain], kind="gas_used", amount=gas, txid=txid))
                db.commit()
                log.info("swept %s %s from %s", bal, asset, row.address)
            except Exception as e:
                db.rollback()
                log.warning("sweep %s %s %s: %s", chain, asset, row.address, e)
    _to_cold(db, nets)


def _to_cold(db, nets):
    cap = setting(db, "hot_wallet_max_usd", D)
    for n in nets:
        cold = setting(db, "cold_address_" + n.chain.lower())
        if not cold or cap <= 0:
            continue
        try:
            bal = hot_balance(n.chain, n.asset, n)
            px = usd_price(db, n.asset)
            if px <= 0:
                continue
            keep = cap / px
            if n.asset == NATIVE[n.chain]:
                keep += GAS_TOKEN[n.chain] * 5
            extra = q(bal - keep, 6)
            if extra * px < D(50):
                continue
            hot, hp = hot_key(n.chain)
            txid, _ = _send_from(n.chain, hp, hot, n.asset, n, cold, extra)
            db.add(Sweep(chain=n.chain, address=hot, asset=n.asset, kind="cold", amount=extra, txid=txid))
            db.commit()
            log.info("sent %s %s to cold", extra, n.asset)
        except Exception as e:
            db.rollback()
            log.warning("cold %s %s: %s", n.chain, n.asset, e)


def reserves(db):
    """Hot wallet on-chain balances next to what users are owed, per asset."""
    from .db import Balance
    from sqlalchemy import func
    out = []
    for n in db.scalars(select(AssetNetwork).where(AssetNetwork.active == True)).all():
        try:
            hb = str(hot_balance(n.chain, n.asset, n))
        except Exception as e:
            log.warning("hot balance %s %s: %s", n.chain, n.asset, e)
            hb = "error"
        out.append({"chain": n.chain, "asset": n.asset, "hot": hb, "hot_address": hot_key(n.chain)[0]})
    owed = {a: str(v) for a, v in db.execute(select(Balance.asset, func.sum(Balance.available + Balance.locked)).join(User, User.id == Balance.user_id).where(User.is_system == False).group_by(Balance.asset))}
    return {"hot": out, "owed": owed}


_last = {"sweep": 0}


def worker():
    while True:
        db = SessionLocal()
        try:
            credit_deposits(db)
            process_withdrawals(db)
            if time.time() - _last["sweep"] > 600:
                _last["sweep"] = time.time()
                sweep(db)
        except Exception:
            log.exception("chain worker")
        finally:
            db.close()
        time.sleep(30)


def start():
    threading.Thread(target=worker, daemon=True).start()
