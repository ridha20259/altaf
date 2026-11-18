"""Exchange API keys (HMAC-signed requests) and the public market WebSocket."""
import asyncio, hashlib, hmac, json, secrets, time
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, select
from sqlalchemy.orm import Mapped, mapped_column
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from .db import Base, SessionLocal

API_PATHS = ("/api/order", "/api/orders", "/api/balances", "/api/me", "/api/ledger", "/api/futures",
             "/api/book", "/api/trades", "/api/klines", "/api/markets", "/api/assets")
MAX_KEYS = 10
WINDOW_MS = 30_000


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(60), default="")
    can_trade: Mapped[bool] = mapped_column(Boolean, default=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used: Mapped[datetime] = mapped_column(DateTime, nullable=True)


def view(k: ApiKey) -> dict:
    return {"id": k.id, "key": k.key, "label": k.label, "can_trade": k.can_trade,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used": k.last_used.isoformat() if k.last_used else None}


def create(db, uid: int, label: str, can_trade: bool) -> dict:
    n = len(db.scalars(select(ApiKey).where(ApiKey.user_id == uid, ApiKey.revoked == False)).all())  # noqa: E712
    if n >= MAX_KEYS:
        raise ValueError(f"At most {MAX_KEYS} API keys")
    k = ApiKey(user_id=uid, key="ak_" + secrets.token_hex(16), secret=secrets.token_hex(32),
               label=(label or "")[:60], can_trade=bool(can_trade))
    db.add(k)
    db.commit()
    return {**view(k), "secret": k.secret}


def sign(secret: str, ts: str, method: str, path: str, body: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + method.upper().encode() + path.encode() + body, hashlib.sha256).hexdigest()


class ApiKeyAuth(BaseHTTPMiddleware):
    """X-API-KEY + X-API-TS (ms) + X-API-SIGN = hex HMAC-SHA256(secret, ts + METHOD + path?query + body)."""

    async def dispatch(self, request, call_next):
        key = request.headers.get("x-api-key")
        if not key:
            return await call_next(request)
        path = request.url.path
        if not path.startswith(API_PATHS):
            return JSONResponse({"detail": "This endpoint is not available with an API key"}, 403)
        ts, sig = request.headers.get("x-api-ts", ""), request.headers.get("x-api-sign", "")
        if not ts.isdigit() or abs(int(time.time() * 1000) - int(ts)) > WINDOW_MS:
            return JSONResponse({"detail": "Bad or expired X-API-TS"}, 401)
        body = await request.body()
        full = path + (("?" + request.url.query) if request.url.query else "")
        db = SessionLocal()
        try:
            k = db.scalars(select(ApiKey).where(ApiKey.key == key, ApiKey.revoked == False)).first()  # noqa: E712
            if not k or not hmac.compare_digest(sign(k.secret, ts, request.method, full, body), sig):
                return JSONResponse({"detail": "Invalid API key or signature"}, 401)
            if request.method != "GET" and not k.can_trade:
                return JSONResponse({"detail": "Read-only API key"}, 403)
            k.last_used = datetime.utcnow()
            db.commit()
            request.state.api_uid = k.user_id
        finally:
            db.close()
        return await call_next(request)


async def market_ws(ws, symbol: str, book_fn, trades_fn):
    """Push {"book", "trades"} for one market whenever it changes (checked every second)."""
    await ws.accept()
    last = None
    try:
        while True:
            db = SessionLocal()
            try:
                msg = json.dumps({"symbol": symbol, "book": book_fn(db, symbol), "trades": trades_fn(db, symbol)}, default=str)
            finally:
                db.close()
            if msg != last:
                await ws.send_text(msg)
                last = msg
            await asyncio.sleep(1)
    except Exception:
        return
