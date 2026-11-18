import os, sys, time
os.environ["ALTAF_DB"] = "sqlite:////home/ubuntu/work/tests/mm.db"
if os.path.exists("/home/ubuntu/work/tests/mm.db"): os.remove("/home/ubuntu/work/tests/mm.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal as D
from app import main, mm, prices
from app.db import SessionLocal, init_db, User, Order
from app.ledger import move, bal, setting, house_id
from app.engine import place_order, OrderError

def px(**kw):
    for s, p in kw.items():
        prices.PRICES[s + "USDT"] = {"last": D(str(p)), "open": D(str(p)), "high": D(str(p)), "low": D(str(p)), "vol": D(1), "qvol": D(1), "change": D(0), "ts": time.time()}
prices.PRICES.clear(); px(BTC=60000, ETH=3000, BNB=600, SOL=150, TRX="0.3")
init_db(); main.seed()
db = SessionLocal()
def user(e):
    u = User(email=e, pw_hash="!", nickname=e.split("@")[0]); db.add(u); db.flush(); return u.id
F, T = user("f@t.io"), user("t@t.io")
move(db, F, "USDT", d_avail=D(1500), kind="test"); move(db, T, "BTC", d_avail=D(1), kind="test"); move(db, T, "USDT", d_avail=D(5000), kind="test"); db.commit()
M = mm.mm_id(db)
def opn(sym=None): return [o for o in db.query(Order).filter(Order.user_id == M, Order.status == "open") if not sym or o.market == sym]
mm.tick(db); db.commit(); assert not opn(), "disabled -> no orders"
mm.fund(db, F, "in", "USDT", "1000"); db.commit()
assert bal(db, F, "USDT").available == 500 and bal(db, M, "USDT").available == 1000
mm.start(db); db.commit()
mm.tick(db); db.commit()
o = opn(); print("orders", len(o), "locked", bal(db, M, "USDT").locked)
assert len(o) > 0 and all(x.side == "buy" for x in o), "starts with USDT only -> bids only"
assert bal(db, M, "USDT").locked <= 1000
best = max(D(x.price) for x in opn("BTCUSDT")); assert best < D(60000)
# trader sells into MM bid -> MM gets inventory, no fee charged to MM
q0 = sum(D(x.qty) - D(x.filled) for x in opn("BTCUSDT") if D(x.price) == best)
place_order(db, T, "BTCUSDT", "sell", "market", qty=str(q0)); db.commit()
print("mm btc", bal(db, M, "BTC").available); assert bal(db, M, "BTC").available == q0, "MM pays no fee"
mm.tick(db); db.commit()
asks = [x for x in opn("BTCUSDT") if x.side == "sell"]; print("asks", len(asks)); assert asks and min(D(x.price) for x in asks) > D(60000)
# stale price -> pauses held-coin quoting
prices.PRICES["BTCUSDT"]["ts"] = time.time() - 500
mm.tick(db); db.commit(); assert not opn(), "stale price -> all cancelled"; print("stale:", mm.STATUS)
# volatility pause
px(BTC=60000); mm.HIST.clear(); mm.tick(db); db.commit()
px(ETH=3100); mm.tick(db); db.commit()
assert not opn("ETHUSDT") and "paused" in mm.STATUS["ETHUSDT"], mm.STATUS; print("vol:", mm.STATUS["ETHUSDT"])
# funding in mid-day does not count as profit
v0 = mm.view(db); mm.fund(db, F, "in", "USDT", "100"); db.commit(); v1 = mm.view(db)
assert abs(D(v1["pnl_today_usd"]) - D(v0["pnl_today_usd"])) < D("0.01"), (v0, v1)
# daily loss limit 3% -> halt and stay halted
eq = mm.equity(db); print("equity", eq)
# compute BTC price giving > 3% equity drop
need = eq * D("0.031"); newp = (D(60000) * q0 - need) / q0; px(BTC=newp); mm.HIST.clear()
mm.tick(db); db.commit()
v = mm.view(db); print("halt:", v["halted"]); assert not v["enabled"] and "daily loss" in v["halted"] and not opn()
mm.tick(db); db.commit(); assert not opn(), "stays halted"
# restart resets baseline
mm.start(db); db.commit(); mm.tick(db); db.commit(); assert mm.view(db)["enabled"] and opn()
# withdraw back
mm.fund(db, F, "out", "USDT", "100"); db.commit(); assert bal(db, F, "USDT").available == 500
try:
    mm.fund(db, F, "out", "USDT", "99999"); print("BAD")
except OrderError as e: print("over-withdraw:", e)
print(mm.view(db))
print("ALL MM TESTS PASSED")
