import os, sys, time
os.environ["ALTAF_DB"] = "sqlite:////tmp/altaf_test.db"
if os.path.exists("/tmp/altaf_test.db"): os.remove("/tmp/altaf_test.db")
sys.path.insert(0, ".")
from decimal import Decimal as D
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal, User
from app.ledger import move, set_setting
from sqlalchemy import select

with TestClient(app) as c:
    time.sleep(4)  # prices
    r = c.post("/api/register", json={"email": "founder@example.com", "password": "abc12345"}); assert r.status_code == 200, r.text
    A = {"Authorization": "Bearer " + r.json()["token"]}
    r = c.post("/api/register", json={"email": "bob@x.com", "password": "abc12345"}); B = {"Authorization": "Bearer " + r.json()["token"]}
    db = SessionLocal()
    uids = {u.email: u.id for u in db.scalars(select(User))}
    for em in ("founder@example.com", "bob@x.com"):
        move(db, uids[em], "USDT", d_avail=D(10000), kind="test"); move(db, uids[em], "ALTF", d_avail=D(10), kind="test")
    move(db, uids["house@altaf.system"], "USDT", d_avail=D(100000), kind="test", wallet="futures")
    move(db, uids["house@altaf.system"], "USDT", d_avail=D(100000), kind="test"); move(db, uids["house@altaf.system"], "BTC", d_avail=D(5), kind="test")
    set_setting(db, "futures_enabled", "1"); db.commit(); db.close()
    ok = lambda r: (r.status_code == 200) or print("FAIL", r.request.url, r.status_code, r.text)
    m = c.get("/api/markets").json(); print("markets", [(x["symbol"], x["last"]) for x in m])
    # spot ALTF book between users
    ok(c.post("/api/order", headers=A, json={"symbol": "ALTFUSDT", "side": "sell", "type": "limit", "price": "25", "qty": "2"}))
    r = c.post("/api/order", headers=B, json={"symbol": "ALTFUSDT", "side": "buy", "type": "limit", "price": "25", "qty": "1"}); ok(r); print("order", r.json().get("status"))
    print("book", c.get("/api/book/ALTFUSDT").json())
    # market buy BTC (external)
    r = c.post("/api/order", headers=B, json={"symbol": "BTCUSDT", "side": "buy", "type": "market", "quote_qty": "100"}); print("btc mkt", r.status_code, r.text[:200])
    r = c.get("/api/convert/quote", params={"frm": "USDT", "to": "BTC", "amount": "50"}); print("quote", r.json())
    r = c.post("/api/convert", headers=B, json={"from": "USDT", "to": "BTC", "amount": "50"}); ok(r); print("convert", r.json())
    # futures
    ok(c.post("/api/transfer", headers=B, json={"from": "spot", "to": "futures", "asset": "USDT", "amount": "500"}))
    r = c.post("/api/futures/open", headers=B, json={"symbol": "BTCUSDT", "side": "long", "margin": "100", "leverage": 10}); ok(r); pos = r.json(); print("pos", pos)
    print("positions", c.get("/api/futures/positions", headers=B).json()[0].get("upnl"))
    r = c.post(f"/api/futures/close/{pos['id']}", headers=B); ok(r); print("closed", r.json().get("pnl"))
    # earn
    pid = c.get("/api/earn").json()[0]["id"]; ok(c.post("/api/earn/subscribe", headers=B, json={"product_id": pid, "amount": "100"}))
    # p2p: A sells USDT
    r = c.post("/api/p2p/ad", headers=A, json={"side": "sell", "asset": "USDT", "fiat": "TND", "price": "3.1", "amount": "100", "min_fiat": "10", "max_fiat": "310", "methods": "D17"}); ok(r); ad = r.json()
    r = c.post(f"/api/p2p/take/{ad['id']}", headers=B, json={"fiat_amount": "31"}); ok(r); o = r.json(); print("p2p order", o.get("status"), o.get("amount"))
    ok(c.post(f"/api/p2p/order/{o['id']}/paid", headers=B, json={}))
    ok(c.post(f"/api/p2p/order/{o['id']}/release", headers=A, json={}))
    # factory (deploy will fail: no mnemonic -> refunded)
    r = c.post("/api/factory", headers=B, json={"chain": "TRON", "name": "Bob Coin", "symbol": "BOB", "supply": "1000000", "decimals": 6, "owner": "TTmpMwovWMqAS9qhkvNfVi2G7nxm74BHjm", "list_it": True}); ok(r); print("factory", r.json().get("status"))
    time.sleep(3); print("factory jobs", [(j["status"], j["error"][:60]) for j in c.get("/api/factory/mine", headers=B).json()])
    bals = c.get("/api/balances", headers=B).json(); print("B balances", bals)
    # withdraw needs 2FA
    print("withdraw", c.post("/api/withdraw", headers=B, json={"asset": "USDT", "chain": "TRON", "address": "TTmpMwovWMqAS9qhkvNfVi2G7nxm74BHjm", "amount": "20"}).json())
    print("admin stats", c.get("/api/admin/stats", headers=A).status_code, "bob admin", c.get("/api/admin/stats", headers=B).status_code)
    print("me", c.get("/api/me", headers=B).json()["launchpad_access"])
