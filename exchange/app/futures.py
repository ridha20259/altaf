"""USDT-margined perpetual futures, isolated margin. The house futures pool is the counterparty."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy import select, func
from .db import Position, Market
from .ledger import move, setting, house_id, bal, InsufficientFunds, D, ZERO
from .engine import market_price, q, OrderError
from . import prices

W = "futures"


def mark(db, symbol):
    m = db.get(Market, symbol)
    if not m or not m.futures or not m.active:
        raise OrderError("Futures market not available")
    p = prices.ext_price(symbol) if m.external else market_price(db, m)
    if not p:
        raise OrderError("Price feed unavailable")
    return m, D(p)


def liq_price(side, entry, lev):
    mmr = D("0.005")
    if side == "long":
        return entry * (1 - D(1) / lev + mmr)
    return entry * (1 + D(1) / lev - mmr)


def open_interest(db):
    rows = db.scalars(select(Position).where(Position.status == "open")).all()
    return sum((D(p.size) * D(p.entry) for p in rows), ZERO)


def open_position(db, uid, symbol, side, margin, leverage, tp=None, sl=None):
    if not setting(db, "futures_enabled", bool):
        raise OrderError("Futures trading opens soon")
    if side not in ("long", "short"):
        raise OrderError("Bad side")
    margin, leverage = D(margin), int(leverage)
    maxlev = int(setting(db, "futures_max_leverage", D))
    if leverage < 1 or leverage > maxlev:
        raise OrderError(f"Leverage 1-{maxlev}x")
    if margin < 1:
        raise OrderError("Minimum margin 1 USDT")
    m, mk = mark(db, symbol)
    spread = setting(db, "futures_spread", D)
    entry = mk * (1 + spread) if side == "long" else mk * (1 - spread)
    notional = margin * leverage
    size = notional / entry
    fee = q(notional * setting(db, "futures_fee", D), 8)
    pool = bal(db, house_id(db), "USDT", W).available
    if open_interest(db) + notional > pool * setting(db, "futures_max_oi_ratio", D):
        raise OrderError("Futures capacity reached, try a smaller size")
    try:
        move(db, uid, "USDT", d_avail=-(margin + fee), d_locked=margin, kind="fut_open", wallet=W)
    except InsufficientFunds:
        raise OrderError("Insufficient USDT in futures wallet (transfer from spot first)")
    move(db, house_id(db, "fees"), "USDT", d_avail=fee, kind="fut_fee")
    p = Position(user_id=uid, market=symbol, side=side, size=size, entry=entry, margin=margin, leverage=leverage,
                 liq_price=liq_price(side, entry, leverage), tp=D(tp) if tp else None, sl=D(sl) if sl else None,
                 status="open", fee=fee)
    db.add(p)
    db.flush()
    return p


def pnl_at(p, price):
    return (price - D(p.entry)) * D(p.size) if p.side == "long" else (D(p.entry) - price) * D(p.size)


def close_position(db, p: Position, reason="manual", price=None):
    if p.status != "open":
        raise OrderError("Position closed")
    if price is None:
        _, mk = mark(db, p.market)
        spread = setting(db, "futures_spread", D)
        price = mk * (1 - spread) if p.side == "long" else mk * (1 + spread)
    pnl = pnl_at(p, price)
    fee = q(price * D(p.size) * setting(db, "futures_fee", D), 8)
    margin = D(p.margin)
    if reason == "liquidation":
        payout, fee = ZERO, ZERO
    else:
        payout = margin + pnl - fee
        if payout < 0:
            fee = max(ZERO, min(fee, margin + pnl))
            payout = ZERO
    payout = q(payout, 8)
    h = house_id(db)
    move(db, p.user_id, "USDT", d_locked=-margin, d_avail=payout, kind="fut_close", ref=f"p{p.id}", wallet=W)
    if fee > 0:
        move(db, house_id(db, "fees"), "USDT", d_avail=fee, kind="fut_fee", ref=f"p{p.id}")
    move(db, h, "USDT", d_avail=margin - payout - fee, kind="fut_settle", ref=f"p{p.id}", wallet=W, allow_negative=True)
    p.status = "liquidated" if reason == "liquidation" else "closed"
    p.exit_price, p.pnl, p.fee = price, payout - margin, D(p.fee) + fee
    p.close_reason, p.closed_at = reason, datetime.utcnow()
    return p


def set_tpsl(db, p, tp, sl):
    p.tp = D(tp) if tp not in (None, "", 0, "0") else None
    p.sl = D(sl) if sl not in (None, "", 0, "0") else None


def risk_loop_once(db):
    """Liquidations + TP/SL."""
    for p in db.scalars(select(Position).where(Position.status == "open")).all():
        px = prices.ext_price(p.market)
        if not px:
            continue
        px = D(px)
        long = p.side == "long"
        if (long and px <= D(p.liq_price)) or (not long and px >= D(p.liq_price)):
            close_position(db, p, "liquidation", price=px)
        elif p.tp and ((long and px >= D(p.tp)) or (not long and px <= D(p.tp))):
            close_position(db, p, "take_profit")
        elif p.sl and ((long and px <= D(p.sl)) or (not long and px >= D(p.sl))):
            close_position(db, p, "stop_loss")


def position_view(p, px=None):
    d = {"id": p.id, "market": p.market, "side": p.side, "size": str(q(p.size, 6)), "entry": str(q(p.entry, 4)),
         "margin": str(q(p.margin, 4)), "leverage": p.leverage, "liq": str(q(p.liq_price, 4)), "tp": str(p.tp or ""),
         "sl": str(p.sl or ""), "status": p.status, "pnl": str(q(p.pnl or 0, 4)), "exit": str(p.exit_price or ""),
         "reason": p.close_reason, "created": p.created_at.isoformat()}
    if px and p.status == "open":
        u = pnl_at(p, D(px))
        d["mark"] = str(px)
        d["upnl"] = str(q(u, 4))
        d["roe"] = str(q(u / D(p.margin) * 100, 2))
    return d
