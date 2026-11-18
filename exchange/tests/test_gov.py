import os, sys
os.environ["ALTAF_DB"] = "sqlite:////home/ubuntu/work/tests/gov.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import timedelta
from fastapi.testclient import TestClient
from app import main, governance as gov
from app.db import SessionLocal, User
from app.servers import ServerNode
from app.security import hash_pw, new_totp_secret
import pyotp
c = TestClient(main.app); main.init_db(); main.seed()
db = SessionLocal(); sec = {}
def mk(e):
    s = new_totp_secret(); u = User(email=e, pw_hash=hash_pw("Passw0rd99"), nickname=e.split("@")[0], totp_secret=s, totp_enabled=True)
    db.add(u); db.flush(); sec[e] = s; return u.id
ids = {e: mk(e) for e in ["founder@example.com", "p1@t.io", "p2@t.io", "p3@t.io", "p4@t.io", "p5@t.io", "x@t.io"]}
for i, e in enumerate(["p1@t.io", "p2@t.io", "p3@t.io", "p4@t.io", "p5@t.io", "p5@t.io"]):
    db.add(ServerNode(user_id=ids[e], address=f"T{i}", url="http://x", status="active"))
db.commit()
def H(e):
    t = c.post("/api/login", json={"email": e, "password": "Passw0rd99", "code": pyotp.TOTP(sec[e]).now()}).json()["token"]
    return {"Authorization": "Bearer " + t}
code = lambda e: pyotp.TOTP(sec[e]).now()
hs = {e: H(e) for e in ids}
r = c.get("/api/gov", headers=hs["x@t.io"]); print("outsider", r.status_code); assert r.status_code == 403
print("me x partner?", c.get("/api/me", headers=hs["x@t.io"]).json()["partner"], "p1", c.get("/api/me", headers=hs["p1@t.io"]).json()["partner"])
m = c.get("/api/gov", headers=hs["p5@t.io"]).json()["me"]; print("p5", m); assert m["role"] == "partner" and m["weight"] == 2
print("founding", m["founding"]); assert m["founding"] == ["p1", "p2", "p3", "p4"]
r = c.post("/api/gov", headers=hs["p1@t.io"], json={"title": "Raise listing fee", "body": "to 2 ALTF", "code": "000000"}); print("bad code", r.status_code); assert r.status_code == 400
p = c.post("/api/gov", headers=hs["p1@t.io"], json={"title": "Raise listing fee", "body": "to 2 ALTF", "code": code("p1@t.io")}).json(); pid = p["id"]
for e, ch in [("p1@t.io", "yes"), ("p5@t.io", "no"), ("p2@t.io", "yes")]:
    r = c.post(f"/api/gov/{pid}/vote", headers=hs[e], json={"choice": ch, "code": code(e)}); assert r.status_code == 200, r.text
r = c.post(f"/api/gov/{pid}/vote", headers=hs["p1@t.io"], json={"choice": "no", "code": code("p1@t.io")}); print("double vote", r.json()); assert r.status_code == 400
v = r = c.get("/api/gov", headers=hs["p1@t.io"]).json()["proposals"][0]; print("tally", v["yes"], v["no"]); assert (v["yes"], v["no"]) == (2, 2)
r = c.post(f"/api/gov/{pid}/veto", headers=hs["p5@t.io"], json={"code": code("p5@t.io")}); print("partner veto", r.json()); assert r.status_code == 400
c.post(f"/api/gov/{pid}/veto", headers=hs["p3@t.io"], json={"code": code("p3@t.io")})
print("after 1 founding veto", c.get("/api/gov", headers=hs["p1@t.io"]).json()["proposals"][0]["status"])
print("after 2 founding vetoes", c.post(f"/api/gov/{pid}/veto", headers=hs["p4@t.io"], json={"code": code("p4@t.io")}).json()["status"])
p2 = c.post("/api/gov", headers=hs["founder@example.com"], json={"title": "Open server in Tunisia", "code": code("founder@example.com")}).json()["id"]
print("founder veto", c.post(f"/api/gov/{p2}/veto", headers=hs["founder@example.com"], json={"code": code("founder@example.com")}).json()["status"])
p3 = c.post("/api/gov", headers=hs["p2@t.io"], json={"title": "Add SOL network", "code": code("p2@t.io")}).json()["id"]
c.post(f"/api/gov/{p3}/vote", headers=hs["p2@t.io"], json={"choice": "yes", "code": code("p2@t.io")})
db.expire_all(); pr = db.get(gov.Proposal, p3); pr.ends_at -= timedelta(days=8); db.commit()
print("after 7 days", c.get("/api/gov", headers=hs["p2@t.io"]).json()["proposals"][0]["status"])
# seat passes on when a founding server closes
n = db.scalars(main.select(ServerNode).where(ServerNode.user_id == ids["p1@t.io"])).first(); n.status = "closed"; db.commit()
print("founding after p1 closed", c.get("/api/gov", headers=hs["p2@t.io"]).json()["me"]["founding"])
print("p1 now", c.get("/api/gov", headers=hs["p1@t.io"]).status_code)
print("ALL GOV TESTS PASSED")
