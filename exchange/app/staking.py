"""On-chain staking of idle hot-wallet ETH (Lido stETH) and SOL (native stake account).
Off until settings staking_enabled=1. The platform keeps `staking_keep_pct` liquid for withdrawals and earns the
difference between the network yield and the Earn APR paid to users (about 20 %)."""
import logging
from decimal import Decimal as D
from .ledger import setting

log = logging.getLogger("staking")
LIDO = "0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84"
LIDO_ABI = [{"inputs": [{"name": "_referral", "type": "address"}], "name": "submit", "outputs": [{"name": "", "type": "uint256"}],
             "stateMutability": "payable", "type": "function"},
            {"inputs": [{"name": "_account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
             "stateMutability": "view", "type": "function"}]


def eth_staked(w3, addr):
    c = w3.eth.contract(address=w3.to_checksum_address(LIDO), abi=LIDO_ABI)
    return D(c.functions.balanceOf(w3.to_checksum_address(addr)).call()) / D(10**18)


def eth_stake(w3, addr, priv, amount):
    """Stake `amount` ETH with Lido from the hot wallet; returns tx hash."""
    a = w3.to_checksum_address(addr)
    c = w3.eth.contract(address=w3.to_checksum_address(LIDO), abi=LIDO_ABI)
    tx = c.functions.submit("0x0000000000000000000000000000000000000000").build_transaction(
        {"from": a, "value": int(D(amount) * 10**18), "gas": 120000, "gasPrice": w3.eth.gas_price,
         "nonce": w3.eth.get_transaction_count(a), "chainId": 1})
    h = w3.eth.send_raw_transaction(w3.eth.account.sign_transaction(tx, private_key=priv).raw_transaction)
    if w3.eth.wait_for_transaction_receipt(h, timeout=300).status != 1:
        raise RuntimeError("lido submit failed")
    return h.hex()


def run(db):
    """Stake hot-wallet ETH above the liquid reserve. SOL delegation is added once a validator is chosen."""
    if not setting(db, "staking_enabled", bool):
        return
    from . import chain as ch
    keep = D(setting(db, "staking_keep_pct") or "30") / 100
    try:
        w3 = ch._w3("ETH")
        addr, priv = ch.hot_key("ETH")
        liquid, staked = ch.hot_balance("ETH", "ETH"), eth_staked(w3, addr)
        target = (liquid + staked) * (1 - keep)
        extra = target - staked
        if extra >= D("0.05"):
            log.info("staking %s ETH with Lido: %s", extra, eth_stake(w3, addr, priv, extra))
    except Exception as e:
        log.warning("eth staking: %s", e)
