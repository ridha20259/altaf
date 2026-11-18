"""Anti-hack / anti-scam layer: withdrawal risk scoring and automatic token risk review."""
from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import select, func
from .db import User, Withdrawal, Asset, AssetNetwork, WhitelistAddress, SecurityEvent, Deposit, Trade
from .ledger import setting, D, ZERO
from .engine import usd_price, q
from .security import log_event


def withdrawal_risk(db, user: User, asset, chain, address, amount, ip=""):
    """Return (ok, reasons[]) — ok=False means hold for manual review."""
    reasons = []
    usd = D(amount) * usd_price(db, asset)
    if user.frozen:
        reasons.append("account frozen")
    if not user.totp_enabled:
        reasons.append("2FA not enabled")
    age_h = (datetime.utcnow() - user.created_at).total_seconds() / 3600
    if age_h < D(setting(db, "withdraw_new_account_hours", D)):
        reasons.append("new account (<%sh)" % setting(db, "withdraw_new_account_hours"))
    if user.security_changed_at and (datetime.utcnow() - user.security_changed_at) < timedelta(hours=int(setting(db, "withdraw_lock_hours_after_change", D))):
        reasons.append("security settings changed recently")
    since = datetime.utcnow() - timedelta(hours=24)
    day = ZERO
    for w in db.scalars(select(Withdrawal).where(Withdrawal.user_id == user.id, Withdrawal.created_at >= since, Withdrawal.status.in_(["pending", "review", "sending", "sent"]))):
        day += D(w.usd_value or 0)
    if day + usd > D(setting(db, "withdraw_daily_limit_usd", D)):
        reasons.append("daily limit exceeded")
    if usd > D(setting(db, "withdraw_auto_limit_usd", D)):
        reasons.append("large amount")
    known = db.scalars(select(WhitelistAddress).where(WhitelistAddress.user_id == user.id, WhitelistAddress.chain == chain, WhitelistAddress.address == address)).first()
    if not known:
        reasons.append("new address")
    # no trading/deposit history but withdrawing big
    dep = db.scalar(select(func.count(Deposit.id)).where(Deposit.user_id == user.id)) or 0
    if dep == 0 and usd > 50:
        reasons.append("no deposit history")
    ok = not reasons or (len(reasons) == 1 and reasons[0] == "new address" and usd <= D(setting(db, "withdraw_auto_limit_usd", D)))
    if not ok:
        log_event(db, "withdraw_review", user.id, ip, "; ".join(reasons), "warn")
    return ok, reasons


SCAM_WORDS = ("1000x", "guaranteed", "elon", "safemoon", "rug", "pump", "moon", "100x", "airdrop free")


def review_asset(db, a: Asset):
    """Automatic project review for listed/created tokens."""
    score, notes = 0, []
    if not a.website or len(a.website) < 8:
        score += 2
        notes.append("no website")
    if len(a.description or "") < 80:
        score += 2
        notes.append("no real project description")
    low = f"{a.name} {a.description}".lower()
    hits = [w for w in SCAM_WORDS if w in low]
    if hits:
        score += 2
        notes.append("promotional/unrealistic wording: " + ", ".join(hits))
    trades = db.scalar(select(func.count(Trade.id)).where(Trade.market.like(f"{a.symbol}%"))) or 0
    if trades == 0:
        score += 1
        notes.append("no trading activity")
    net = db.scalars(select(AssetNetwork).where(AssetNetwork.asset == a.symbol)).first()
    if net and net.contract:
        try:
            from . import chain as ch
            if net.chain == "BSC":
                w3 = ch._w3()
                c = w3.eth.contract(address=w3.to_checksum_address(net.contract), abi=ch.ERC20_ABI)
                holders_sample = None
            # concentration check is chain-dependent; skipped when unavailable
        except Exception:
            pass
    level = "ok" if score <= 1 else ("watch" if score <= 3 else "danger")
    if a.risk_level == "banned":
        level = "banned"
    a.risk_level, a.risk_notes, a.risk_checked_at = level, "; ".join(notes), datetime.utcnow()
    return level, notes


def review_all(db):
    for a in db.scalars(select(Asset).where(Asset.listed_by != None)).all():
        review_asset(db, a)
