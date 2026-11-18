"""Self-service listing (1 ALTF / 30 days, auto-renew or auto-delist) and the Token Factory."""
import json, logging, re, threading
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from sqlalchemy import select
from .db import Asset, AssetNetwork, Market, Listing, Order, TokenJob, SessionLocal
from .ledger import move, transfer, setting, house_id, bal, InsufficientFunds, D, ZERO
from .db import WRITE_LOCK
from .engine import OrderError, _close
from . import risk

log = logging.getLogger("listing")
TOKEN_DIR = Path(__file__).resolve().parent.parent / "token"
SYM_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def token_info(chain, contract):
    from . import chain as ch
    if chain == "TRON":
        cl = ch._tron()
        c = cl.get_contract(contract)
        return {"name": c.functions.name(), "symbol": c.functions.symbol(), "decimals": int(c.functions.decimals()),
                "supply": str(c.functions.totalSupply())}
    w3 = ch._w3()
    abi = [{"name": n, "inputs": [], "outputs": [{"type": t}], "stateMutability": "view", "type": "function"}
           for n, t in (("name", "string"), ("symbol", "string"), ("decimals", "uint8"), ("totalSupply", "uint256"))]
    c = w3.eth.contract(address=w3.to_checksum_address(contract), abi=abi)
    return {"name": c.functions.name().call(), "symbol": c.functions.symbol().call(), "decimals": int(c.functions.decimals().call()),
            "supply": str(c.functions.totalSupply().call())}


def _charge_altf(db, uid, amount, kind, ref=""):
    try:
        transfer(db, uid, house_id(db, "fees"), "ALTF", amount, kind, ref)
    except InsufficientFunds:
        raise OrderError(f"You need {amount} ALTF (buy ALTF with Convert)")


def create_listing(db, uid, chain, contract, name, symbol, decimals, website="", description="", logo="", skip_fee=False):
    symbol = symbol.upper().strip()
    if not SYM_RE.match(symbol):
        raise OrderError("Token symbol must be 2-12 letters/numbers")
    if db.get(Asset, symbol):
        raise OrderError(f"{symbol} is already listed")
    if db.scalars(select(AssetNetwork).where(AssetNetwork.contract == contract)).first():
        raise OrderError("This contract is already listed")
    fee = setting(db, "listing_fee_altf", D)
    if not skip_fee:
        _charge_altf(db, uid, fee, "listing_fee", symbol)
    days = int(setting(db, "listing_days", D))
    a = Asset(symbol=symbol, name=name[:100], precision=min(decimals, 8), logo=logo[:2000], price_source="internal",
              listed_by=uid, listing_expires=datetime.utcnow() + timedelta(days=days), website=website[:300],
              description=description[:5000], sort=500)
    db.add(a)
    db.add(AssetNetwork(asset=symbol, chain=chain, contract=contract, decimals=decimals, withdraw_fee=ZERO, min_withdraw=ZERO, min_deposit=ZERO))
    db.add(Market(symbol=symbol + "USDT", base=symbol, quote="USDT", price_prec=8, qty_prec=2, min_notional=D(1), external=False, sort=500))
    db.add(Listing(user_id=uid, asset=symbol, chain=chain, contract=contract, paid_altf=ZERO if skip_fee else fee,
                   expires_at=a.listing_expires))
    db.flush()
    risk.review_asset(db, a)
    return a


def delist(db, symbol, reason="expired"):
    a = db.get(Asset, symbol)
    m = db.get(Market, symbol + "USDT")
    if m:
        m.active = False
        for o in db.scalars(select(Order).where(Order.market == m.symbol, Order.status == "open")).all():
            _close(db, m, o, "cancelled")
    if a:
        a.deposit_enabled = False
        a.risk_notes = (a.risk_notes + f"; delisted: {reason}").strip("; ")
    for l in db.scalars(select(Listing).where(Listing.asset == symbol, Listing.status == "active")).all():
        l.status = "delisted"


def renew(db, listing: Listing, manual=False):
    fee = setting(db, "listing_fee_altf", D)
    _charge_altf(db, listing.user_id, fee, "listing_renew", listing.asset)
    base = max(listing.expires_at, datetime.utcnow())
    listing.expires_at = base + timedelta(days=int(setting(db, "listing_days", D)))
    listing.paid_altf = D(listing.paid_altf) + fee
    a = db.get(Asset, listing.asset)
    if a:
        a.listing_expires = listing.expires_at
        a.deposit_enabled = True
    m = db.get(Market, listing.asset + "USDT")
    if m and listing.status == "delisted":
        m.active = True
    listing.status = "active"


def renew_job(db):
    for l in db.scalars(select(Listing).where(Listing.status == "active", Listing.expires_at < datetime.utcnow())).all():
        try:
            if not l.auto_renew:
                raise OrderError("auto-renew off")
            renew(db, l)
            log.info("renewed %s", l.asset)
        except OrderError:
            delist(db, l.asset)
            log.info("delisted %s", l.asset)


# -------------------- Token Factory --------------------
def factory_price(db, chain):
    return {"usdt": setting(db, "factory_net_usd_tron" if chain == "TRON" else "factory_net_usd_bsc", D),
            "altf": setting(db, "factory_fee_altf", D), "listing_altf": setting(db, "listing_fee_altf", D)}


def order_token(db, uid, chain, name, symbol, supply, decimals, owner, website, description, logo, list_it):
    symbol = symbol.upper().strip()
    name = name.strip()
    if chain not in ("TRON", "BSC"):
        raise OrderError("Choose TRON or BSC")
    if not SYM_RE.match(symbol) or not (2 <= len(name) <= 40):
        raise OrderError("Name 2-40 chars, symbol 2-12 letters/numbers")
    supply = D(supply)
    decimals = int(decimals)
    if supply <= 0 or supply > D(10) ** 15 or not (0 <= decimals <= 18):
        raise OrderError("Supply 1 - 1,000,000,000,000,000; decimals 0-18")
    if chain == "TRON":
        from tronpy.keys import is_base58check_address
        if not is_base58check_address(owner):
            raise OrderError("Invalid TRON address")
    else:
        if not re.match(r"^0x[0-9a-fA-F]{40}$", owner):
            raise OrderError("Invalid BSC address")
    if list_it and db.get(Asset, symbol):
        raise OrderError(f"{symbol} already exists on the exchange; choose another symbol")
    p = factory_price(db, chain)
    altf = p["altf"] + (p["listing_altf"] if list_it else ZERO)
    try:
        transfer(db, uid, house_id(db, "fees"), "USDT", p["usdt"], "factory_fee", symbol)
    except InsufficientFunds:
        raise OrderError(f"You need {p['usdt']} USDT")
    _charge_altf(db, uid, altf, "factory_fee", symbol)
    j = TokenJob(user_id=uid, chain=chain, name=name, symbol=symbol, decimals=decimals, supply=supply, owner_address=owner,
                 website=website[:300], description=description[:5000], logo=logo[:2000], list_it=bool(list_it),
                 paid_usdt=p["usdt"], paid_altf=altf, status="queued")
    db.add(j)
    db.flush()
    return j


def _tron_addr_hex20(addr):
    from tronpy.keys import to_hex_address
    return "0x" + to_hex_address(addr)[2:]


def deploy_tron(art, args_types, args, priv_hex, fee_limit_trx=300):
    """Deploy compiled contract on TRON. Returns (contract_base58, txid)."""
    from eth_abi import encode
    from tronpy.contract import Contract
    from tronpy.keys import PrivateKey
    from . import chain as ch
    cl = ch._tron()
    pk = PrivateKey(bytes.fromhex(priv_hex))
    owner = pk.public_key.to_base58check_address()
    bytecode = art["bin"] + encode(args_types, args).hex()
    cntr = Contract(name=art.get("name", "Token"), bytecode=bytecode, abi=art["abi"], origin_energy_limit=10_000_000, user_resource_percent=100)
    txn = cl.trx.deploy_contract(owner, cntr).fee_limit(fee_limit_trx * 1_000_000).build().sign(pk)
    r = txn.broadcast()
    res = r.wait(timeout=120)
    if res.get("receipt", {}).get("result") not in (None, "SUCCESS"):
        raise RuntimeError(f"deploy failed: {res.get('receipt')}")
    from tronpy.keys import to_base58check_address
    return to_base58check_address(res["contract_address"]), r.txid


def deploy_bsc(art, args, priv_hex):
    from . import chain as ch
    w3 = ch._w3()
    acct = w3.eth.account.from_key(priv_hex)
    C = w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
    tx = C.constructor(*args).build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
                                                 "gasPrice": w3.eth.gas_price, "chainId": 56})
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rc = w3.eth.wait_for_transaction_receipt(h, timeout=240)
    if rc.status != 1:
        raise RuntimeError("deploy reverted")
    return rc.contractAddress, h.hex()


def run_job(job_id):
    from . import chain as ch
    db = SessionLocal()
    try:
        j = db.get(TokenJob, job_id)
        if not j or j.status != "queued":
            return
        j.status = "deploying"
        db.commit()
        art = json.loads((TOKEN_DIR / "Factory.json").read_text())
        supply_units = int(D(j.supply) * (D(10) ** j.decimals))
        try:
            _, priv = ch.hot_key(j.chain)
            if j.chain == "TRON":
                contract, txid = deploy_tron(art, ["string", "string", "uint8", "uint256", "address"],
                                             [j.name, j.symbol, j.decimals, supply_units, _tron_addr_hex20(j.owner_address)], priv)
            else:
                from web3 import Web3
                contract, txid = deploy_bsc(art, [j.name, j.symbol, j.decimals, supply_units, Web3.to_checksum_address(j.owner_address)], priv)
            j.contract, j.txid, j.status = contract, txid, "done"
            if j.list_it:
                with WRITE_LOCK:
                    try:
                        a = create_listing(db, j.user_id, j.chain, contract, j.name, j.symbol, j.decimals, j.website, j.description, j.logo, skip_fee=True)
                        l = db.scalars(select(Listing).where(Listing.asset == a.symbol)).first()
                        l.paid_altf = setting(db, "listing_fee_altf", D)
                    except OrderError as e:
                        j.error = f"created, listing failed: {e}"
            db.commit()
        except Exception as e:
            log.exception("token job %s", job_id)
            db.rollback()
            j = db.get(TokenJob, job_id)
            j.status, j.error = "failed", str(e)[:1000]
            with WRITE_LOCK:
                fees = house_id(db, "fees")
                transfer(db, fees, j.user_id, "USDT", D(j.paid_usdt), "factory_refund", j.symbol)
                transfer(db, fees, j.user_id, "ALTF", D(j.paid_altf), "factory_refund", j.symbol)
            db.commit()
    finally:
        db.close()


def start_job(job_id):
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
