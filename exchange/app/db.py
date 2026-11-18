import threading
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (create_engine, String, Integer, Boolean, DateTime, Numeric, Text,
                        ForeignKey, UniqueConstraint, Index, BigInteger)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator, NUMERIC
from .config import DATABASE_URL

_kw = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}
engine = create_engine(DATABASE_URL, **_kw)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
WRITE_LOCK = threading.RLock()



class _RawNumeric(NUMERIC):
    def bind_processor(self, dialect):
        return None

    def result_processor(self, dialect, coltype):
        return None


class Dec(TypeDecorator):
    """Exact Decimal round-trip. SQLite has no decimal type, so values are bound as text and read back via the shortest float repr."""
    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(_RawNumeric(38, 18) if dialect.name == "sqlite" else Numeric(38, 18))

    def process_bind_param(self, v, dialect):
        if v is None or dialect.name != "sqlite":
            return v
        return format(v if isinstance(v, Decimal) else Decimal(str(v)), "f")

    def process_result_value(self, v, dialect):
        if v is None or isinstance(v, Decimal):
            return v
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))


N = lambda: Dec()
now = datetime.utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    pw_hash: Mapped[str] = mapped_column(String(200))
    totp_secret: Mapped[str] = mapped_column(String(64), default="")
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    kyc_status: Mapped[str] = mapped_column(String(20), default="none")  # none/pending/approved/rejected
    kyc_name: Mapped[str] = mapped_column(String(200), default="")
    kyc_country: Mapped[str] = mapped_column(String(60), default="")
    kyc_files: Mapped[str] = mapped_column(Text, default="")
    pay_fee_altf: Mapped[bool] = mapped_column(Boolean, default=True)
    nickname: Mapped[str] = mapped_column(String(60), default="")
    security_changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Asset(Base):
    __tablename__ = "assets"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    precision: Mapped[int] = mapped_column(Integer, default=8)
    logo: Mapped[str] = mapped_column(Text, default="")
    price_source: Mapped[str] = mapped_column(String(20), default="binance")  # binance / internal / fixed
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    deposit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    withdraw_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    listed_by: Mapped[int] = mapped_column(Integer, nullable=True)
    listing_expires: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    website: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[int] = mapped_column(Integer, default=100)
    risk_level: Mapped[str] = mapped_column(String(10), default="ok")  # ok / watch / danger / banned
    risk_notes: Mapped[str] = mapped_column(Text, default="")
    risk_checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AssetNetwork(Base):
    """An asset on a chain: native coin (contract='') or token contract."""
    __tablename__ = "asset_networks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(20), ForeignKey("assets.symbol"), index=True)
    chain: Mapped[str] = mapped_column(String(10))  # TRON / BSC
    contract: Mapped[str] = mapped_column(String(80), default="")
    decimals: Mapped[int] = mapped_column(Integer, default=18)
    withdraw_fee: Mapped[Decimal] = mapped_column(N(), default=0)
    min_withdraw: Mapped[Decimal] = mapped_column(N(), default=0)
    min_deposit: Mapped[Decimal] = mapped_column(N(), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("asset", "chain"),)


class Balance(Base):
    __tablename__ = "balances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    wallet: Mapped[str] = mapped_column(String(10), default="spot")  # spot / futures / earn / p2p
    asset: Mapped[str] = mapped_column(String(20))
    available: Mapped[Decimal] = mapped_column(N(), default=0)
    locked: Mapped[Decimal] = mapped_column(N(), default=0)
    __table_args__ = (UniqueConstraint("user_id", "wallet", "asset"),)


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    wallet: Mapped[str] = mapped_column(String(10))
    asset: Mapped[str] = mapped_column(String(20))
    d_available: Mapped[Decimal] = mapped_column(N(), default=0)
    d_locked: Mapped[Decimal] = mapped_column(N(), default=0)
    kind: Mapped[str] = mapped_column(String(30))
    ref: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Market(Base):
    __tablename__ = "markets"
    symbol: Mapped[str] = mapped_column(String(30), primary_key=True)
    base: Mapped[str] = mapped_column(String(20))
    quote: Mapped[str] = mapped_column(String(20))
    price_prec: Mapped[int] = mapped_column(Integer, default=2)
    qty_prec: Mapped[int] = mapped_column(Integer, default=5)
    min_notional: Mapped[Decimal] = mapped_column(N(), default=Decimal("1"))
    external: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    futures: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=100)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    market: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(4))  # buy/sell
    type: Mapped[str] = mapped_column(String(10))  # limit/market
    price: Mapped[Decimal] = mapped_column(N(), nullable=True)
    qty: Mapped[Decimal] = mapped_column(N(), default=0)
    quote_qty: Mapped[Decimal] = mapped_column(N(), default=0)  # market buy by quote
    filled: Mapped[Decimal] = mapped_column(N(), default=0)
    filled_quote: Mapped[Decimal] = mapped_column(N(), default=0)
    locked_left: Mapped[Decimal] = mapped_column(N(), default=0)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/filled/cancelled
    is_mm: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_book", "market", "status", "side", "price"),)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(30), index=True)
    price: Mapped[Decimal] = mapped_column(N())
    qty: Mapped[Decimal] = mapped_column(N())
    buy_order: Mapped[int] = mapped_column(Integer)
    sell_order: Mapped[int] = mapped_column(Integer)
    buyer: Mapped[int] = mapped_column(Integer, index=True)
    seller: Mapped[int] = mapped_column(Integer, index=True)
    taker_side: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    market: Mapped[str] = mapped_column(String(30))
    side: Mapped[str] = mapped_column(String(5))  # long/short
    size: Mapped[Decimal] = mapped_column(N())
    entry: Mapped[Decimal] = mapped_column(N())
    margin: Mapped[Decimal] = mapped_column(N())
    leverage: Mapped[int] = mapped_column(Integer)
    liq_price: Mapped[Decimal] = mapped_column(N())
    tp: Mapped[Decimal] = mapped_column(N(), nullable=True)
    sl: Mapped[Decimal] = mapped_column(N(), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/closed/liquidated
    exit_price: Mapped[Decimal] = mapped_column(N(), nullable=True)
    pnl: Mapped[Decimal] = mapped_column(N(), default=0)
    fee: Mapped[Decimal] = mapped_column(N(), default=0)
    close_reason: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    closed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class EarnProduct(Base):
    __tablename__ = "earn_products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(20))
    apr: Mapped[Decimal] = mapped_column(N())
    lock_days: Mapped[int] = mapped_column(Integer, default=0)
    min_amount: Mapped[Decimal] = mapped_column(N(), default=0)
    cap: Mapped[Decimal] = mapped_column(N(), default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EarnStake(Base):
    __tablename__ = "earn_stakes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    asset: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(N())
    earned: Mapped[Decimal] = mapped_column(N(), default=0)
    status: Mapped[str] = mapped_column(String(12), default="active")
    unlock_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_accrual: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class P2PAd(Base):
    __tablename__ = "p2p_ads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    side: Mapped[str] = mapped_column(String(4))  # sell = advertiser sells crypto
    asset: Mapped[str] = mapped_column(String(20))
    fiat: Mapped[str] = mapped_column(String(10), default="TND")
    price: Mapped[Decimal] = mapped_column(N())
    amount_left: Mapped[Decimal] = mapped_column(N())
    min_fiat: Mapped[Decimal] = mapped_column(N(), default=0)
    max_fiat: Mapped[Decimal] = mapped_column(N(), default=0)
    methods: Mapped[str] = mapped_column(String(300), default="")
    terms: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class P2POrder(Base):
    __tablename__ = "p2p_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ad_id: Mapped[int] = mapped_column(Integer, index=True)
    buyer: Mapped[int] = mapped_column(Integer, index=True)
    seller: Mapped[int] = mapped_column(Integer, index=True)
    asset: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(N())
    price: Mapped[Decimal] = mapped_column(N())
    fiat: Mapped[str] = mapped_column(String(10))
    fiat_amount: Mapped[Decimal] = mapped_column(N())
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/paid/released/cancelled/dispute
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class P2PMessage(Base):
    __tablename__ = "p2p_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DepositAddress(Base):
    __tablename__ = "deposit_addresses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chain: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(80), index=True)
    hd_index: Mapped[int] = mapped_column(Integer)
    last_scan: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (UniqueConstraint("user_id", "chain"),)


class Sweep(Base):
    """On-chain movement touching a deposit address that is not a user deposit: sweep out, gas in, gas spent."""
    __tablename__ = "sweeps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(80), index=True)
    asset: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(12))  # out | gas_in | gas_used | cold
    amount: Mapped[Decimal] = mapped_column(N())
    txid: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Deposit(Base):
    __tablename__ = "deposits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chain: Mapped[str] = mapped_column(String(10))
    asset: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(N())
    txid: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="credited")
    swept: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("chain", "txid", "address", "asset"),)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    asset: Mapped[str] = mapped_column(String(20))
    chain: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(N())
    fee: Mapped[Decimal] = mapped_column(N(), default=0)
    usd_value: Mapped[Decimal] = mapped_column(N(), default=0)
    status: Mapped[str] = mapped_column(String(14), default="pending")  # pending/review/sending/sent/rejected/failed
    risk_reason: Mapped[str] = mapped_column(String(300), default="")
    txid: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class WhitelistAddress(Base):
    __tablename__ = "whitelist_addresses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chain: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    ip: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), default="info")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    asset: Mapped[str] = mapped_column(String(20))
    chain: Mapped[str] = mapped_column(String(10))
    contract: Mapped[str] = mapped_column(String(80))
    paid_altf: Mapped[Decimal] = mapped_column(N(), default=0)
    status: Mapped[str] = mapped_column(String(12), default="active")  # active/delisted
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TokenJob(Base):
    __tablename__ = "token_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    chain: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(60))
    symbol: Mapped[str] = mapped_column(String(20))
    decimals: Mapped[int] = mapped_column(Integer, default=6)
    supply: Mapped[Decimal] = mapped_column(N())
    owner_address: Mapped[str] = mapped_column(String(80))
    website: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    logo: Mapped[str] = mapped_column(Text, default="")
    list_it: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_usdt: Mapped[Decimal] = mapped_column(N(), default=0)
    paid_altf: Mapped[Decimal] = mapped_column(N(), default=0)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued/deploying/done/failed
    contract: Mapped[str] = mapped_column(String(80), default="")
    txid: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)



class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(8))  # grid | dca | copy
    market: Mapped[str] = mapped_column(String(30))
    params: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(10), default="running", index=True)
    pnl: Mapped[Decimal] = mapped_column(N(), default=0)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class BotOrder(Base):
    __tablename__ = "bot_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(Integer, index=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    level: Mapped[int] = mapped_column(Integer, default=-1)
    side: Mapped[str] = mapped_column(String(4))
    handled: Mapped[bool] = mapped_column(Boolean, default=False)


class CopyLeader(Base):
    __tablename__ = "copy_leaders"
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def init_db():
    Base.metadata.create_all(engine)
