from datetime import datetime, timedelta
from sqlalchemy import select, or_
from .db import P2PAd, P2POrder, P2PMessage
from .ledger import move, setting, D, ZERO, InsufficientFunds
from .engine import q, OrderError

W = "p2p"


def create_ad(db, uid, side, asset, fiat, price, amount, min_fiat, max_fiat, methods, terms):
    price, amount = D(price), D(amount)
    if side not in ("buy", "sell") or price <= 0 or amount <= 0:
        raise OrderError("Bad ad")
    if side == "sell":  # escrow the crypto
        try:
            move(db, uid, asset, d_avail=-amount, kind="p2p_ad_lock")
        except InsufficientFunds:
            raise OrderError(f"Insufficient {asset}")
        move(db, uid, asset, d_locked=amount, kind="p2p_ad_lock", wallet=W)
    ad = P2PAd(user_id=uid, side=side, asset=asset, fiat=fiat or "TND", price=price, amount_left=amount,
               min_fiat=D(min_fiat or 0), max_fiat=D(max_fiat or 0), methods=methods[:300], terms=terms[:2000])
    db.add(ad)
    db.flush()
    return ad


def close_ad(db, uid, ad_id):
    ad = db.get(P2PAd, ad_id)
    if not ad or ad.user_id != uid or not ad.active:
        raise OrderError("Not found")
    if db.scalars(select(P2POrder).where(P2POrder.ad_id == ad_id, P2POrder.status.in_(["pending", "paid", "dispute"]))).first():
        raise OrderError("Finish open orders first")
    if ad.side == "sell" and ad.amount_left > 0:
        move(db, uid, ad.asset, d_locked=-D(ad.amount_left), kind="p2p_ad_unlock", wallet=W)
        move(db, uid, ad.asset, d_avail=D(ad.amount_left), kind="p2p_ad_unlock")
    ad.amount_left = ZERO
    ad.active = False


def take(db, uid, ad_id, fiat_amount):
    ad = db.get(P2PAd, ad_id)
    if not ad or not ad.active:
        raise OrderError("Ad not available")
    if ad.user_id == uid:
        raise OrderError("This is your ad")
    fiat_amount = D(fiat_amount)
    if ad.min_fiat and fiat_amount < ad.min_fiat or ad.max_fiat and fiat_amount > ad.max_fiat:
        raise OrderError(f"Limit {ad.min_fiat} - {ad.max_fiat} {ad.fiat}")
    amount = q(fiat_amount / D(ad.price), 6)
    if amount <= 0 or amount > D(ad.amount_left):
        raise OrderError("Amount not available")
    if ad.side == "sell":
        buyer, seller = uid, ad.user_id
        ad.amount_left = D(ad.amount_left) - amount  # already escrowed in seller's p2p wallet
    else:
        buyer, seller = ad.user_id, uid
        try:
            move(db, uid, ad.asset, d_avail=-amount, kind="p2p_lock")
        except InsufficientFunds:
            raise OrderError(f"Insufficient {ad.asset}")
        move(db, uid, ad.asset, d_locked=amount, kind="p2p_lock", wallet=W)
        ad.amount_left = D(ad.amount_left) - amount
    if ad.amount_left <= 0:
        ad.active = False
    o = P2POrder(ad_id=ad.id, buyer=buyer, seller=seller, asset=ad.asset, amount=amount, price=ad.price, fiat=ad.fiat,
                 fiat_amount=fiat_amount, status="pending",
                 expires_at=datetime.utcnow() + timedelta(minutes=int(setting(db, "p2p_timeout_min", D))))
    db.add(o)
    db.flush()
    return o


def _return_escrow(db, o):
    ad = db.get(P2PAd, o.ad_id)
    if ad.side == "sell" and ad.user_id == o.seller:
        ad.amount_left = D(ad.amount_left) + D(o.amount)
        if not ad.active and ad.amount_left > 0:
            ad.active = True
    else:
        move(db, o.seller, o.asset, d_locked=-D(o.amount), kind="p2p_unlock", ref=f"p2p{o.id}", wallet=W)
        move(db, o.seller, o.asset, d_avail=D(o.amount), kind="p2p_unlock", ref=f"p2p{o.id}")
        ad.amount_left = D(ad.amount_left) + D(o.amount)


def mark_paid(db, uid, oid):
    o = db.get(P2POrder, oid)
    if not o or o.buyer != uid or o.status != "pending":
        raise OrderError("Not allowed")
    o.status = "paid"


def release(db, uid, oid, admin=False):
    o = db.get(P2POrder, oid)
    if not o or (o.seller != uid and not admin) or o.status not in ("paid", "dispute", "pending" if admin else "paid"):
        raise OrderError("Not allowed")
    move(db, o.seller, o.asset, d_locked=-D(o.amount), kind="p2p_release", ref=f"p2p{o.id}", wallet=W)
    move(db, o.buyer, o.asset, d_avail=D(o.amount), kind="p2p_release", ref=f"p2p{o.id}")
    o.status = "released"


def cancel(db, uid, oid, admin=False):
    o = db.get(P2POrder, oid)
    if not o or o.status not in (("pending",) if not admin else ("pending", "paid", "dispute")):
        raise OrderError("Not allowed")
    if not admin and o.buyer != uid:
        raise OrderError("Only the buyer can cancel")
    _return_escrow(db, o)
    o.status = "cancelled"


def dispute(db, uid, oid):
    o = db.get(P2POrder, oid)
    if not o or uid not in (o.buyer, o.seller) or o.status not in ("paid",):
        raise OrderError("Not allowed")
    o.status = "dispute"


def expire(db):
    for o in db.scalars(select(P2POrder).where(P2POrder.status == "pending", P2POrder.expires_at < datetime.utcnow())).all():
        _return_escrow(db, o)
        o.status = "cancelled"


def add_msg(db, uid, oid, text, admin=False):
    o = db.get(P2POrder, oid)
    if not o or (uid not in (o.buyer, o.seller) and not admin):
        raise OrderError("Not allowed")
    db.add(P2PMessage(order_id=oid, user_id=uid, text=text[:1000]))
