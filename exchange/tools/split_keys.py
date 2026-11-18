"""One-time: move the 24 words out of the main app env into the signer env. Run as root on the VPS."""
import json, os, secrets, shutil, sys, time
sys.path.insert(0, "/opt/altaf")
MAIN, SIGN, BK = "/etc/altaf.env", "/etc/altaf-signer.env", "/root/altaf-secrets"

def load(p):
    out = {}
    for l in open(p):
        l = l.strip()
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1)
            out[k] = v.strip().strip('"').strip("'")
    return out

def dump(p, d):
    with open(p, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in d.items()))
    os.chmod(p, 0o600)

env = load(MAIN)
if not env.get("ALTAF_MNEMONIC"):
    sys.exit("already split")
os.makedirs(BK, exist_ok=True)
shutil.copy2(MAIN, f"{BK}/altaf.env.before-split.{int(time.time())}")
os.environ.update(env)
from app import chain
from app.db import SessionLocal, AssetNetwork
from app.ledger import setting
from tronpy.keys import PrivateKey
from eth_account import Account
db = SessionLocal()
hot_tron, hot_bsc = PrivateKey.random().hex(), Account.create().key.hex().removeprefix("0x")
hot = {"TRON": chain.key_address("TRON", hot_tron), "BSC": chain.key_address("BSC", hot_bsc)}
token = secrets.token_urlsafe(32)
contracts = sorted({n.contract for n in db.query(AssetNetwork).all() if n.contract})
dump(f"{BK}/hot-keys.env", {"ALTAF_HOT_TRON_KEY": hot_tron, "ALTAF_HOT_BSC_KEY": hot_bsc, "HOT_TRON": hot["TRON"], "HOT_BSC": hot["BSC"]})
sign = {"ALTAF_MNEMONIC": env["ALTAF_MNEMONIC"], "SIGNER_TOKEN": token, "SIGNER_HOT_TRON": hot["TRON"], "SIGNER_HOT_BSC": hot["BSC"],
        "SIGNER_CONTRACTS": ",".join(contracts), "SIGNER_ALTF_CONTRACT": setting(db, "altf_contract") or ""}
for k in ("TRONGRID_KEY", "TRON_API", "BSC_RPC"):
    if env.get(k):
        sign[k] = env[k]
new = {k: v for k, v in env.items() if k != "ALTAF_MNEMONIC"}
new.update({"ALTAF_XPUB_TRON": chain.account_xpub("TRON"), "ALTAF_XPUB_BSC": chain.account_xpub("BSC"),
            "ALTAF_HOT_TRON_KEY": hot_tron, "ALTAF_HOT_BSC_KEY": hot_bsc,
            "ALTAF_SIGNER_URL": "http://127.0.0.1:8101", "ALTAF_SIGNER_TOKEN": token})
dump(SIGN, sign)
dump(MAIN, new)
print(json.dumps({"hot": hot, "contracts": contracts, "altf": sign["SIGNER_ALTF_CONTRACT"],
                  "legacy_hot": {c: chain.derive(c, 0)[0] for c in ("TRON", "BSC")}}))
