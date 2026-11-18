"""Referral program: every user gets a code; inviters earn a share of their invitees' trading fees.
The same code works on the website (#/register/CODE) and in the Telegram bot (?start=CODE)."""
import secrets
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Integer, String, DateTime, select, func
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, Ledger
from .ledger import transfer, house_id

RATE = Decimal("0.20")
KIND = "ref_commission"
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class Referral(Base):
    __tablename__ = "referrals"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    referred_by: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


def row(db, uid):
    r = db.get(Referral, uid)
    if r:
        return r
    while True:
        code = "".join(secrets.choice(_ALPHA) for _ in range(8))
        if not db.scalars(select(Referral).where(Referral.code == code)).first():
            break
    r = Referral(user_id=uid, code=code)
    db.add(r)
    db.flush()
    return r


def attach(db, uid, code):
    """Link a new user to the owner of `code` (once, never to themselves)."""
    r = row(db, uid)
    code = (code or "").strip().upper()
    if not code or r.referred_by:
        return False
    inv = db.scalars(select(Referral).where(Referral.code == code)).first()
    if not inv or inv.user_id == uid:
        return False
    r.referred_by = inv.user_id
    return True


def pay(db, uid, asset, fee, ref):
    """Give the inviter RATE of a fee the invitee just paid into the fees account."""
    r = db.get(Referral, uid)
    if not r or not r.referred_by or fee <= 0:
        return
    c = (Decimal(fee) * RATE).quantize(Decimal("0.00000001"))
    if c > 0:
        transfer(db, house_id(db, "fees"), r.referred_by, asset, c, KIND, ref)


def stats(db, uid):
    r = row(db, uid)
    invited = db.scalar(select(func.count()).select_from(Referral).where(Referral.referred_by == uid)) or 0
    earned = {a: str(v) for a, v in db.execute(select(Ledger.asset, func.sum(Ledger.d_available))
                                               .where(Ledger.user_id == uid, Ledger.kind == KIND).group_by(Ledger.asset))}
    return {"code": r.code, "invited": invited, "earned": earned, "rate": str(RATE)}
