"""Partner voting: active server owners only, 1 active server = 1 vote.
Founder has a veto; any 2 founding partners (owners of the 4 oldest active servers) together also veto."""
from datetime import datetime, timedelta
from sqlalchemy import Integer, String, Text, DateTime, select, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, User, now
from .config import ADMIN_EMAILS
from .engine import OrderError
from .servers import ServerNode

VOTE_DAYS = 7
FOUNDING_SEATS = 4
VETOES_NEEDED = 2


class Proposal(Base):
    __tablename__ = "gov_proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    author_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|passed|rejected|vetoed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Vote(Base):
    __tablename__ = "gov_votes"
    __table_args__ = (UniqueConstraint("proposal_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    choice: Mapped[str] = mapped_column(String(4))  # yes|no
    weight: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Veto(Base):
    __tablename__ = "gov_vetoes"
    __table_args__ = (UniqueConstraint("proposal_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))  # founder|founding
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def is_founder(u: User):
    return bool(ADMIN_EMAILS) and u.email.lower() == ADMIN_EMAILS[0]


def _active(db):
    return db.scalars(select(ServerNode).where(ServerNode.status == "active").order_by(ServerNode.id)).all()


def founding_partners(db):
    """Owners of the oldest active servers, in opening order; a closed server's seat passes to the next one."""
    out = []
    for n in _active(db):
        if n.user_id not in out:
            out.append(n.user_id)
        if len(out) == FOUNDING_SEATS:
            break
    return out


def weight(db, uid):
    return sum(1 for n in _active(db) if n.user_id == uid)


def role(db, u: User):
    if is_founder(u):
        return "founder"
    if u.id in founding_partners(db):
        return "founding"
    return "partner" if weight(db, u.id) else None


def require(db, u: User):
    r = role(db, u)
    if not r:
        raise OrderError("Voting is for partners with an active ALTAF server")
    if not u.totp_enabled:
        raise OrderError("Turn on 2FA to use partner voting")
    return r


def _finish(db, p: Proposal):
    if p.status != "open":
        return
    vetoes = db.scalars(select(Veto).where(Veto.proposal_id == p.id)).all()
    if any(v.role == "founder" for v in vetoes) or sum(1 for v in vetoes if v.role == "founding") >= VETOES_NEEDED:
        p.status, p.closed_at = "vetoed", now()
    elif now() >= p.ends_at:
        t = tally(db, p)
        p.status, p.closed_at = ("passed" if t["yes"] > t["no"] else "rejected"), now()


def tally(db, p):
    vs = db.scalars(select(Vote).where(Vote.proposal_id == p.id)).all()
    return {"yes": sum(v.weight for v in vs if v.choice == "yes"), "no": sum(v.weight for v in vs if v.choice == "no")}


def propose(db, u, title, body):
    require(db, u)
    title = (title or "").strip()
    if not 5 <= len(title) <= 150:
        raise OrderError("Title must be 5-150 characters")
    if db.scalars(select(Proposal).where(Proposal.author_id == u.id, Proposal.status == "open")).all().__len__() >= 3:
        raise OrderError("You already have 3 open proposals")
    p = Proposal(author_id=u.id, title=title, body=(body or "").strip()[:4000], ends_at=now() + timedelta(days=VOTE_DAYS))
    db.add(p); db.flush()
    return p


def _open(db, pid):
    p = db.get(Proposal, int(pid))
    if not p:
        raise OrderError("Proposal not found")
    _finish(db, p)
    if p.status != "open":
        raise OrderError("Voting on this proposal is closed")
    return p


def vote(db, u, pid, choice):
    require(db, u)
    if choice not in ("yes", "no"):
        raise OrderError("Choose yes or no")
    p = _open(db, pid)
    w = weight(db, u.id)
    if w == 0:
        raise OrderError("Only active servers carry votes")
    if db.scalars(select(Vote).where(Vote.proposal_id == p.id, Vote.user_id == u.id)).first():
        raise OrderError("You already voted; votes cannot be changed")
    db.add(Vote(proposal_id=p.id, user_id=u.id, choice=choice, weight=w)); db.flush()
    return p


def veto(db, u, pid):
    r = require(db, u)
    if r not in ("founder", "founding"):
        raise OrderError("Only the founder or a founding partner can veto")
    p = _open(db, pid)
    if db.scalars(select(Veto).where(Veto.proposal_id == p.id, Veto.user_id == u.id)).first():
        raise OrderError("You already vetoed this proposal")
    db.add(Veto(proposal_id=p.id, user_id=u.id, role=r)); db.flush()
    _finish(db, p)
    return p


def view(db, p, u=None):
    _finish(db, p)
    nick = lambda uid: (db.get(User, uid).nickname if db.get(User, uid) else "?")
    vs = db.scalars(select(Vote).where(Vote.proposal_id == p.id)).all()
    ve = db.scalars(select(Veto).where(Veto.proposal_id == p.id)).all()
    mine = next((v.choice for v in vs if u and v.user_id == u.id), None)
    return {"id": p.id, "title": p.title, "body": p.body, "author": nick(p.author_id), "status": p.status,
            "created": p.created_at, "ends": p.ends_at, **tally(db, p), "voters": len(vs), "my_vote": mine,
            "vetoes": [{"by": nick(v.user_id), "role": v.role} for v in ve],
            "my_veto": any(u and v.user_id == u.id for v in ve)}


def info(db, u):
    fp = founding_partners(db)
    return {"role": role(db, u), "weight": weight(db, u.id), "totp": u.totp_enabled, "days": VOTE_DAYS,
            "total_votes": len(_active(db)),
            "founding": [db.get(User, x).nickname for x in fp if db.get(User, x)], "vetoes_needed": VETOES_NEEDED}


class WithdrawalApproval(Base):
    __tablename__ = "withdrawal_approvals"
    __table_args__ = (UniqueConstraint("withdrawal_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    withdrawal_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def review_withdrawals(db, u):
    """Withdrawals waiting for review, as seen by a partner (founder approves from the admin page)."""
    from .db import Withdrawal
    require(db, u)
    out = []
    for w in db.scalars(select(Withdrawal).where(Withdrawal.status == "review").order_by(Withdrawal.id.desc()).limit(100)):
        ids = [a.user_id for a in db.scalars(select(WithdrawalApproval).where(WithdrawalApproval.withdrawal_id == w.id))]
        out.append({"id": w.id, "asset": w.asset, "chain": w.chain, "amount": str(w.amount), "usd_value": str(w.usd_value),
                    "reason": w.risk_reason, "created_at": w.created_at.isoformat(), "approvals": len(ids),
                    "mine": w.user_id == u.id, "approved": u.id in ids})
    return out


def approve_withdrawal(db, u, wid, needed=2):
    """Founder alone, or `needed` distinct partners with an active server; nobody counts for their own withdrawal."""
    from .db import Withdrawal
    r = require(db, u)
    w = db.get(Withdrawal, wid)
    if not w or w.status != "review":
        raise OrderError("Not waiting for review")
    if w.user_id == u.id:
        raise OrderError("You cannot approve your own withdrawal")
    if not db.scalars(select(WithdrawalApproval).where(WithdrawalApproval.withdrawal_id == wid, WithdrawalApproval.user_id == u.id)).first():
        db.add(WithdrawalApproval(withdrawal_id=wid, user_id=u.id))
        db.flush()
    partners = [a.user_id for a in db.scalars(select(WithdrawalApproval).where(WithdrawalApproval.withdrawal_id == wid))
                if a.user_id != w.user_id and weight(db, a.user_id)]
    if r == "founder" or len(set(partners)) >= needed:
        w.status = "pending"
    return {"id": wid, "status": w.status, "approvals": len(set(partners))}
