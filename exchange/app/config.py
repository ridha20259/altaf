import os
from decimal import Decimal

def env(k, d=None):
    return os.environ.get(k, d)

DATABASE_URL = env("ALTAF_DB", "sqlite:///./data/altaf.db")
SECRET_KEY = env("ALTAF_SECRET", "dev-secret-change-me")
MASTER_MNEMONIC = env("ALTAF_MNEMONIC", "")
# Split keys: public account keys derive deposit addresses; hot wallet has its own key;
# the 24 words live only in the signer service (app/signer.py).
XPUB = {"TRON": env("ALTAF_XPUB_TRON", ""), "BSC": env("ALTAF_XPUB_BSC", "")}
HOT_KEY = {"TRON": env("ALTAF_HOT_TRON_KEY", ""), "BSC": env("ALTAF_HOT_BSC_KEY", "")}
SIGNER_URL = env("ALTAF_SIGNER_URL", "")
SIGNER_TOKEN = env("ALTAF_SIGNER_TOKEN", "")
TRONGRID_KEY = env("TRONGRID_KEY", "")
TRON_API = env("TRON_API", "https://api.trongrid.io")
BSC_RPC = env("BSC_RPC", "https://bsc-dataseed.binance.org")
ADMIN_EMAILS = [e.strip().lower() for e in env("ALTAF_ADMINS", "").split(",") if e.strip()]
CHAIN_WORKERS = env("ALTAF_CHAIN", "0") == "1"
SITE_NAME = "ALTAF Exchange"
UPLOAD_DIR = env("ALTAF_UPLOADS", "./data/uploads")

# Default settings (editable in admin, stored in settings table)
DEFAULTS = {
    "maker_fee": "0.001",
    "taker_fee": "0.001",
    "altf_fee_discount": "0.25",
    "altf_price": "25",
    "convert_spread": "0.003",
    "futures_enabled": "0",
    "futures_max_leverage": "50",
    "futures_fee": "0.0005",
    "futures_mmr": "0.005",
    "futures_spread": "0.0003",
    "futures_max_oi_ratio": "3",
    "listing_fee_altf": "1",
    "listing_days": "30",
    "withdrawals_enabled": "1",
    "withdraw_auto_limit_usd": "1000",
    "withdraw_daily_limit_usd": "5000",
    "withdraw_new_account_hours": "24",
    "withdraw_lock_hours_after_change": "24",
    "kyc_required_withdraw": "0",
    "mm_enabled": "0",
    "mm_spread": "0.002",
    "mm_size_usd": "200",
    "mm_levels": "5",
    "mm_markets": "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,TRXUSDT",
    "mm_max_daily_loss_pct": "0.03",
    "mm_max_inventory_pct": "0.5",
    "mm_vol_pause": "0.01",
    "mm_requote": "0.0005",
    "mm_halted": "",
    "mm_day": "",
    "mm_day_start_equity": "0",
    "mm_net_funded_usd": "0",
    "p2p_timeout_min": "15",
    "deposits_enabled": "1",
    "fee_split": "50,25,15,10",
    "factory_fee_altf": "2",
    "factory_net_usd_tron": "25",
    "factory_net_usd_bsc": "5",
    "listed_withdraw_fee_usd": "3",
    "hot_wallet_max_usd": "5000",
    "cold_address_tron": "",
    "cold_address_bsc": "",
    "sweep_min_usd": "50",
    "partner_approvals_needed": "2",
}
