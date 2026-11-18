"""Spot matching engine, fees, convert, internal prices."""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from sqlalchemy import select, func
from .db import Order, Trade, Market, Asset, User
from .ledger import move, transfer, setting, house_id, bal, InsufficientFunds, D, ZERO
from . import prices


class OrderError(Exception):
    pass


def q(x, prec):
    return D(x).quantize(D(1).scaleb(-prec), rounding=ROUND_DOWN)


def last_trade_price(db, market):
    return db.scalars(select(Trade.price).where(Trade.market == market).order_by(Trade.id.desc()).limit(1)).first()


def usd_price(db, asset):
    """Price of 1 unit of asset in USDT."""
    if asset in ("USDT", "USDC"):
        return D(1)
    p = prices.ext_price(asset + "USDT")
    if p:
        return p
    lt = last_trade_price(db, asset + "USDT")
    if lt:
        return D(lt)
    if asset == "ALTF":
        return setting(db, "altf_price", D)
    return ZERO


def market_price(db, m: Market):
    if m.external:
        p = prices.ext_price(m.symbol)
        if p:
            return p
    lt = last_trade_price(db, m.symbol)
    if lt:
        return D(lt)
    return usd_price(db, m.base) / (usd_price(db, m.quote) or D(1))


def charge_fee(db, uid, asset, amount, rate, ref):
    """Fee on received amount. Returns net amount credited to user. Pays in ALTF with discount if enabled."""
    if rate <= 0 or amount <= 0:
        return amount
    fees = house_id(db, "fees")
    if uid in (fees, house_id(db), house_id(db, "mm")):
        return amount
    u = db.get(User, uid)
    fee = amount * rate
    if u and u.pay_fee_altf and asset != "ALTF":
        altf_usd = usd_price(db, "ALTF")
        if altf_usd > 0:
            fee_usd = fee * usd_price(db, asset) * (1 - setting(db, "altf_fee_discount", D))
            fee_altf = q(fee_usd / altf_usd, 8)
            b = bal(db, uid, "ALTF")
            if fee_altf > 0 and b.available >= fee_altf:
                transfer(db, uid, fees, "ALTF", fee_altf, "fee", ref)
                return amount
    fee = q(fee, 8)
    move(db, fees, asset, d_avail=fee, kind="fee", ref=ref)
    return amount - fee


def _remaining(o: Order):
    return (o.qty or ZERO) - (o.filled or ZERO)


def place_order(db, uid, symbol, side, otype, price=None, qty=None, quote_qty=None, is_mm=False):
    m = db.get(Market, symbol)
    if not m or not m.active:
        raise OrderError("Market not available")
    if side not in ("buy", "sell") or otype not in ("limit", "market"):
        raise OrderError("Bad order")
    price = q(price, m.price_prec) if price not in (None, "") else None
    qty = q(qty, m.qty_prec) if qty not in (None, "") else None
    quote_qty = q(quote_qty, 8) if quote_qty not in (None, "") else None
    ref_price = market_price(db, m)
    if otype == "limit":
        if not price or price <= 0 or not qty or qty <= 0:
            raise OrderError("Enter price and amount")
        if price * qty < m.min_notional:
            raise OrderError(f"Minimum order {m.min_notional} {m.quote}")
        lock_asset, lock_amt = (m.quote, price * qty) if side == "buy" else (m.base, qty)
    else:
        if side == "buy":
            if not quote_qty:
                if not qty:
                    raise OrderError("Enter amount")
                quote_qty = q(qty * ref_price * D("1.05"), 8)
                qty = None
            if quote_qty < m.min_notional:
                raise OrderError(f"Minimum order {m.min_notional} {m.quote}")
            lock_asset, lock_amt = m.quote, quote_qty
        else:
            if not qty or qty <= 0:
                raise OrderError("Enter amount")
            lock_asset, lock_amt = m.base, qty
    try:
        move(db, uid, lock_asset, d_avail=-lock_amt, d_locked=lock_amt, kind="order_lock")
    except InsufficientFunds:
        raise OrderError(f"Insufficient {lock_asset} balance")
    o = Order(user_id=uid, market=symbol, side=side, type=otype, price=price, qty=qty or ZERO, quote_qty=quote_qty or ZERO,
              filled=ZERO, filled_quote=ZERO, locked_left=lock_amt, status="open", is_mm=is_mm)
    db.add(o)
    db.flush()
    _match(db, m, o)
    return o


def _match(db, m: Market, o: Order):
    opp = "sell" if o.side == "buy" else "buy"
    qry = select(Order).where(Order.market == m.symbol, Order.status == "open", Order.side == opp, Order.id != o.id)
    if o.type == "limit":
        qry = qry.where(Order.price <= o.price) if o.side == "buy" else qry.where(Order.price >= o.price)
    qry = qry.order_by(Order.price.asc() if o.side == "buy" else Order.price.desc(), Order.id.asc())
    taker_fee, maker_fee = setting(db, "taker_fee", D), setting(db, "maker_fee", D)
    for mk in db.scalars(qry).all():
        if mk.user_id == o.user_id:
            continue
        px = D(mk.price)
        mrem = _remaining(mk)
        if o.type == "market" and o.side == "buy":
            budget = o.locked_left
            tq = min(mrem, q(budget / px, m.qty_prec))
        else:
            tq = min(mrem, _remaining(o))
        if tq <= 0:
            break
        quote_amt = q(tq * px, 8)
        buy_o, sell_o = (o, mk) if o.side == "buy" else (mk, o)
        # buyer pays quote from lock, seller gives base from lock
        move(db, buy_o.user_id, m.quote, d_locked=-quote_amt, kind="trade", ref=f"o{buy_o.id}")
        buy_o.locked_left -= quote_amt
        move(db, sell_o.user_id, m.base, d_locked=-tq, kind="trade", ref=f"o{sell_o.id}")
        sell_o.locked_left -= tq
        b_rate = taker_fee if buy_o is o else maker_fee
        s_rate = taker_fee if sell_o is o else maker_fee
        net_base = charge_fee(db, buy_o.user_id, m.base, tq, b_rate, f"o{buy_o.id}")
        move(db, buy_o.user_id, m.base, d_avail=net_base, kind="trade", ref=f"o{buy_o.id}")
        net_quote = charge_fee(db, sell_o.user_id, m.quote, quote_amt, s_rate, f"o{sell_o.id}")
        move(db, sell_o.user_id, m.quote, d_avail=net_quote, kind="trade", ref=f"o{sell_o.id}")
        for x in (o, mk):
            x.filled = (x.filled or ZERO) + tq
            x.filled_quote = (x.filled_quote or ZERO) + quote_amt
        if o.type == "market" and o.side == "buy":
            o.qty = o.filled
        db.add(Trade(market=m.symbol, price=px, qty=tq, buy_order=buy_o.id, sell_order=sell_o.id,
                     buyer=buy_o.user_id, seller=sell_o.user_id, taker_side=o.side))
        if _remaining(mk) <= 0:
            _close(db, m, mk, "filled")
        if o.type != "market" or o.side != "buy":
            if _remaining(o) <= 0:
                break
        elif o.locked_left < px * D(1).scaleb(-m.qty_prec):
            break
    if o.type == "market":
        if o.filled <= 0:
            raise OrderError("No sellers/buyers in the book right now, use a limit order")
        _close(db, m, o, "filled")
    elif _remaining(o) <= 0:
        _close(db, m, o, "filled")


def _close(db, m, o, status):
    o.status = status
    if o.locked_left and o.locked_left > 0:
        asset = m.quote if o.side == "buy" else m.base
        move(db, o.user_id, asset, d_avail=o.locked_left, d_locked=-o.locked_left, kind="order_unlock", ref=f"o{o.id}")
        o.locked_left = ZERO


def cancel_order(db, uid, oid, admin=False):
    o = db.get(Order, oid)
    if not o or (o.user_id != uid and not admin) or o.status != "open":
        raise OrderError("Order not found")
    _close(db, db.get(Market, o.market), o, "cancelled")
    return o


def book(db, symbol, depth=20):
    m = db.get(Market, symbol)
    out = {}
    for side, order in (("bids", Order.price.desc()), ("asks", Order.price.asc())):
        rows = db.execute(select(Order.price, func.sum(Order.qty - Order.filled)).where(
            Order.market == symbol, Order.status == "open", Order.side == ("buy" if side == "bids" else "sell"))
            .group_by(Order.price).order_by(order).limit(depth)).all()
        out[side] = [[str(q(p, m.price_prec)), str(q(s, m.qty_prec))] for p, s in rows if s and s > 0]
    return out


def ticker(db, m: Market):
    since = datetime.utcnow() - timedelta(hours=24)
    if m.external and prices.PRICES.get(m.symbol):
        p = prices.PRICES[m.symbol]
        return {"symbol": m.symbol, "last": str(p["last"]), "change": f"{p['change']:.2f}", "high": str(p["high"]),
                "low": str(p["low"]), "vol": str(p["vol"]), "qvol": str(p["qvol"])}
    rows = db.execute(select(Trade.price, Trade.qty).where(Trade.market == m.symbol, Trade.created_at >= since).order_by(Trade.id)).all()
    last = market_price(db, m)
    if rows:
        op = D(rows[0][0])
        hi, lo = max(D(r[0]) for r in rows), min(D(r[0]) for r in rows)
        vol = sum(D(r[1]) for r in rows)
        qv = sum(D(r[0]) * D(r[1]) for r in rows)
        ch = (last - op) / op * 100 if op else ZERO
    else:
        hi = lo = last
        vol = qv = ch = ZERO
    return {"symbol": m.symbol, "last": str(q(last, m.price_prec)), "change": f"{ch:.2f}", "high": str(hi), "low": str(lo), "vol": str(vol), "qvol": str(q(qv, 2))}


INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def klines(db, symbol, interval="15m", limit=300):
    m = db.get(Market, symbol)
    if m and m.external:
        try:
            return prices.ext_klines(symbol, interval, limit)
        except Exception:
            pass
    step = INTERVALS.get(interval, 900)
    since = datetime.utcnow() - timedelta(seconds=step * limit)
    rows = db.execute(select(Trade.created_at, Trade.price, Trade.qty).where(Trade.market == symbol, Trade.created_at >= since).order_by(Trade.id)).all()
    out = {}
    for t, p, s in rows:
        k = int(t.timestamp()) // step * step
        p, s = float(p), float(s)
        if k not in out:
            out[k] = [k, p, p, p, p, s]
        else:
            c = out[k]
            c[2], c[3], c[4], c[5] = max(c[2], p), min(c[3], p), p, c[5] + s
    return [out[k] for k in sorted(out)]


def recent_trades(db, symbol, limit=40):
    rows = db.scalars(select(Trade).where(Trade.market == symbol).order_by(Trade.id.desc()).limit(limit)).all()
    return [{"price": str(t.price), "qty": str(t.qty), "side": t.taker_side, "t": int(t.created_at.timestamp())} for t in rows]


# ---------------- Convert (house is counterparty) ----------------
def convert_quote(db, frm, to, amount):
    amount = D(amount)
    pf, pt = usd_price(db, frm), usd_price(db, to)
    if pf <= 0 or pt <= 0 or frm == to:
        raise OrderError("Pair not available")
    rate = pf / pt * (1 - setting(db, "convert_spread", D))
    return rate, q(amount * rate, 8)


def convert(db, uid, frm, to, amount):
    amount = D(amount)
    if amount <= 0:
        raise OrderError("Enter amount")
    if amount * usd_price(db, frm) < 1:
        raise OrderError("Minimum 1 USDT")
    rate, out = convert_quote(db, frm, to, amount)
    h = house_id(db)
    if bal(db, h, to).available < out:
        raise OrderError("Convert liquidity is low for this pair, try later")
    try:
        transfer(db, uid, h, frm, amount, "convert")
    except InsufficientFunds:
        raise OrderError(f"Insufficient {frm} balance")
    transfer(db, h, uid, to, out, "convert")
    return {"rate": str(rate), "received": str(out)}
