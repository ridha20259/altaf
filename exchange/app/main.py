"""ALTAF Exchange API."""
import logging, re, threading, time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, or_
from . import prices, engine as eng, futures as fut, earn, p2p, listing, risk, servers, bots, governance as gov, mm
from .config import ADMIN_EMAILS, CHAIN_WORKERS, SITE_NAME
from .db import (SessionLocal, init_db, WRITE_LOCK, User, Asset, AssetNetwork, Balance, Ledger, Market, Order, Position,
                 EarnProduct, EarnStake, P2PAd, P2POrder, P2PMessage, Deposit, Withdrawal, WhitelistAddress, SecurityEvent,
                 Listing, TokenJob, Trade)
from .ledger import bal, move, transfer, setting, set_setting, all_settings, house_id, InsufficientFunds, D, ZERO
from .engine import OrderError
from .security import (hash_pw, check_pw, make_token, read_token, new_totp_secret, check_totp, totp_uri, limiter, log_event)
from .servers import ServerNode
from . import apikeys
from . import referral
from . import tgbot
from . import staking
from fastapi import WebSocket

log = logging.getLogger("altaf")
logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title=SITE_NAME, docs_url=None, redoc_url=None)
app.add_middleware(apikeys.ApiKeyAuth)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
WALLETS = ("spot", "futures")


# ---------------- helpers ----------------
def js(o):
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if isinstance(o, Decimal):
        return format(o.normalize(), "f") if o == o.to_integral() else format(o, "f").rstrip("0").rstrip(".")
    if isinstance(o, datetime):
        return o.isoformat() + "Z"
    if isinstance(o, dict):
        return {k: js(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [js(v) for v in o]
    if hasattr(o, "__table__"):
        hide = {"pw_hash", "totp_secret", "kyc_files"}
        return {c.name: js(getattr(o, c.name)) for c in o.__table__.columns if c.name not in hide}
    return str(o)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ip_of(r: Request):
    return r.headers.get("x-real-ip") or (r.client.host if r.client else "")


def current_user(request: Request, db=Depends(get_db)):
    api_uid = getattr(request.state, "api_uid", None)
    if api_uid:
        u = db.get(User, api_uid)
        if not u or u.is_system:
            raise HTTPException(401, "Invalid API key")
        return u
    h = request.headers.get("authorization", "")
    data = read_token(h[7:]) if h.startswith("Bearer ") else None
    if not data:
        raise HTTPException(401, "Please log in")
    u = db.get(User, data["u"])
    if not u or u.token_version != data["v"] or u.is_system:
        raise HTTPException(401, "Session expired, log in again")
    return u


def admin_user(u=Depends(current_user)):
    if not u.is_admin:
        raise HTTPException(403, "Admins only")
    return u


def need_2fa(u: User, code):
    if u.totp_enabled and not check_totp(u.totp_secret, str(code or "")):
        raise HTTPException(400, "Wrong 2FA code")


def run(db, fn):
    """Run a balance-changing action atomically."""
    with WRITE_LOCK:
        try:
            r = fn()
            db.commit()
            return r
        except (OrderError, InsufficientFunds) as e:
            db.rollback()
            raise HTTPException(400, str(e))
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            log.exception("action failed")
            raise HTTPException(500, "Server error, nothing was changed")


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and not limiter.hit("ip:" + ip_of(request), 300, 60):
        return JSONResponse({"detail": "Too many requests, slow down"}, 429)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------- seed ----------------
ASSETS = [("USDT", "Tether USD", 2, "fixed"), ("USDC", "USD Coin", 2, "fixed"), ("BTC", "Bitcoin", 8, "BTCUSDT"), ("ETH", "Ethereum", 6, "ETHUSDT"),
          ("BNB", "BNB", 6, "BNBUSDT"), ("SOL", "Solana", 4, "SOLUSDT"), ("TRX", "TRON", 2, "TRXUSDT"),
          ("ALTF", "Altaf", 6, "internal")]
NETWORKS = [("USDT", "TRON", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6, "1", "10"), ("USDT", "BSC", "0x55d398326f99059fF775485246999027B3197955", 18, "0.3", "10"),
            ("USDC", "BSC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18, "0.3", "10"),
            ("TRX", "TRON", "", 6, "1", "10"), ("BNB", "BSC", "", 18, "0.0005", "0.01"),
            ("BTC", "BSC", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", 18, "0.00001", "0.0002"),
            ("ETH", "BSC", "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18, "0.0002", "0.005"),
            ("SOL", "BSC", "0x570A5D26f7765Ecb712C0924E4De545B89fD43dF", 18, "0.005", "0.1"),
            ("BTC", "BTC", "", 8, "0.00005", "0.0002"), ("ETH", "ETH", "", 18, "0.0008", "0.005"), ("SOL", "SOL", "", 9, "0.01", "0.05"),
            ("USDT", "ETH", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6, "3", "10"),
            ("USDC", "ETH", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6, "3", "10")]
NEW_CHAINS = ("BTC", "ETH", "SOL")  # added inactive; the admin turns them on once the hot wallets hold gas
MARKETS = [("BTCUSDT", "BTC", 2, 6, True, True, 1), ("ETHUSDT", "ETH", 2, 5, True, True, 2), ("BNBUSDT", "BNB", 2, 4, True, True, 3),
           ("SOLUSDT", "SOL", 3, 3, True, True, 4), ("TRXUSDT", "TRX", 5, 1, True, True, 5), ("ALTFUSDT", "ALTF", 4, 2, False, False, 0)]
EARN = [("USDT", "0.05", 0, "10"), ("USDT", "0.09", 30, "50"), ("ALTF", "0.12", 90, "1"), ("TRX", "0.04", 0, "100")]


def seed():
    db = SessionLocal()
    try:
        for i, (s, n, p, src) in enumerate(ASSETS):
            if not db.get(Asset, s):
                db.add(Asset(symbol=s, name=n, precision=p, price_source=src, sort=i, website="https://altaf" if s == "ALTF" else ""))
        for a, c, con, dec, fee, mn in NETWORKS:
            if not db.scalars(select(AssetNetwork).where(AssetNetwork.asset == a, AssetNetwork.chain == c)).first():
                db.add(AssetNetwork(asset=a, chain=c, contract=con, decimals=dec, withdraw_fee=D(fee), min_withdraw=D(mn) * 2, min_deposit=D(mn) / 10,
                                    active=c not in NEW_CHAINS))
        for sym, base, pp, qp, ext, f, sort in MARKETS:
            if not db.get(Market, sym):
                db.add(Market(symbol=sym, base=base, quote="USDT", price_prec=pp, qty_prec=qp, min_notional=D(1), external=ext, futures=f, sort=sort))
        if not db.scalars(select(EarnProduct)).first():
            for a, apr, days, mn in EARN:
                db.add(EarnProduct(asset=a, apr=D(apr), lock_days=days, min_amount=D(mn), cap=ZERO))
        for name in ("house", "fees", "mm"):
            em = f"{name}@altaf.system"
            if not db.scalars(select(User).where(User.email == em)).first():
                db.add(User(email=em, pw_hash="!", is_system=True, nickname=name))
        db.commit()
    finally:
        db.close()


def loop(name, fn, every):
    def w():
        while True:
            db = SessionLocal()
            try:
                with WRITE_LOCK:
                    fn(db)
                    db.commit()
            except Exception:
                log.exception(name)
                db.rollback()
            finally:
                db.close()
            time.sleep(every)
    threading.Thread(target=w, daemon=True, name=name).start()


@app.on_event("startup")
def startup():
    init_db()
    seed()
    prices.start()
    loop("futures-risk", fut.risk_loop_once, 2)
    loop("p2p-expire", p2p.expire, 30)
    loop("earn", earn.accrue_all, 600)
    loop("staking", staking.run, 3600)
    loop("listing-renew", listing.renew_job, 900)
    loop("risk-review", risk.review_all, 3600)
    loop("bots", lambda db: bots.tick(), 15)
    loop("market-maker", mm.tick, 5)
    if CHAIN_WORKERS:
        from . import chain
        chain.start()
        servers.start()


# ---------------- auth ----------------
@app.post("/api/register")
def register(body: dict, request: Request, db=Depends(get_db)):
    ip = ip_of(request)
    if not limiter.hit("reg:" + ip, 5, 3600):
        raise HTTPException(429, "Too many sign-ups from your network")
    email = str(body.get("email", "")).strip().lower()
    pw = str(body.get("password", ""))
    if not EMAIL_RE.match(email) or email.endswith("@altaf.system"):
        raise HTTPException(400, "Enter a valid email")
    if len(pw) < 8 or pw.isdigit() or pw.isalpha():
        raise HTTPException(400, "Password: at least 8 characters, letters and numbers")
    if db.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(400, "Email already registered")
    u = User(email=email, pw_hash=hash_pw(pw), is_admin=email in ADMIN_EMAILS, nickname=email.split("@")[0][:20])
    db.add(u)
    db.flush()
    referral.attach(db, u.id, str(body.get("ref", "")))
    log_event(db, "register", u.id, ip)
    db.commit()
    return {"token": make_token(u.id, u.token_version), "user": me_view(db, u)}


@app.post("/api/tglink")
def tg_link_code(u=Depends(current_user), db=Depends(get_db)):
    code = tgbot.new_link_code(db, u.id)
    db.commit()
    return {"code": code, "bot": setting(db, "tg_bot_username")}


@app.get("/api/referral")
def my_referral(u=Depends(current_user), db=Depends(get_db)):
    out = referral.stats(db, u.id)
    db.commit()
    return out


@app.post("/api/login")
def login(body: dict, request: Request, db=Depends(get_db)):
    ip = ip_of(request)
    email = str(body.get("email", "")).strip().lower()
    if not limiter.hit("login:" + ip, 10, 600) or not limiter.hit("login:" + email, 10, 600):
        raise HTTPException(429, "Too many attempts, wait 10 minutes")
    u = db.scalars(select(User).where(User.email == email)).first()
    if not u or u.is_system or not check_pw(str(body.get("password", "")), u.pw_hash):
        log_event(db, "login_fail", u.id if u else None, ip, email, "warn")
        db.commit()
        raise HTTPException(400, "Wrong email or password")
    if u.totp_enabled:
        if not body.get("code"):
            return {"need_2fa": True}
        if not check_totp(u.totp_secret, str(body["code"])):
            log_event(db, "login_2fa_fail", u.id, ip, "", "warn")
            db.commit()
            raise HTTPException(400, "Wrong 2FA code")
    if u.frozen:
        raise HTTPException(403, "Account frozen, contact support")
    log_event(db, "login", u.id, ip)
    db.commit()
    return {"token": make_token(u.id, u.token_version), "user": me_view(db, u)}


def me_view(db, u):
    d = js(u)
    d["launchpad_access"] = bal(db, u.id, "ALTF", lock=False).available >= 1
    d["partner"] = bool(gov.role(db, u))
    return d


@app.get("/api/me")
def me(u=Depends(current_user), db=Depends(get_db)):
    return me_view(db, u)


@app.post("/api/2fa/setup")
def twofa_setup(u=Depends(current_user), db=Depends(get_db)):
    if u.totp_enabled:
        raise HTTPException(400, "2FA already on")
    u.totp_secret = new_totp_secret()
    db.commit()
    return {"secret": u.totp_secret, "uri": totp_uri(u.totp_secret, u.email)}


@app.post("/api/2fa/enable")
def twofa_enable(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    if not u.totp_secret or not check_totp(u.totp_secret, str(body.get("code", ""))):
        raise HTTPException(400, "Wrong code")
    u.totp_enabled = True
    u.security_changed_at = datetime.utcnow()
    log_event(db, "2fa_on", u.id, ip_of(request))
    db.commit()
    return {"ok": True}


@app.post("/api/password")
def change_pw(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    if not check_pw(str(body.get("old", "")), u.pw_hash):
        raise HTTPException(400, "Current password is wrong")
    need_2fa(u, body.get("code"))
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "Password too short")
    u.pw_hash, u.token_version, u.security_changed_at = hash_pw(new), u.token_version + 1, datetime.utcnow()
    log_event(db, "password_change", u.id, ip_of(request), "", "warn")
    db.commit()
    return {"token": make_token(u.id, u.token_version)}


@app.post("/api/settings/fee-altf")
def fee_altf(body: dict, u=Depends(current_user), db=Depends(get_db)):
    u.pay_fee_altf = bool(body.get("on"))
    db.commit()
    return {"ok": True}


# ---------------- public market data ----------------
def assets_view(db):
    out = []
    for a in db.scalars(select(Asset).where(Asset.active == True).order_by(Asset.sort, Asset.symbol)):
        d = js(a)
        d["usd"] = js(eng.usd_price(db, a.symbol))
        d["networks"] = [js(n) for n in db.scalars(select(AssetNetwork).where(AssetNetwork.asset == a.symbol, AssetNetwork.active == True))]
        out.append(d)
    return out


@app.get("/api/assets")
def assets(db=Depends(get_db)):
    return assets_view(db)


@app.get("/api/markets")
def markets(db=Depends(get_db)):
    out = []
    for m in db.scalars(select(Market).where(Market.active == True).order_by(Market.sort, Market.symbol)):
        d = js(m)
        d.update(eng.ticker(db, m))
        a = db.get(Asset, m.base)
        d["risk"] = a.risk_level if a else "ok"
        out.append(d)
    return out


@app.get("/api/book/{symbol}")
def book(symbol: str, db=Depends(get_db)):
    return js(eng.book(db, symbol.upper()))


@app.get("/api/klines/{symbol}")
def klines(symbol: str, interval: str = "15m", db=Depends(get_db)):
    return js(eng.klines(db, symbol.upper(), interval))


@app.get("/api/trades/{symbol}")
def trades(symbol: str, db=Depends(get_db)):
    return eng.recent_trades(db, symbol.upper())


@app.get("/api/config")
def public_config(db=Depends(get_db)):
    s = all_settings(db)
    keys = ("maker_fee", "taker_fee", "altf_fee_discount", "altf_price", "futures_enabled", "futures_max_leverage", "futures_fee",
            "listing_fee_altf", "listing_days", "factory_fee_altf", "factory_net_usd_tron", "factory_net_usd_bsc", "withdrawals_enabled",
            "deposits_enabled", "altf_contract")
    return {k: s.get(k, "") for k in keys}


# ---------------- wallet ----------------
@app.get("/api/balances")
def balances(u=Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Balance).where(Balance.user_id == u.id)).all()
    out, total = [], ZERO
    for b in rows:
        usd = (D(b.available) + D(b.locked)) * eng.usd_price(db, b.asset)
        total += usd
        out.append({"wallet": b.wallet, "asset": b.asset, "available": js(b.available), "locked": js(b.locked), "usd": js(round(usd, 2))})
    return {"total_usd": js(round(total, 2)), "balances": out}


@app.get("/api/ledger")
def ledger(u=Depends(current_user), db=Depends(get_db)):
    return [js(r) for r in db.scalars(select(Ledger).where(Ledger.user_id == u.id).order_by(Ledger.id.desc()).limit(200))]


@app.post("/api/transfer")
def wallet_transfer(body: dict, u=Depends(current_user), db=Depends(get_db)):
    frm, to, asset, amt = body.get("from"), body.get("to"), str(body.get("asset", "")).upper(), D(str(body.get("amount", "0")))
    if frm not in WALLETS or to not in WALLETS or frm == to or amt <= 0:
        raise HTTPException(400, "Bad transfer")
    return run(db, lambda: transfer(db, u.id, u.id, asset, amt, "wallet_transfer", f"{frm}->{to}", frm, to) or {"ok": True})


@app.post("/api/pay")
def altaf_pay(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    """Instant free internal transfer to another user by email (ALTAF Pay)."""
    asset, amt = str(body.get("asset", "")).upper(), D(str(body.get("amount", "0")))
    email = str(body.get("email", "")).strip().lower()
    if not u.totp_enabled:
        raise HTTPException(400, "Turn on 2FA before sending")
    need_2fa(u, body.get("code"))
    if amt <= 0 or not db.get(Asset, asset):
        raise HTTPException(400, "Bad amount")
    to = db.scalars(select(User).where(User.email == email)).first()
    if not to or to.email.endswith("@altaf.system"):
        raise HTTPException(404, "No ALTAF user with this email")
    if to.id == u.id:
        raise HTTPException(400, "You cannot send to yourself")
    note = str(body.get("note", ""))[:60]

    def go():
        transfer(db, u.id, to.id, asset, amt, "pay", f"{u.email}->{to.email} {note}".strip())
        log_event(db, "pay", u.id, ip_of(request), f"{amt} {asset} to {to.id}")
        return {"ok": True, "to": to.nickname}
    return run(db, go)


@app.get("/api/pay")
def pay_history(u=Depends(current_user), db=Depends(get_db)):
    rows = db.scalars(select(Ledger).where(Ledger.user_id == u.id, Ledger.kind == "pay").order_by(Ledger.id.desc()).limit(50)).all()
    return [{"asset": r.asset, "amount": str(r.d_available), "ref": r.ref, "at": r.created_at.isoformat()} for r in rows]


@app.get("/api/deposit/{chain}")
def deposit_address(chain: str, u=Depends(current_user), db=Depends(get_db)):
    from . import chain as ch
    chain = chain.upper()
    if chain not in ch.CHAINS:
        raise HTTPException(400, "Unknown network")
    if not setting(db, "deposits_enabled", bool):
        raise HTTPException(400, "Deposits are paused")
    if not db.scalars(select(AssetNetwork).where(AssetNetwork.chain == chain, AssetNetwork.active == True)).first():  # noqa: E712
        raise HTTPException(400, "This network opens soon")
    try:
        with WRITE_LOCK:
            row = ch.get_address(db, u.id, chain)
    except RuntimeError:
        raise HTTPException(503, "Deposits open soon")
    return {"chain": chain, "address": row.address}


@app.get("/api/deposits")
def deposits(u=Depends(current_user), db=Depends(get_db)):
    return [js(r) for r in db.scalars(select(Deposit).where(Deposit.user_id == u.id).order_by(Deposit.id.desc()).limit(100))]


@app.get("/api/withdrawals")
def withdrawals(u=Depends(current_user), db=Depends(get_db)):
    return [js(r) for r in db.scalars(select(Withdrawal).where(Withdrawal.user_id == u.id).order_by(Withdrawal.id.desc()).limit(100))]


def valid_address(chain, addr):
    if chain == "ALTAF":
        from .altafnative import valid
        return valid(addr)
    if chain == "TRON":
        from tronpy.keys import is_base58check_address
        return is_base58check_address(addr)
    if chain in ("BTC", "SOL"):
        from . import natives
        return natives.btc_valid(addr) if chain == "BTC" else natives.sol_valid(addr)
    return bool(re.match(r"^0x[0-9a-fA-F]{40}$", addr))


@app.post("/api/whitelist")
def add_whitelist(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    chain, addr = str(body.get("chain", "")).upper(), str(body.get("address", "")).strip()
    if not valid_address(chain, addr):
        raise HTTPException(400, f"This is not a valid {chain} address")
    need_2fa(u, body.get("code"))
    db.add(WhitelistAddress(user_id=u.id, chain=chain, address=addr, label=str(body.get("label", ""))[:40]))
    u.security_changed_at = datetime.utcnow()
    log_event(db, "whitelist_add", u.id, ip_of(request), f"{chain} {addr}", "warn")
    db.commit()
    return {"ok": True, "note": "New address: withdrawals to it unlock in 24 hours"}


@app.get("/api/whitelist")
def list_whitelist(u=Depends(current_user), db=Depends(get_db)):
    return [js(r) for r in db.scalars(select(WhitelistAddress).where(WhitelistAddress.user_id == u.id))]


@app.post("/api/withdraw")
def withdraw(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    ip = ip_of(request)
    asset, chain = str(body.get("asset", "")).upper(), str(body.get("chain", "")).upper()
    addr, amt = str(body.get("address", "")).strip(), D(str(body.get("amount", "0")))
    if not setting(db, "withdrawals_enabled", bool):
        raise HTTPException(400, "Withdrawals are paused for maintenance")
    if not limiter.hit(f"wd:{u.id}", 10, 3600):
        raise HTTPException(429, "Too many withdrawal requests")
    if not u.totp_enabled:
        raise HTTPException(400, "Turn on 2FA before withdrawing")
    need_2fa(u, body.get("code"))
    if u.frozen:
        raise HTTPException(403, "Account frozen")
    net = db.scalars(select(AssetNetwork).where(AssetNetwork.asset == asset, AssetNetwork.chain == chain, AssetNetwork.active == True)).first()
    a = db.get(Asset, asset)
    if not net or not a or not a.withdraw_enabled:
        raise HTTPException(400, f"{asset} withdrawals on {chain} are not available")
    if not valid_address(chain, addr):
        raise HTTPException(400, f"This is not a valid {chain} address")
    if amt < D(net.min_withdraw) or amt <= 0:
        raise HTTPException(400, f"Minimum withdrawal {js(net.min_withdraw)} {asset}")
    fee = D(net.withdraw_fee)
    if a.listed_by:
        fee = setting(db, "listed_withdraw_fee_usd", D) / max(eng.usd_price(db, asset), D("0.00000001"))

    def do():
        ok, reasons = risk.withdrawal_risk(db, u, asset, chain, addr, amt, ip)
        try:
            move(db, u.id, asset, d_avail=-(amt + fee), d_locked=amt + fee, kind="withdraw_lock")
        except InsufficientFunds:
            raise OrderError(f"Insufficient {asset} (amount + fee {js(fee)})")
        w = Withdrawal(user_id=u.id, asset=asset, chain=chain, address=addr, amount=amt, fee=fee,
                       usd_value=amt * eng.usd_price(db, asset), status="pending" if ok else "review", risk_reason="; ".join(reasons))
        db.add(w)
        log_event(db, "withdraw_request", u.id, ip, f"{amt} {asset} {chain} {addr} {w.status}", "info" if ok else "warn")
        db.flush()
        return {"id": w.id, "status": w.status, "reasons": reasons}
    return run(db, do)


# ---------------- spot ----------------
@app.post("/api/order")
def order(body: dict, u=Depends(current_user), db=Depends(get_db)):
    if u.frozen:
        raise HTTPException(403, "Account frozen")
    if not limiter.hit(f"ord:{u.id}", 60, 60):
        raise HTTPException(429, "Too many orders")
    return run(db, lambda: js(eng.place_order(db, u.id, str(body.get("symbol", "")).upper(), body.get("side"), body.get("type", "limit"),
                                              body.get("price"), body.get("qty"), body.get("quote_qty"))))


@app.delete("/api/order/{oid}")
def cancel(oid: int, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(eng.cancel_order(db, u.id, oid)) or {"ok": True})


@app.get("/api/orders")
def orders(symbol: str = "", open: int = 1, u=Depends(current_user), db=Depends(get_db)):
    q = select(Order).where(Order.user_id == u.id)
    if symbol:
        q = q.where(Order.market == symbol.upper())
    q = q.where(Order.status.in_(["open", "partial"])) if open else q.where(Order.status.notin_(["open", "partial"]))
    return [js(o) for o in db.scalars(q.order_by(Order.id.desc()).limit(200))]


# ---------------- trading bots ----------------
@app.get("/api/bots")
def my_bots(u=Depends(current_user), db=Depends(get_db)):
    return [js(bots.view(db, b)) for b in db.scalars(select(bots.Bot).where(bots.Bot.user_id == u.id).order_by(bots.Bot.id.desc()).limit(50))]


@app.post("/api/bots")
def new_bot(body: dict, u=Depends(current_user), db=Depends(get_db)):
    if u.frozen:
        raise HTTPException(403, "Account frozen")
    return run(db, lambda: js(bots.view(db, bots.create(db, u.id, body))))


@app.post("/api/bots/{bid}/stop")
def stop_bot(bid: int, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(bots.view(db, bots.stop(db, u.id, bid))))


@app.get("/api/copy/leaders")
def copy_leaders(db=Depends(get_db)):
    return js(bots.leaders(db))


@app.post("/api/copy/leader")
def copy_leader(body: dict, u=Depends(current_user), db=Depends(get_db)):
    def go():
        cl = db.get(bots.CopyLeader, u.id) or bots.CopyLeader(user_id=u.id)
        cl.enabled = bool(body.get("enabled", True))
        db.add(cl)
        return {"ok": True, "enabled": cl.enabled}
    return run(db, go)


@app.get("/api/convert/quote")
def convert_quote(frm: str, to: str, amount: str, db=Depends(get_db)):
    try:
        rate, out = eng.convert_quote(db, frm.upper(), to.upper(), amount)
    except OrderError as e:
        raise HTTPException(400, str(e))
    return {"rate": js(rate), "receive": js(out)}


@app.post("/api/convert")
def convert(body: dict, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: eng.convert(db, u.id, str(body.get("from", "")).upper(), str(body.get("to", "")).upper(), body.get("amount", "0")))


# ---------------- futures ----------------
@app.post("/api/futures/open")
def fut_open(body: dict, u=Depends(current_user), db=Depends(get_db)):
    if u.frozen:
        raise HTTPException(403, "Account frozen")
    def do():
        p = fut.open_position(db, u.id, str(body.get("symbol", "")).upper(), body.get("side"), body.get("margin", "0"),
                              body.get("leverage", 1), body.get("tp") or None, body.get("sl") or None)
        return fut.position_view(p)
    return run(db, do)


def my_position(db, u, pid):
    p = db.get(Position, pid)
    if not p or p.user_id != u.id or p.status != "open":
        raise HTTPException(404, "Position not found")
    return p


@app.post("/api/futures/close/{pid}")
def fut_close(pid: int, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: fut.position_view(fut.close_position(db, my_position(db, u, pid), "manual") or my_position_any(db, pid)))


def my_position_any(db, pid):
    return db.get(Position, pid)


@app.post("/api/futures/tpsl/{pid}")
def fut_tpsl(pid: int, body: dict, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: fut.set_tpsl(db, my_position(db, u, pid), body.get("tp") or None, body.get("sl") or None) or {"ok": True})


@app.get("/api/futures/positions")
def fut_positions(open: int = 1, u=Depends(current_user), db=Depends(get_db)):
    q = select(Position).where(Position.user_id == u.id, Position.status == "open" if open else Position.status != "open")
    out = []
    for p in db.scalars(q.order_by(Position.id.desc()).limit(100)):
        px = None
        if p.status == "open":
            try:
                px = fut.mark(db, p.market)[1]
            except Exception:
                pass
        out.append(fut.position_view(p, px))
    return out


# ---------------- earn ----------------
@app.get("/api/earn")
def earn_products(db=Depends(get_db)):
    return [js(p) for p in db.scalars(select(EarnProduct).where(EarnProduct.active == True))]


@app.post("/api/earn/subscribe")
def earn_sub(body: dict, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(earn.subscribe(db, u.id, int(body.get("product_id", 0)), body.get("amount", "0"))))


@app.post("/api/earn/redeem/{sid}")
def earn_redeem(sid: int, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(earn.redeem(db, u.id, sid)) or {"ok": True})


@app.get("/api/earn/mine")
def earn_mine(u=Depends(current_user), db=Depends(get_db)):
    return [js(s) for s in db.scalars(select(EarnStake).where(EarnStake.user_id == u.id).order_by(EarnStake.id.desc()))]


# ---------------- P2P ----------------
@app.get("/api/p2p/ads")
def p2p_ads(side: str = "sell", asset: str = "USDT", fiat: str = "TND", db=Depends(get_db)):
    q = select(P2PAd).where(P2PAd.active == True, P2PAd.side == side, P2PAd.asset == asset.upper(), P2PAd.fiat == fiat.upper())
    out = []
    for ad in db.scalars(q.order_by(P2PAd.price if side == "sell" else P2PAd.price.desc()).limit(100)):
        d = js(ad)
        owner = db.get(User, ad.user_id)
        d["nickname"] = owner.nickname if owner else ""
        out.append(d)
    return out


@app.post("/api/p2p/ad")
def p2p_create(body: dict, u=Depends(current_user), db=Depends(get_db)):
    b = body
    return run(db, lambda: js(p2p.create_ad(db, u.id, b.get("side"), str(b.get("asset", "")).upper(), str(b.get("fiat", "TND")).upper(),
                                            b.get("price"), b.get("amount"), b.get("min_fiat"), b.get("max_fiat"),
                                            str(b.get("methods", ""))[:200], str(b.get("terms", ""))[:1000])))


@app.post("/api/p2p/ad/{aid}/close")
def p2p_close(aid: int, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(p2p.close_ad(db, u.id, aid)) or {"ok": True})


@app.post("/api/p2p/take/{aid}")
def p2p_take(aid: int, body: dict, u=Depends(current_user), db=Depends(get_db)):
    return run(db, lambda: js(p2p.take(db, u.id, aid, body.get("fiat_amount"))))


@app.post("/api/p2p/order/{oid}/{action}")
def p2p_action(oid: int, action: str, body: dict = None, u=Depends(current_user), db=Depends(get_db)):
    if action == "release":
        need_2fa(u, (body or {}).get("code"))
    fn = {"paid": p2p.mark_paid, "release": p2p.release, "cancel": p2p.cancel, "dispute": p2p.dispute}.get(action)
    if action == "message":
        return run(db, lambda: js(p2p.add_msg(db, u.id, oid, str((body or {}).get("text", ""))[:1000])) or {"ok": True})
    if not fn:
        raise HTTPException(404, "Unknown action")
    return run(db, lambda: js(fn(db, u.id, oid)) or {"ok": True})


@app.get("/api/p2p/orders")
def p2p_orders(u=Depends(current_user), db=Depends(get_db)):
    q = select(P2POrder).where(or_(P2POrder.buyer == u.id, P2POrder.seller == u.id)).order_by(P2POrder.id.desc()).limit(100)
    return [js(o) for o in db.scalars(q)]


@app.get("/api/p2p/order/{oid}")
def p2p_order(oid: int, u=Depends(current_user), db=Depends(get_db)):
    o = db.get(P2POrder, oid)
    if not o or (u.id not in (o.buyer, o.seller) and not u.is_admin):
        raise HTTPException(404, "Not found")
    ad = db.get(P2PAd, o.ad_id)
    msgs = [js(m) for m in db.scalars(select(P2PMessage).where(P2PMessage.order_id == oid).order_by(P2PMessage.id))]
    return {"order": js(o), "ad": js(ad), "messages": msgs}


# ---------------- Launchpad / listing ----------------
@app.get("/api/launchpad")
def launchpad(db=Depends(get_db)):
    out = []
    for a in db.scalars(select(Asset).where(Asset.listed_by != None, Asset.active == True).order_by(Asset.created_at.desc())):
        d = js(a)
        d["usd"] = js(eng.usd_price(db, a.symbol))
        n = db.scalars(select(AssetNetwork).where(AssetNetwork.asset == a.symbol)).first()
        d["chain"], d["contract"] = (n.chain, n.contract) if n else ("", "")
        out.append(d)
    return out


def require_launchpad(db, u):
    if bal(db, u.id, "ALTF", lock=False).available < 1 and not u.is_admin:
        raise HTTPException(400, "Launchpad needs 1 ALTF in your spot wallet")


@app.post("/api/listing")
def list_token(body: dict, u=Depends(current_user), db=Depends(get_db)):
    chain, contract = str(body.get("chain", "")).upper(), str(body.get("contract", "")).strip()
    if chain not in ("TRON", "BSC") or not valid_address(chain, contract):
        raise HTTPException(400, "Enter the token contract address (TRON or BSC)")
    require_launchpad(db, u)
    try:
        info = listing.token_info(chain, contract)
    except Exception:
        raise HTTPException(400, "Could not read this contract on the blockchain")
    def do():
        a = listing.create_listing(db, u.id, chain, contract, info["name"], info["symbol"], int(info["decimals"]),
                                   str(body.get("website", ""))[:300], str(body.get("description", ""))[:5000], str(body.get("logo", ""))[:2000])
        return js(a)
    return run(db, do)


@app.get("/api/listing/mine")
def my_listings(u=Depends(current_user), db=Depends(get_db)):
    return [js(l) for l in db.scalars(select(Listing).where(Listing.user_id == u.id).order_by(Listing.id.desc()))]


@app.post("/api/listing/{lid}/renew")
def renew_listing(lid: int, u=Depends(current_user), db=Depends(get_db)):
    l = db.get(Listing, lid)
    if not l or l.user_id != u.id:
        raise HTTPException(404, "Not found")
    return run(db, lambda: js(listing.renew(db, l, manual=True)) or {"ok": True})


@app.post("/api/listing/{lid}/auto")
def auto_renew(lid: int, body: dict, u=Depends(current_user), db=Depends(get_db)):
    l = db.get(Listing, lid)
    if not l or l.user_id != u.id:
        raise HTTPException(404, "Not found")
    l.auto_renew = bool(body.get("on"))
    db.commit()
    return {"ok": True}


@app.post("/api/listing/{symbol}/info")
def listing_info(symbol: str, body: dict, u=Depends(current_user), db=Depends(get_db)):
    a = db.get(Asset, symbol.upper())
    if not a or a.listed_by != u.id:
        raise HTTPException(404, "Not found")
    a.website, a.description, a.logo = str(body.get("website", a.website))[:300], str(body.get("description", a.description))[:5000], str(body.get("logo", a.logo))[:2000]
    risk.review_asset(db, a)
    db.commit()
    return js(a)


# ---------------- Token Factory ----------------
@app.get("/api/factory/price")
def factory_price(db=Depends(get_db)):
    return {c: js(listing.factory_price(db, c)) for c in ("TRON", "BSC")}


@app.post("/api/factory")
def factory_order(body: dict, u=Depends(current_user), db=Depends(get_db)):
    b = body
    if u.frozen:
        raise HTTPException(403, "Account frozen")
    if not limiter.hit(f"fac:{u.id}", 5, 86400):
        raise HTTPException(429, "Limit: 5 tokens per day")
    j = run(db, lambda: listing.order_token(db, u.id, str(b.get("chain", "")).upper(), str(b.get("name", "")), str(b.get("symbol", "")),
                                            b.get("supply", "0"), b.get("decimals", 6), str(b.get("owner", "")).strip(),
                                            str(b.get("website", "")), str(b.get("description", "")), str(b.get("logo", "")), bool(b.get("list_it"))))
    listing.start_job(j.id)
    return js(j)


@app.get("/api/factory/mine")
def factory_mine(u=Depends(current_user), db=Depends(get_db)):
    return [js(j) for j in db.scalars(select(TokenJob).where(TokenJob.user_id == u.id).order_by(TokenJob.id.desc()))]


# ---------------- partner servers ----------------
@app.post("/api/servers")
def server_register(body: dict, u=Depends(current_user), db=Depends(get_db)):
    from tronpy.keys import is_base58check_address
    addr, url = str(body.get("address", "")).strip(), str(body.get("url", "")).strip()
    if not is_base58check_address(addr) or not re.match(r"^https?://[\w.\-:]+/?$", url):
        raise HTTPException(400, "Enter the node TRON address and server URL like http://1.2.3.4:8787")
    if db.scalars(select(ServerNode).where(ServerNode.address == addr)).first():
        raise HTTPException(400, "This node is already registered")
    try:
        ok = servers.probe(url, addr)
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(400, "Your server did not answer. Start altaf_node.py and open its port")
    n = ServerNode(user_id=u.id, address=addr, url=url, up_since=datetime.utcnow(), last_ok=datetime.utcnow())
    db.add(n)
    db.commit()
    return js(n)


@app.get("/api/servers")
def server_list(db=Depends(get_db)):
    rows = db.scalars(select(ServerNode).where(ServerNode.status.in_(["pending", "active"])).order_by(ServerNode.id)).all()
    return {"max": 16, "active": sum(1 for r in rows if r.status == "active"),
            "servers": [{"address": r.address, "status": r.status, "since": js(r.created_at), "last_ok": js(r.last_ok)} for r in rows]}


# ---------------- partner voting ----------------
@app.get("/api/gov")
def gov_list(u=Depends(current_user), db=Depends(get_db)):
    me = gov.info(db, u)
    if not me["role"]:
        raise HTTPException(403, "Voting is for partners with an active ALTAF server")
    def go():
        ps = db.scalars(select(gov.Proposal).order_by(gov.Proposal.id.desc()).limit(50)).all()
        return {"me": me, "proposals": [js(gov.view(db, p, u)) for p in ps]}
    return run(db, go)


@app.post("/api/gov")
def gov_new(body: dict, u=Depends(current_user), db=Depends(get_db)):
    need_2fa(u, body.get("code"))
    return run(db, lambda: js(gov.view(db, gov.propose(db, u, body.get("title"), body.get("body")), u)))


@app.post("/api/gov/{pid}/vote")
def gov_vote(pid: int, body: dict, u=Depends(current_user), db=Depends(get_db)):
    need_2fa(u, body.get("code"))
    return run(db, lambda: js(gov.view(db, gov.vote(db, u, pid, body.get("choice")), u)))


@app.post("/api/gov/{pid}/veto")
def gov_veto(pid: int, body: dict, u=Depends(current_user), db=Depends(get_db)):
    need_2fa(u, body.get("code"))
    return run(db, lambda: js(gov.view(db, gov.veto(db, u, pid), u)))


@app.get("/api/partner/withdrawals")
def partner_withdrawals(u=Depends(current_user), db=Depends(get_db)):
    try:
        return gov.review_withdrawals(db, u)
    except OrderError as e:
        raise HTTPException(403, str(e))


@app.post("/api/partner/withdrawals/{wid}/approve")
def partner_withdraw_approve(wid: int, body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    need_2fa(u, body.get("code"))
    def do():
        r = gov.approve_withdrawal(db, u, wid, setting(db, "partner_approvals_needed", int))
        log_event(db, "partner_withdraw_approve", u.id, ip_of(request), f"{wid} -> {r['status']}", "warn")
        return r
    return run(db, do)


@app.get("/api/node/altaf_node.py")
def node_download():
    return FileResponse(Path(__file__).resolve().parent.parent / "node" / "altaf_node.py", filename="altaf_node.py")


# ---------------- admin ----------------
@app.get("/api/admin/stats")
def admin_stats(a=Depends(admin_user), db=Depends(get_db)):
    fees = house_id(db, "fees")
    house = house_id(db)
    def wallet(uid):
        return [{"wallet": b.wallet, "asset": b.asset, "available": js(b.available), "locked": js(b.locked)}
                for b in db.scalars(select(Balance).where(Balance.user_id == uid))]
    return {"users": db.scalar(select(func.count(User.id)).where(User.is_system == False)),
            "open_positions": db.scalar(select(func.count(Position.id)).where(Position.status == "open")),
            "pending_withdrawals": db.scalar(select(func.count(Withdrawal.id)).where(Withdrawal.status == "review")),
            "fees": wallet(fees), "house": wallet(house), "open_interest": js(fut.open_interest(db))}


@app.get("/api/admin/reserves")
def admin_reserves(a=Depends(admin_user), db=Depends(get_db)):
    from . import chain as ch
    try:
        return ch.reserves(db)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.get("/api/admin/users")
def admin_users(q: str = "", a=Depends(admin_user), db=Depends(get_db)):
    s = select(User).where(User.is_system == False)
    if q:
        s = s.where(User.email.ilike(f"%{q}%"))
    return [js(u) for u in db.scalars(s.order_by(User.id.desc()).limit(200))]


@app.post("/api/admin/users/{uid}/freeze")
def admin_freeze(uid: int, body: dict, request: Request, a=Depends(admin_user), db=Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.is_system:
        raise HTTPException(404, "Not found")
    u.frozen = bool(body.get("on"))
    u.token_version += 1
    log_event(db, "admin_freeze", uid, ip_of(request), f"by {a.email} -> {u.frozen}", "warn")
    db.commit()
    return {"ok": True}


@app.get("/api/admin/withdrawals")
def admin_withdrawals(status: str = "review", a=Depends(admin_user), db=Depends(get_db)):
    out = []
    for w in db.scalars(select(Withdrawal).where(Withdrawal.status == status).order_by(Withdrawal.id.desc()).limit(200)):
        d = js(w)
        d["email"] = db.get(User, w.user_id).email
        out.append(d)
    return out


@app.post("/api/admin/withdrawals/{wid}/{action}")
def admin_withdraw_action(wid: int, action: str, body: dict, request: Request, a=Depends(admin_user), db=Depends(get_db)):
    need_2fa(a, body.get("code"))
    w = db.get(Withdrawal, wid)
    if not w or w.status != "review":
        raise HTTPException(400, "Not waiting for review")
    def do():
        if action == "approve":
            w.status = "pending"
        elif action == "reject":
            w.status, w.error = "rejected", str(body.get("reason", "rejected"))[:300]
            move(db, w.user_id, w.asset, d_locked=-(D(w.amount) + D(w.fee)), d_avail=D(w.amount) + D(w.fee), kind="withdraw_refund", ref=str(w.id))
        else:
            raise OrderError("Unknown action")
        log_event(db, "admin_withdraw_" + action, w.user_id, ip_of(request), f"{wid} by {a.email}", "warn")
        return {"ok": True}
    return run(db, do)


@app.get("/api/admin/settings")
def admin_settings(a=Depends(admin_user), db=Depends(get_db)):
    return all_settings(db)


@app.post("/api/admin/settings")
def admin_set(body: dict, request: Request, a=Depends(admin_user), db=Depends(get_db)):
    need_2fa(a, body.pop("code", None))
    for k, v in body.items():
        set_setting(db, str(k), str(v))
    log_event(db, "admin_settings", a.id, ip_of(request), str(body), "warn")
    db.commit()
    return all_settings(db)


@app.post("/api/admin/emergency")
def admin_emergency(body: dict, request: Request, a=Depends(admin_user), db=Depends(get_db)):
    on = bool(body.get("on"))
    for k in ("withdrawals_enabled", "deposits_enabled"):
        set_setting(db, k, "0" if on else "1")
    if on:
        set_setting(db, "futures_enabled", "0")
        mm.stop(db, "emergency stop")
    log_event(db, "emergency_stop" if on else "emergency_release", a.id, ip_of(request), a.email, "critical")
    db.commit()
    return all_settings(db)


@app.get("/api/admin/mm")
def admin_mm(a=Depends(admin_user), db=Depends(get_db)):
    return js(mm.view(db))


@app.post("/api/admin/mm/{action}")
def admin_mm_action(action: str, body: dict, request: Request, a=Depends(admin_user), db=Depends(get_db)):
    need_2fa(a, body.get("code"))
    def do():
        if action == "start":
            mm.start(db)
        elif action == "stop":
            mm.stop(db)
        elif action == "fund":
            mm.fund(db, a.id, str(body.get("direction")), str(body.get("asset", "USDT")).upper(), body.get("amount", "0"))
        else:
            raise OrderError("Unknown action")
        log_event(db, "admin_mm_" + action, a.id, ip_of(request), str({k: v for k, v in body.items() if k != "code"}), "warn")
        return js(mm.view(db))
    return run(db, do)


@app.post("/api/admin/assets/{symbol}/risk")
def admin_risk(symbol: str, body: dict, a=Depends(admin_user), db=Depends(get_db)):
    x = db.get(Asset, symbol.upper())
    if not x:
        raise HTTPException(404, "Not found")
    lvl = body.get("level")
    if lvl not in ("ok", "watch", "danger", "banned"):
        raise HTTPException(400, "Bad level")
    x.risk_level, x.risk_notes = lvl, f"set by admin: {str(body.get('note', ''))[:300]}"
    if lvl == "banned":
        listing.delist(db, x.symbol, "banned")
    db.commit()
    return js(x)


@app.get("/api/admin/tokens")
def admin_tokens(a=Depends(admin_user), db=Depends(get_db)):
    return {"listed": [js(x) for x in db.scalars(select(Asset).where(Asset.listed_by != None))],
            "factory": [js(j) for j in db.scalars(select(TokenJob).order_by(TokenJob.id.desc()).limit(200))]}


@app.get("/api/admin/events")
def admin_events(a=Depends(admin_user), db=Depends(get_db)):
    return [js(e) for e in db.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(300))]


@app.get("/api/admin/servers")
def admin_servers(a=Depends(admin_user), db=Depends(get_db)):
    return [js(n) for n in db.scalars(select(ServerNode).order_by(ServerNode.id.desc()))]


@app.get("/api/admin/p2p")
def admin_p2p(a=Depends(admin_user), db=Depends(get_db)):
    return [js(o) for o in db.scalars(select(P2POrder).where(P2POrder.status == "dispute"))]


@app.post("/api/admin/p2p/{oid}/{action}")
def admin_p2p_action(oid: int, action: str, a=Depends(admin_user), db=Depends(get_db)):
    fn = {"release": p2p.release, "cancel": p2p.cancel}.get(action)
    if not fn:
        raise HTTPException(404, "Unknown")
    return run(db, lambda: js(fn(db, a.id, oid, admin=True)) or {"ok": True})


# ---------------- proof of reserves ----------------
_RES = {"t": 0, "v": None}


@app.get("/api/reserves")
def reserves(db=Depends(get_db)):
    if _RES["v"] and time.time() - _RES["t"] < 600:
        return _RES["v"]
    from . import chain as ch
    sys_ids = select(User.id).where(User.is_system == True)  # noqa: E712
    rows = db.execute(select(Balance.asset, func.sum(Balance.available + Balance.locked))
                      .where(Balance.user_id.notin_(sys_ids)).group_by(Balance.asset)).all()
    liab = {a: D(str(v or 0)) for a, v in rows}
    hold, wallets = {}, {}
    for n in db.scalars(select(AssetNetwork).where(AssetNetwork.active == True)):  # noqa: E712
        try:
            addr = ch.hot_key(n.chain)[0]
            b = D(str(ch.hot_balance(n.chain, n.asset, n)))
        except Exception:
            continue
        hold[n.asset] = hold.get(n.asset, ZERO) + b
        wallets.setdefault(n.chain, addr)
    out = {"at": int(time.time()), "wallets": wallets, "assets": [
        {"asset": a, "users": str(liab.get(a, ZERO)), "on_chain": str(hold.get(a, ZERO)),
         "ratio": str(round(hold.get(a, ZERO) / liab[a] * 100, 2)) if liab.get(a) else None}
        for a in sorted(set(liab) | set(hold))]}
    _RES.update(t=time.time(), v=out)
    return out


# ---------------- exchange API keys + market WebSocket ----------------
@app.get("/api/apikeys")
def apikeys_list(u=Depends(current_user), db=Depends(get_db)):
    q = select(apikeys.ApiKey).where(apikeys.ApiKey.user_id == u.id, apikeys.ApiKey.revoked == False)  # noqa: E712
    return [apikeys.view(k) for k in db.scalars(q.order_by(apikeys.ApiKey.id.desc()))]


@app.post("/api/apikeys")
def apikeys_create(body: dict, request: Request, u=Depends(current_user), db=Depends(get_db)):
    need_2fa(u, body.get("code"))
    try:
        r = apikeys.create(db, u.id, str(body.get("label", "")), body.get("can_trade", True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    log_event(db, "apikey_create", u.id, ip_of(request), r["key"]); db.commit()
    return r


@app.delete("/api/apikeys/{kid}")
def apikeys_delete(kid: int, u=Depends(current_user), db=Depends(get_db)):
    k = db.get(apikeys.ApiKey, kid)
    if not k or k.user_id != u.id:
        raise HTTPException(404, "Not found")
    k.revoked = True
    db.commit()
    return {"ok": True}


@app.websocket("/ws/market/{symbol}")
async def ws_market(ws: WebSocket, symbol: str):
    await apikeys.market_ws(ws, symbol.upper(), lambda db, s: js(eng.book(db, s)), eng.recent_trades)


# ---------------- static SPA ----------------
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    return FileResponse(STATIC / "index.html")
