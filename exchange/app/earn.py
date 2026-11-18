from datetime import datetime, timedelta
from sqlalchemy import select
from .db import EarnProduct, EarnStake
from .ledger import move, transfer, house_id, bal, InsufficientFunds, D, ZERO
from .engine import q, OrderError


def subscribe(db, uid, pid, amount):
    p = db.get(EarnProduct, pid)
    amount = D(amount)
    if not p or not p.active:
        raise OrderError("Product not available")
    if amount <= 0 or amount < D(p.min_amount):
        raise OrderError(f"Minimum {p.min_amount} {p.asset}")
    if p.cap and D(p.cap) > 0:
        total = sum((D(s.amount) for s in db.scalars(select(EarnStake).where(EarnStake.product_id == pid, EarnStake.status == "active"))), ZERO)
        if total + amount > D(p.cap):
            raise OrderError("Product is full")
    try:
        move(db, uid, p.asset, d_avail=-amount, kind="earn_sub")
    except InsufficientFunds:
        raise OrderError(f"Insufficient {p.asset}")
    move(db, uid, p.asset, d_locked=amount, kind="earn_sub", wallet="earn")
    s = EarnStake(user_id=uid, product_id=pid, asset=p.asset, amount=amount, earned=ZERO, status="active",
                  unlock_at=datetime.utcnow() + timedelta(days=p.lock_days) if p.lock_days else None)
    db.add(s)
    db.flush()
    return s


def redeem(db, uid, sid):
    s = db.get(EarnStake, sid)
    if not s or s.user_id != uid or s.status != "active":
        raise OrderError("Not found")
    if s.unlock_at and s.unlock_at > datetime.utcnow():
        raise OrderError(f"Locked until {s.unlock_at:%Y-%m-%d}")
    accrue_one(db, s)
    move(db, uid, s.asset, d_locked=-D(s.amount), kind="earn_redeem", wallet="earn")
    move(db, uid, s.asset, d_avail=D(s.amount), kind="earn_redeem")
    s.status = "redeemed"
    return s


def accrue_one(db, s):
    p = db.get(EarnProduct, s.product_id)
    now = datetime.utcnow()
    secs = (now - s.last_accrual).total_seconds()
    if secs < 60:
        return
    reward = q(D(s.amount) * D(p.apr) / 100 * D(secs) / D(365 * 86400), 8)
    h = house_id(db)
    if reward > 0 and bal(db, h, s.asset).available >= reward:
        transfer(db, h, uid_ := s.user_id, s.asset, reward, "earn_reward", f"s{s.id}")
        s.earned = D(s.earned) + reward
    s.last_accrual = now


def accrue_all(db):
    for s in db.scalars(select(EarnStake).where(EarnStake.status == "active")).all():
        accrue_one(db, s)
