"""Trading bots: Grid, DCA and Copy Trading. They trade from the user's spot balance through the normal matching engine."""
import json, logging
from datetime import datetime, timedelta
from sqlalchemy import select, func, or_
from .db import SessionLocal, Bot, BotOrder, CopyLeader, Order, Trade, Market, User, now
from .engine import place_order, cancel_order, market_price, q, OrderError
from .ledger import bal, D, ZERO, InsufficientFunds

log = logging.getLogger("bots")
MAX_BOTS = 10


def _p(b):
    return json.loads(b.params or "{}")


def _place(db, b, side, otype, level=-1, **kw):
    o = place_order(db, b.user_id, kw.pop("market", b.market), side, otype, **kw)
    db.add(BotOrder(bot_id=b.id, order_id=o.id, level=level, side=side, handled=otype == "market"))
    return o


def _market(db, symbol):
    m = db.get(Market, str(symbol).upper())
    if not m or not m.active:
        raise OrderError("Market not available")
    return m


def _num(x, name):
    try:
        v = D(str(x))
    except Exception:
        raise OrderError(f"Bad {name}")
    if not v.is_finite() or v <= 0:
        raise OrderError(f"Bad {name}")
    return v


def create(db, uid, body):
    if db.scalar(select(func.count()).select_from(Bot).where(Bot.user_id == uid, Bot.status == "running")) >= MAX_BOTS:
        raise OrderError(f"Maximum {MAX_BOTS} running bots")
    kind = str(body.get("kind", ""))
    if kind == "grid":
        return _create_grid(db, uid, body)
    if kind == "dca":
        m = _market(db, body.get("market"))
        amount, hours = _num(body.get("amount"), "amount"), int(body.get("every_hours") or 24)
        if amount < m.min_notional:
            raise OrderError(f"Minimum {m.min_notional} {m.quote}")
        if not 1 <= hours <= 720:
            raise OrderError("Bad interval")
        b = Bot(user_id=uid, kind="dca", market=m.symbol, params=json.dumps({"amount": str(amount), "every_hours": hours, "next_at": now().isoformat()}))
        db.add(b)
        db.flush()
        return b
    if kind == "copy":
        leader = int(body.get("leader_id") or 0)
        amount = _num(body.get("amount"), "amount")
        cl = db.get(CopyLeader, leader)
        if not cl or not cl.enabled or leader == uid:
            raise OrderError("Trader not available")
        last = db.scalar(select(func.max(Trade.id))) or 0
        b = Bot(user_id=uid, kind="copy", market="*", params=json.dumps({"leader_id": leader, "amount": str(amount), "last_trade": last, "done": []}))
        db.add(b)
        db.flush()
        return b
    raise OrderError("Unknown bot")


def _create_grid(db, uid, body):
    m = _market(db, body.get("market"))
    lower, upper, invest = _num(body.get("lower"), "lower price"), _num(body.get("upper"), "upper price"), _num(body.get("invest"), "amount")
    grids = int(body.get("grids") or 10)
    if not 2 <= grids <= 50:
        raise OrderError("Grids must be 2 to 50")
    p = market_price(db, m)
    if not p or not lower < p < upper:
        raise OrderError(f"Current price {p} must be between lower and upper")
    step = (upper - lower) / grids
    qty = q(invest / grids / p, m.qty_prec)
    if qty <= 0 or qty * lower < m.min_notional:
        raise OrderError(f"Amount too small: each grid needs at least {m.min_notional} {m.quote}")
    lv = [lower + step * i for i in range(grids + 1)]
    b = Bot(user_id=uid, kind="grid", market=m.symbol,
            params=json.dumps({"lower": str(lower), "upper": str(upper), "grids": grids, "qty": str(qty), "step": str(step), "levels": [str(x) for x in lv]}))
    db.add(b)
    db.flush()
    sells = [i + 1 for i in range(grids) if lv[i + 1] > p]
    buys = [i for i in range(grids) if lv[i + 1] <= p]
    need = qty * len(sells) - bal(db, uid, m.base).available
    if need > 0:
        _place(db, b, "buy", "market", quote_qty=q(need * p * D("1.03"), 8))
    for i in sells:
        _place(db, b, "sell", "limit", level=i, price=lv[i], qty=qty)
    for i in buys:
        _place(db, b, "buy", "limit", level=i, price=lv[i], qty=qty)
    return b


def _tick_grid(db, b):
    p = _p(b)
    lv, qty, step = [D(x) for x in p["levels"]], D(p["qty"]), D(p["step"])
    for bo in db.scalars(select(BotOrder).where(BotOrder.bot_id == b.id, BotOrder.handled == False)).all():
        o = db.get(Order, bo.order_id)
        if o.status == "cancelled":
            bo.handled = True
        elif o.status == "filled":
            if bo.side == "buy" and bo.level + 1 < len(lv):
                _place(db, b, "sell", "limit", level=bo.level + 1, price=lv[bo.level + 1], qty=qty)
            elif bo.side == "sell" and bo.level - 1 >= 0:
                _place(db, b, "buy", "limit", level=bo.level - 1, price=lv[bo.level - 1], qty=qty)
                b.pnl = (b.pnl or ZERO) + qty * step
            bo.handled = True


def _tick_dca(db, b):
    p = _p(b)
    if now() < datetime.fromisoformat(p["next_at"]):
        return
    p["next_at"] = (now() + timedelta(hours=p["every_hours"])).isoformat()
    b.params = json.dumps(p)
    _place(db, b, "buy", "market", quote_qty=D(p["amount"]))
    b.note = ""


def _tick_copy(db, b):
    p = _p(b)
    leader, amount = p["leader_id"], D(p["amount"])
    cl = db.get(CopyLeader, leader)
    if not cl or not cl.enabled:
        b.status, b.note = "stopped", "Trader stopped sharing"
        return
    trades = db.scalars(select(Trade).where(Trade.id > p["last_trade"], or_(Trade.buyer == leader, Trade.seller == leader))
                        .order_by(Trade.id).limit(50)).all()
    done = set(p.get("done", []))
    for t in trades:
        p["last_trade"] = t.id
        if b.user_id in (t.buyer, t.seller) or t.buyer == t.seller:
            continue
        oid = t.buy_order if t.buyer == leader else t.sell_order
        if oid in done:
            continue
        done.add(oid)
        m = db.get(Market, t.market)
        if t.buyer == leader:
            _place(db, b, "buy", "market", market=m.symbol, quote_qty=amount)
        else:
            qty = q(min(amount / t.price, bal(db, b.user_id, m.base).available), m.qty_prec)
            if qty * t.price >= m.min_notional:
                _place(db, b, "sell", "market", market=m.symbol, qty=qty)
    p["done"] = sorted(done)[-200:]
    b.params = json.dumps(p)


TICK = {"grid": _tick_grid, "dca": _tick_dca, "copy": _tick_copy}


def tick():
    with SessionLocal() as db:
        ids = db.scalars(select(Bot.id).where(Bot.status == "running")).all()
    for bid in ids:
        db = SessionLocal()
        try:
            b = db.get(Bot, bid)
            TICK[b.kind](db, b)
            db.commit()
        except Exception as e:
            db.rollback()
            if not isinstance(e, (OrderError, InsufficientFunds)):
                log.exception("bot %s", bid)
            b = db.get(Bot, bid)
            b.note = str(e)[:200]
            if b.kind == "dca":
                p = _p(b)
                p["next_at"] = (now() + timedelta(hours=p["every_hours"])).isoformat()
                b.params = json.dumps(p)
            db.commit()
        finally:
            db.close()


def stop(db, uid, bid):
    b = db.get(Bot, bid)
    if not b or b.user_id != uid:
        raise OrderError("Bot not found")
    for bo in db.scalars(select(BotOrder).where(BotOrder.bot_id == bid)).all():
        o = db.get(Order, bo.order_id)
        if o and o.status == "open":
            cancel_order(db, uid, o.id)
        bo.handled = True
    b.status = "stopped"
    return b


def view(db, b):
    open_n = db.scalar(select(func.count()).select_from(BotOrder).join(Order, Order.id == BotOrder.order_id)
                       .where(BotOrder.bot_id == b.id, Order.status == "open"))
    p = _p(b)
    p.pop("done", None)
    p.pop("levels", None)
    return {"id": b.id, "kind": b.kind, "market": b.market, "status": b.status, "pnl": b.pnl, "note": b.note, "params": p,
            "open": open_n, "created_at": b.created_at}


def leaders(db):
    since = now() - timedelta(days=30)
    follow = {}
    for b in db.scalars(select(Bot).where(Bot.kind == "copy", Bot.status == "running")).all():
        lid = _p(b)["leader_id"]
        follow[lid] = follow.get(lid, 0) + 1
    out = []
    for cl in db.scalars(select(CopyLeader).where(CopyLeader.enabled == True)).all():
        u = db.get(User, cl.user_id)
        n = db.scalar(select(func.count()).select_from(Trade).where(Trade.created_at >= since, or_(Trade.buyer == cl.user_id, Trade.seller == cl.user_id)))
        out.append({"user_id": cl.user_id, "nickname": u.nickname, "trades_30d": n, "followers": follow.get(cl.user_id, 0)})
    return sorted(out, key=lambda x: -x["trades_30d"])
