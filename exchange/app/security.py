import base64, hashlib, hmac, json, time, threading
from collections import defaultdict, deque
import bcrypt, pyotp
from .config import SECRET_KEY
from .db import SecurityEvent


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except Exception:
        return False


def _sign(b: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(SECRET_KEY.encode(), b, hashlib.sha256).digest()).decode().rstrip("=")


def make_token(uid: int, ver: int, hours=24 * 7, scope="user") -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "v": ver, "e": int(time.time() + hours * 3600), "s": scope}).encode()).decode().rstrip("=")
    return body + "." + _sign(body.encode())


def read_token(tok: str):
    try:
        body, sig = tok.split(".")
        if not hmac.compare_digest(sig, _sign(body.encode())):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["e"] < time.time():
            return None
        return data
    except Exception:
        return None


def new_totp_secret():
    return pyotp.random_base32()


def check_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)


def totp_uri(secret, email):
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="ALTAF Exchange")


class RateLimiter:
    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, key, limit, window):
        t = time.time()
        with self.lock:
            q = self.hits[key]
            while q and q[0] < t - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True


limiter = RateLimiter()


def log_event(db, kind, uid=None, ip="", detail="", severity="info"):
    db.add(SecurityEvent(user_id=uid, kind=kind, ip=ip[:60], detail=str(detail)[:2000], severity=severity))
