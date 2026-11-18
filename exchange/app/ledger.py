from decimal import Decimal
from sqlalchemy import select
from .db import Balance, Ledger, User, Setting
from .config import DEFAULTS

D = Decimal
ZERO = D(0)
_settings_cache = {}


class InsufficientFunds(Exception):
    pass


def setting(db, key, cast=str):
    if key not in _settings_cache:
        row = db.get(Setting, key)
        _settings_cache[key] = row.value if row else DEFAULTS.get(key, "")
    v = _settings_cache[key]
    if cast is D:
        return D(v or "0")
    if cast is bool:
        return v in ("1", "true", "True", "yes")
    return cast(v)


def set_setting(db, key, value):
    row = db.get(Setting, key)
    if row:
        row.value = str(value)
    else:
        db.add(Setting(key=key, value=str(value)))
    _settings_cache.pop(key, None)


def all_settings(db):
    out = dict(DEFAULTS)
    for r in db.scalars(select(Setting)):
        out[r.key] = r.value
    return out


def bal(db, uid, asset, wallet="spot", lock=True):
    q = select(Balance).where(Balance.user_id == uid, Balance.wallet == wallet, Balance.asset == asset)
    if lock and db.bind.dialect.name == "postgresql":
        q = q.with_for_update()
    b = db.scalars(q).first()
    if not b:
        b = Balance(user_id=uid, wallet=wallet, asset=asset, available=ZERO, locked=ZERO)
        db.add(b)
        db.flush()
    return b


def move(db, uid, asset, d_avail=ZERO, d_locked=ZERO, kind="adjust", ref="", wallet="spot", allow_negative=False):
    d_avail, d_locked = D(d_avail), D(d_locked)
    if d_avail == 0 and d_locked == 0:
        return
    b = bal(db, uid, asset, wallet)
    na, nl = (b.available or ZERO) + d_avail, (b.locked or ZERO) + d_locked
    if not allow_negative and (na < 0 or nl < 0):
        raise InsufficientFunds(f"Insufficient {asset} balance")
    b.available, b.locked = na, nl
    db.add(Ledger(user_id=uid, wallet=wallet, asset=asset, d_available=d_avail, d_locked=d_locked, kind=kind, ref=str(ref)))


def transfer(db, from_uid, to_uid, asset, amount, kind, ref="", from_wallet="spot", to_wallet="spot", from_locked=False):
    amount = D(amount)
    if amount <= 0:
        return
    if from_locked:
        move(db, from_uid, asset, d_locked=-amount, kind=kind, ref=ref, wallet=from_wallet)
    else:
        move(db, from_uid, asset, d_avail=-amount, kind=kind, ref=ref, wallet=from_wallet)
    move(db, to_uid, asset, d_avail=amount, kind=kind, ref=ref, wallet=to_wallet)


_house = {}


def house_id(db, name="house"):
    if name not in _house:
        u = db.scalars(select(User).where(User.email == f"{name}@altaf.system")).first()
        _house[name] = u.id
    return _house[name]
