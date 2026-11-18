"""Partner server network: verify running nodes, open/close their 1M ALTF allocation on-chain."""
import json, logging, secrets, threading, time
from datetime import datetime, timedelta
from pathlib import Path
import httpx
from sqlalchemy import String, Integer, DateTime, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, SessionLocal, now
from .ledger import setting
from .db import WRITE_LOCK

log = logging.getLogger("servers")
TOKEN_DIR = Path(__file__).resolve().parent.parent / "token"
OPERATOR_INDEX = 9000
UP_HOURS_TO_OPEN = 24
DOWN_HOURS_TO_CLOSE = 72


class ServerNode(Base):
    __tablename__ = "server_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    address: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/active/closed/failed
    up_since: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_ok: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_check: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    open_tx: Mapped[str] = mapped_column(String(120), default="")
    close_tx: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def probe(url, address):
    """Node must sign a fresh challenge with the private key of its TRON address."""
    from tronpy.keys import Signature
    c = secrets.token_hex(16)
    r = httpx.get(url.rstrip("/") + "/altaf-node/health", params={"c": c}, timeout=15)
    r.raise_for_status()
    j = r.json()
    sig = Signature.fromhex(j["sig"])
    who = sig.recover_public_key_from_msg(c.encode()).to_base58check_address()
    return who == address and j.get("address") == address


def operator():
    from .chain import derive
    return derive("TRON", OPERATOR_INDEX)


def _contract(tron):
    addr = setting_value("altf_contract")
    if not addr:
        raise RuntimeError("ALTF contract not set")
    c = tron.get_contract(addr)
    c.abi = json.loads((TOKEN_DIR / "Altaf.json").read_text())["abi"]
    return c


def setting_value(key):
    db = SessionLocal()
    try:
        return setting(db, key)
    finally:
        db.close()


def call(fn, server_addr):
    from .config import SIGNER_URL
    if SIGNER_URL:
        from .chain import signer
        return signer("/operator", {"fn": fn, "server": server_addr})["txid"]
    from tronpy.keys import PrivateKey
    from .chain import _tron
    tron = _tron()
    op_addr, priv = operator()
    c = _contract(tron)
    tx = getattr(c.functions, fn)(server_addr).with_owner(op_addr).fee_limit(30_000_000).build().sign(PrivateKey(bytes.fromhex(priv)))
    res = tx.broadcast().wait(timeout=60)
    if res.get("receipt", {}).get("result") != "SUCCESS":
        raise RuntimeError(f"{fn} failed: {res.get('receipt')}")
    return tx.txid


def check_once(db):
    t = datetime.utcnow()
    for n in db.scalars(select(ServerNode).where(ServerNode.status.in_(["pending", "active"]))):
        try:
            ok = probe(n.url, n.address)
        except Exception as e:
            ok, n.error = False, str(e)[:300]
        n.last_check = t
        if ok:
            n.last_ok, n.error = t, ""
            n.up_since = n.up_since or t
        else:
            n.up_since = None
        if n.status == "pending" and n.up_since and t - n.up_since >= timedelta(hours=UP_HOURS_TO_OPEN):
            try:
                n.open_tx, n.status = call("openServer", n.address), "active"
            except Exception as e:
                n.error = str(e)[:300]
        elif n.status == "active" and (not n.last_ok or t - n.last_ok >= timedelta(hours=DOWN_HOURS_TO_CLOSE)):
            try:
                n.close_tx, n.status = call("closeServer", n.address), "closed"
            except Exception as e:
                n.error = str(e)[:300]
        db.commit()


def loop():
    while True:
        db = SessionLocal()
        try:
            with WRITE_LOCK:
                check_once(db)
        except Exception:
            log.exception("server monitor")
        finally:
            db.close()
        time.sleep(3600)


def start():
    threading.Thread(target=loop, daemon=True, name="servers").start()
