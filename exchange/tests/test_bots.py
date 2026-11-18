import os, sys
os.environ["ALTAF_DB"] = "sqlite:////home/ubuntu/work/tests/bt.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal as D
from app import main, bots
from app.db import SessionLocal, init_db, User, Order, Bot
from app.ledger import move, bal, set_setting
from app.engine import place_order

init_db(); main.seed()
db = SessionLocal()
def user(e):
    u = User(email=e, pw_hash="!", nickname=e.split("@")[0]); db.add(u); db.flush(); return u.id
A, S, T, B, L, F = (user(f"{x}@t.io") for x in "astblf")
for u in (A, S, T, B, L, F):
    move(db, u, "USDT", d_avail=D(2000), kind="test"); move(db, u, "ALTF", d_avail=D(100), kind="test")
move(db, A, "ALTF", d_avail=D(-100), kind="test")
db.commit()
print("altf price", main.eng.market_price(db, db.get(main.Market, "ALTFUSDT")))
place_order(db, S, "ALTFUSDT", "sell", "limit", price="25.5", qty="4.2"); db.commit()
# GRID
b = bots.create(db, A, {"kind": "grid", "market": "ALTFUSDT", "lower": "20", "upper": "30", "grids": "10", "invest": "200"}); db.commit()
v = bots.view(db, b); print("grid", v["open"], "open orders", bal(db, A, "ALTF"))
assert v["open"] == 10, v
place_order(db, T, "ALTFUSDT", "buy", "limit", price="26", qty="1"); db.commit()
bots.tick(); db.expire_all()
b = db.get(Bot, b.id); print("after up-move pnl", b.pnl, "note", b.note, "open", bots.view(db, b)["open"])
assert b.pnl == D("0.8") and bots.view(db, b)["open"] == 10
place_order(db, T, "ALTFUSDT", "sell", "limit", price="25", qty="0.8"); db.commit()
bots.tick(); db.expire_all(); print("after down-move open", bots.view(db, db.get(Bot, b.id))["open"])
assert bots.view(db, db.get(Bot, b.id))["open"] == 10
# DCA
place_order(db, T, "ALTFUSDT", "sell", "limit", price="27", qty="5"); db.commit()
d = bots.create(db, B, {"kind": "dca", "market": "ALTFUSDT", "amount": "10", "every_hours": "24"}); db.commit()
b0 = bal(db, B, "ALTF").available; bots.tick(); db.expire_all(); b1 = bal(db, B, "ALTF").available
print("dca bought", b1 - b0, db.get(Bot, d.id).note); assert b1 > b0
bots.tick(); db.expire_all(); assert bal(db, B, "ALTF").available == b1, "dca must wait 24h"
# COPY
from app.db import CopyLeader
db.add(CopyLeader(user_id=L)); db.commit()
c = bots.create(db, F, {"kind": "copy", "leader_id": L, "amount": "10"}); db.commit()
f0 = bal(db, F, "ALTF").available
place_order(db, L, "ALTFUSDT", "buy", "limit", price="27", qty="1"); db.commit()
bots.tick(); db.expire_all(); f1 = bal(db, F, "ALTF").available
print("copy bought", f1 - f0, db.get(Bot, c.id).note); assert f1 > f0
bots.tick(); db.expire_all(); assert bal(db, F, "ALTF").available == f1, "no double copy"
print("leaders", bots.leaders(db))
# STOP returns locked funds
bots.stop(db, A, b.id); db.commit(); db.expire_all()
print("stopped; A locked", bal(db, A, "USDT").locked, bal(db, A, "ALTF").locked)
assert bal(db, A, "USDT").locked == 0 and bal(db, A, "ALTF").locked == 0
try:
    bots.create(db, A, {"kind": "grid", "market": "ALTFUSDT", "lower": "30", "upper": "40", "grids": "5", "invest": "100"}); print("BAD")
except main.OrderError as e: print("range check:", e)
print("ALL BOT TESTS PASSED")
