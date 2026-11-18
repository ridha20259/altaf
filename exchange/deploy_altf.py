"""Deploy ALTF on TRON mainnet: constructor(founder, operator). Prints contract + txid."""
import json, os, sys
from app import listing, servers
FOUNDER = "TTmpMwovWMqAS9qhkvNfVi2G7nxm74BHjm"
d = json.load(open("secrets/deployer.json"))
art = json.load(open("token/Altaf.json")); art["name"] = "Altaf"
op = servers.operator()[0]
args = [listing._tron_addr_hex20(FOUNDER), listing._tron_addr_hex20(op)]
if "--dry" in sys.argv:
    from eth_abi import encode
    print("operator", op, "args", encode(["address", "address"], args).hex()); sys.exit()
contract, txid = listing.deploy_tron(art, ["address", "address"], args, d["priv"], fee_limit_trx=140)
json.dump({"contract": contract, "txid": txid, "operator": op}, open("secrets/altf_deploy.json", "w"))
print("CONTRACT", contract, "TX", txid)
