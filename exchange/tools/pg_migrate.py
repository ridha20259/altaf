"""One-time copy of the ALTAF SQLite database into PostgreSQL. Run with ALTAF_DB=<postgres url>."""
import os, sys
from decimal import Decimal
from sqlalchemy import create_engine, func, select, text
sys.path.insert(0, "/opt/altaf")
from app import main  # noqa: F401  (registers every model)
from app.db import Base, engine as dst, init_db

src = create_engine("sqlite:////opt/altaf/data/altaf.db")
init_db()
tables = Base.metadata.sorted_tables
with dst.begin() as d:
    if any(d.execute(select(func.count()).select_from(t)).scalar() for t in tables):
        sys.exit("target database is not empty")
with src.connect() as s, dst.begin() as d:
    for t in tables:
        rows = [dict(r) for r in s.execute(t.select()).mappings()]
        if rows:
            d.execute(t.insert(), rows)
    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) == 1 and pk[0].autoincrement in (True, "auto") and str(pk[0].type).upper().startswith(("INTEGER", "BIGINT")):
            d.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{t.name}\"', '{pk[0].name}'), COALESCE((SELECT MAX(\"{pk[0].name}\") FROM \"{t.name}\"), 0) + 1, false)"))
bad = []
with src.connect() as s, dst.connect() as d:
    for t in tables:
        a = s.execute(select(func.count()).select_from(t)).scalar()
        b = d.execute(select(func.count()).select_from(t)).scalar()
        if a != b:
            bad.append((t.name, a, b))
    from app.db import Balance
    sa = sorted((r.user_id, r.wallet, r.asset, Decimal(r.available), Decimal(r.locked)) for r in s.execute(Balance.__table__.select()))
    sb = sorted((r.user_id, r.wallet, r.asset, Decimal(r.available), Decimal(r.locked)) for r in d.execute(Balance.__table__.select()))
    if sa != sb:
        bad.append(("balances differ", len(sa), len(sb)))
print("tables", len(tables), "mismatches", bad)
sys.exit(1 if bad else 0)
