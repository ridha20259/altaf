"""ALTAF native chain (altafd) support for the exchange: deposit addresses, scanning, sweeps, withdrawals.
Keys: ALTAF_NATIVE_SEED (64 hex) is the hot/bridge key; deposit keys are HMAC(seed, "deposit/<i>")."""
import base64, hashlib, hmac, struct
from decimal import Decimal as D
import httpx
from nacl.signing import SigningKey
from .config import env

NODE = env("ALTAF_NODE_URL", "http://127.0.0.1:8545")
SEED = env("ALTAF_NATIVE_SEED", "")
UNIT = D(10**6)
FEE = 10_000  # 0.01 ALTF network fee
CONFIRMATIONS = 6
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(b):
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def _sha(b):
    return hashlib.sha256(b).digest()


def address_of(pub):
    raw = b"\x17" + _sha(pub)[:20]
    return _b58(raw + _sha(_sha(raw))[:4])


def valid(addr):
    try:
        n = 0
        for c in addr:
            n = n * 58 + B58.index(c)
        b = n.to_bytes(25, "big")
        return b[0] == 0x17 and _sha(_sha(b[:21]))[:4] == b[21:]
    except Exception:
        return False


def _key(index):
    if not SEED:
        raise RuntimeError("ALTAF_NATIVE_SEED not set")
    s = bytes.fromhex(SEED)
    return SigningKey(s if index == 0 else hmac.new(s, b"deposit/%d" % index, hashlib.sha256).digest())


def derive(index):
    k = _key(index)
    return address_of(bytes(k.verify_key)), k


def _get(path):
    r = httpx.get(NODE + path, timeout=20)
    r.raise_for_status()
    return r.json()


def balance(addr):
    return D(_get("/account/" + addr)["balance"]) / UNIT


def height():
    return _get("/status")["height"]


def scan(addr):
    """Confirmed incoming transfers: [(amount, txid)]."""
    tip = height()
    return [(D(x["amount"]) / UNIT, x["txid"]) for x in _get("/history/" + addr)
            if x["kind"] == "receive" and tip - x["height"] + 1 >= CONFIRMATIONS]


def _signing_bytes(pub, to, amount, fee, nonce, memo=b""):
    to = to.encode()
    return b"ALTF-TX-v1" + pub + bytes([len(to)]) + to + struct.pack("<QQQ", amount, fee, nonce) + bytes([len(memo)]) + memo


def send(index, to, amount):
    """Send `amount` ALTF (Decimal) from key `index` (0 = hot). Returns txid."""
    frm, k = derive(index)
    if not valid(to):
        raise RuntimeError("bad ALTAF address")
    units = int(D(amount) * UNIT)
    acc = _get("/account/" + frm)
    pend = sum(1 for t in _get("/mempool") if t["from"] == base64.b64encode(bytes(k.verify_key)).decode())
    nonce = acc["nonce"] + 1 + pend
    pub = bytes(k.verify_key)
    sig = k.sign(_signing_bytes(pub, to, units, FEE, nonce)).signature
    r = httpx.post(NODE + "/tx", json={"from": base64.b64encode(pub).decode(), "to": to, "amount": units, "fee": FEE,
                                       "nonce": nonce, "sig": base64.b64encode(sig).decode()}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError("altafd: " + r.text[:300])
    return r.json()["txid"]
