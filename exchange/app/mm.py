"""Market maker: quotes external markets from a dedicated account (mm@altaf.system) funded by the founder.
Guards: stale price, volatility pause, inventory cap with skew, budget split, daily loss limit (kill switch)."""
import logging, time
from collections import deque
from datetime import datetime
from sqlalchemy import select, func
from .db import Order, Trade, Market, Balance
from .engine import place_order, _close, usd_price, q, OrderError
from .ledger import setting, set_setting, house_id, InsufficientFunds, transfer, D, ZERO
from . import prices

log = logging.getLogger("mm")
HIST = {}      # symbol -> deque[(ts, price)]
LAST = {}      # symbol -> (ts, ref, n_orders)
STATUS = {}    # symbol -> text


def mm_id(db):
    return house_id(db, "mm")


def equity(db):
    uid = mm_id(db)
    tot = ZERO
    for b in db.scalars(select(Balance).where(Balance.user_id == uid, Balance.wallet == "spot")):
        amt = D(b.available or 0) + D(b.locked or 0)
        if amt:
            tot += amt * usd_price(db, b.asset)
    return tot


def markets(db):
    want = [s.strip().upper() for s in setting(db, "mm_markets").split(",") if s.strip()]
    rows = db.scalars(select(Market).where(Market.active == True, Market.external == True, Market.symbol.in_(want))).all()
    return sorted(rows, key=lambda m: want.index(m.symbol))


def _open(db, symbol=None):
    s = select(Order).where(Order.user_id == mm_id(db), Order.status == "open")
    if symbol:
        s = s.where(Order.market == symbol)
    return db.scalars(s).all()


def cancel_all(db, symbol=None):
    n = 0
    for o in _open(db, symbol):
        _close(db, db.get(Market, o.market), o, "cancelled")
        n += 1
    if symbol:
        LAST.pop(symbol, None)
    else:
        LAST.clear()
    return n


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def halt(db, reason):
    cancel_all(db)
    set_setting(db, "mm_enabled", "0")
    set_setting(db, "mm_halted", reason)
    from .security import log_event
    log_event(db, "mm_halt", mm_id(db), "", reason, "critical")
    log.warning("market maker halted: %s", reason)


def start(db):
    set_setting(db, "mm_halted", "")
    set_setting(db, "mm_day", _today())
    set_setting(db, "mm_day_start_equity", str(equity(db)))
    set_setting(db, "mm_enabled", "1")


def stop(db, reason="stopped by admin"):
    cancel_all(db)
    set_setting(db, "mm_enabled", "0")
    set_setting(db, "mm_halted", reason)


def fund(db, admin_uid, direction, asset, amount):
    amount = D(str(amount))
    if amount <= 0 or direction not in ("in", "out"):
        raise OrderError("Bad amount")
    if direction == "out":
        cancel_all(db)
    src, dst = (admin_uid, mm_id(db)) if direction == "in" else (mm_id(db), admin_uid)
    try:
        transfer(db, src, dst, asset, amount, "mm_fund_" + direction)
    except InsufficientFunds:
        raise OrderError(f"Insufficient {asset} balance")
    usd = amount * usd_price(db, asset) * (1 if direction == "in" else -1)
    set_setting(db, "mm_net_funded_usd", str(setting(db, "mm_net_funded_usd", D) + usd))
    if setting(db, "mm_day") == _today():
        set_setting(db, "mm_day_start_equity", str(setting(db, "mm_day_start_equity", D) + usd))


def _move(sym, p):
    h = HIST.setdefault(sym, deque())
    now = time.time()
    h.append((now, p))
    while h and now - h[0][0] > 60:
        h.popleft()
    old = h[0][1]
    return abs(p - old) / old if old else ZERO


def tick(db):
    if not setting(db, "mm_enabled", bool):
        if _open(db):
            cancel_all(db)
        return
    uid = mm_id(db)
    held = [b.asset for b in db.scalars(select(Balance).where(Balance.user_id == uid, Balance.wallet == "spot"))
            if (D(b.available or 0) + D(b.locked or 0)) > 0 and b.asset not in ("USDT", "USDC")]
    if any(not prices.ext_price(a + "USDT") for a in held):
        cancel_all(db)
        STATUS["*"] = "paused: no live price for held coins"
        return
    STATUS.pop("*", None)
    eq = equity(db)
    if setting(db, "mm_day") != _today():
        set_setting(db, "mm_day", _today())
        set_setting(db, "mm_day_start_equity", str(eq))
    start_eq = setting(db, "mm_day_start_equity", D)
    limit = setting(db, "mm_max_daily_loss_pct", D)
    if start_eq > 0 and eq < start_eq * (1 - limit):
        return halt(db, f"daily loss limit {limit * 100}% reached: {q(start_eq, 2)} -> {q(eq, 2)} USD")
    if eq <= 0:
        STATUS.clear()
        STATUS["*"] = "no funds"
        return
    mk = markets(db)
    if not mk:
        return
    spread, levels = setting(db, "mm_spread", D), max(1, min(10, int(setting(db, "mm_levels", D))))
    size_cap, inv_pct = setting(db, "mm_size_usd", D), setting(db, "mm_max_inventory_pct", D)
    vol_pause, requote = setting(db, "mm_vol_pause", D), setting(db, "mm_requote", D)
    quote_total = {}
    for b in db.scalars(select(Balance).where(Balance.user_id == uid, Balance.wallet == "spot")):
        quote_total[b.asset] = D(b.available or 0) + D(b.locked or 0)
    per_market = eq / len(mk)
    for m in mk:
        ref = prices.ext_price(m.symbol)
        if not ref or ref <= 0:
            cancel_all(db, m.symbol)
            STATUS[m.symbol] = "paused: no live price"
            continue
        mv = _move(m.symbol, ref)
        if mv > vol_pause:
            cancel_all(db, m.symbol)
            STATUS[m.symbol] = f"paused: price moved {q(mv * 100, 2)}% in 60s"
            continue
        sp = spread * 2 if mv > vol_pause / 2 else spread
        last = LAST.get(m.symbol)
        n_open = len(_open(db, m.symbol))
        if last and abs(ref - last[1]) / last[1] < requote and n_open == last[2] and time.time() - last[0] < 60:
            continue
        cancel_all(db, m.symbol)
        base_amt = quote_total.get(m.base, ZERO)
        base_usd = base_amt * ref
        max_inv = per_market * inv_pct * 2
        inv = min(base_usd / max_inv, D(1)) if max_inv > 0 else D(1)
        mid = ref * (1 - inv * sp)
        bid_budget = max(ZERO, min(per_market - base_usd, max_inv - base_usd, quote_total.get(m.quote, ZERO) / len(mk)))
        bid_lvl = min(size_cap, bid_budget / levels)
        ask_lvl_qty = min(size_cap / ref, base_amt / levels)
        placed = 0
        for i in range(1, levels + 1):
            off = sp * i
            bid = min(mid * (1 - off), ref * (1 - sp / 2))
            ask = max(mid * (1 + off), ref * (1 + sp / 2))
            for side, px, qty in (("buy", bid, bid_lvl / bid if bid > 0 else ZERO), ("sell", ask, ask_lvl_qty)):
                qty = q(qty, m.qty_prec)
                if qty <= 0 or q(px, m.price_prec) * qty < m.min_notional:
                    continue
                try:
                    o = place_order(db, uid, m.symbol, side, "limit", price=px, qty=qty, is_mm=True)
                    placed += o.status == "open"
                except (OrderError, InsufficientFunds):
                    pass
        LAST[m.symbol] = (time.time(), ref, placed)
        STATUS[m.symbol] = f"quoting {placed} orders, spread {q(sp * 100, 3)}%, inventory {q(inv * 100, 1)}%"


def view(db):
    uid = mm_id(db)
    eq = equity(db)
    start_eq = setting(db, "mm_day_start_equity", D) if setting(db, "mm_day") == _today() else eq
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    fills = db.execute(select(func.count(Trade.id), func.coalesce(func.sum(Trade.price * Trade.qty), 0)).where(
        Trade.created_at >= since, (Trade.buyer == uid) | (Trade.seller == uid))).one()
    bals = [{"asset": b.asset, "available": b.available, "locked": b.locked}
            for b in db.scalars(select(Balance).where(Balance.user_id == uid, Balance.wallet == "spot")) if (b.available or 0) or (b.locked or 0)]
    limit = setting(db, "mm_max_daily_loss_pct", D)
    return {"enabled": setting(db, "mm_enabled", bool), "halted": setting(db, "mm_halted"), "equity_usd": q(eq, 2),
            "day_start_usd": q(start_eq, 2), "pnl_today_usd": q(eq - start_eq, 2), "loss_limit_usd": q(start_eq * limit, 2),
            "pnl_total_usd": q(eq - setting(db, "mm_net_funded_usd", D), 2), "fills_today": fills[0], "volume_today_usd": q(D(fills[1] or 0), 2),
            "open_orders": len(_open(db)), "balances": bals, "markets": STATUS}
