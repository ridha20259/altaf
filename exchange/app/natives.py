"""Native Bitcoin (BIP84 P2WPKH) and Solana support: keys, balances, deposits and transfers.
Shared by the main app (public data only) and the signer (private keys)."""
import base64, os, time
from decimal import Decimal as D
import httpx
from bip_utils import Bip44, Bip44Coins, Bip44Changes, Bip84, Bip84Coins

BTC_API = os.environ.get("BTC_API", "https://blockstream.info/api").rstrip("/")
SOL_RPC = os.environ.get("SOL_RPC", "https://api.mainnet-beta.solana.com")
BTC_CONFIRMATIONS = int(os.environ.get("BTC_CONFIRMATIONS", "2"))
SAT, LAMPORTS = D(10**8), D(10**9)
SOL_FEE = 5000
DUST = 546


# ---------------- Bitcoin ----------------
def btc_derive(seed, index):
    a = Bip84.FromSeed(seed, Bip84Coins.BITCOIN).Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index)
    return a.PublicKey().ToAddress(), a.PrivateKey().Raw().ToHex()


def btc_xpub(seed):
    return Bip84.FromSeed(seed, Bip84Coins.BITCOIN).Purpose().Coin().Account(0).PublicKey().ToExtended()


def btc_address_from_xpub(xpub, index):
    return Bip84.FromExtendedKey(xpub, Bip84Coins.BITCOIN).Change(Bip44Changes.CHAIN_EXT).AddressIndex(index).PublicKey().ToAddress()


def btc_key_address(priv_hex):
    from embit import ec, script
    return script.p2wpkh(ec.PrivateKey(bytes.fromhex(priv_hex)).get_public_key()).address(script.NETWORKS["main"])


def btc_valid(addr):
    from embit import script
    try:
        return script.address_to_scriptpubkey(addr) is not None and addr.lower().startswith(("bc1", "1", "3"))
    except Exception:
        return False


def _btc(path):
    r = httpx.get(BTC_API + path, timeout=20)
    r.raise_for_status()
    return r


def btc_balance(addr):
    c = _btc(f"/address/{addr}").json()["chain_stats"]
    return D(c["funded_txo_sum"] - c["spent_txo_sum"]) / SAT


def btc_deposits(addr):
    """Confirmed incoming transactions: [(txid, amount BTC)]."""
    tip = int(_btc("/blocks/tip/height").text)
    out = []
    for tx in _btc(f"/address/{addr}/txs").json():
        st = tx.get("status", {})
        if not st.get("confirmed") or tip - st["block_height"] + 1 < BTC_CONFIRMATIONS:
            continue
        if any((v.get("prevout") or {}).get("scriptpubkey_address") == addr for v in tx["vin"]):
            continue
        sats = sum(v["value"] for v in tx["vout"] if v.get("scriptpubkey_address") == addr)
        if sats > 0:
            out.append((tx["txid"], D(sats) / SAT))
    return out


def btc_fee_rate():
    est = _btc("/fee-estimates").json()
    return max(2, int(D(str(est.get("6", 5))) + 1))


def btc_build(priv_hex, utxos, to, amount, rate):
    """Signed P2WPKH tx. amount=None or >= total sends everything minus fee. Returns (raw hex, fee sats)."""
    from embit import ec, script
    from embit.transaction import Transaction, TransactionInput, TransactionOutput
    pk = ec.PrivateKey(bytes.fromhex(priv_hex))
    pub = pk.get_public_key()
    spk = script.p2wpkh(pub)
    total = sum(u["value"] for u in utxos)
    if not utxos:
        raise RuntimeError("no confirmed BTC to spend")
    want = None if amount is None else int(D(amount) * SAT)
    sweep_all = want is None or want >= total
    nout = 1 if sweep_all else 2
    fee = int((11 + 68 * len(utxos) + 31 * nout) * rate)
    outs = []
    if sweep_all:
        val = total - fee
        if val <= DUST:
            raise RuntimeError("BTC balance too small for the network fee")
        outs.append(TransactionOutput(val, script.address_to_scriptpubkey(to)))
    else:
        change = total - want - fee
        if change < 0:
            raise RuntimeError("not enough BTC for amount + fee")
        outs.append(TransactionOutput(want, script.address_to_scriptpubkey(to)))
        if change > DUST:
            outs.append(TransactionOutput(change, spk))
        else:
            fee += change
    tx = Transaction(vin=[TransactionInput(bytes.fromhex(u["txid"]), u["vout"]) for u in utxos], vout=outs)
    for i, u in enumerate(utxos):
        h = tx.sighash_segwit(i, script.p2pkh(pub), u["value"])
        tx.vin[i].witness = script.witness_p2wpkh(pk.sign(h), pub)
    return tx.serialize().hex(), fee


def btc_send(priv_hex, frm, to, amount=None):
    utxos = [u for u in _btc(f"/address/{frm}/utxo").json() if u.get("status", {}).get("confirmed")]
    raw, fee = btc_build(priv_hex, utxos, to, amount, btc_fee_rate())
    r = httpx.post(BTC_API + "/tx", content=raw, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"btc broadcast: {r.text[:300]}")
    return r.text.strip(), D(fee) / SAT


# ---------------- Solana ----------------
def sol_derive(seed, index):
    a = Bip44.FromSeed(seed, Bip44Coins.SOLANA).Purpose().Coin().Account(index).Change(Bip44Changes.CHAIN_EXT)
    return a.PublicKey().ToAddress(), a.PrivateKey().Raw().ToHex()


def sol_key_address(priv_hex):
    from solders.keypair import Keypair
    return str(Keypair.from_seed(bytes.fromhex(priv_hex)).pubkey())


def sol_valid(addr):
    from solders.pubkey import Pubkey
    try:
        Pubkey.from_string(addr)
        return 32 <= len(addr) <= 44
    except Exception:
        return False


def _sol(method, params):
    j = httpx.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20).json()
    if "error" in j:
        raise RuntimeError(f"solana {method}: {j['error']}")
    return j["result"]


def sol_balance(addr):
    return D(_sol("getBalance", [addr, {"commitment": "finalized"}])["value"]) / LAMPORTS


def sol_build(priv_hex, to, lamports, blockhash):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    kp = Keypair.from_seed(bytes.fromhex(priv_hex))
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to), lamports=lamports))
    tx = Transaction([kp], Message([ix], kp.pubkey()), Hash.from_string(blockhash))
    return base64.b64encode(bytes(tx)).decode(), str(tx.signatures[0])


def sol_send(priv_hex, frm, to, amount=None):
    """amount=None or >= balance sends everything minus the fee (the account is closed)."""
    have = _sol("getBalance", [frm, {"commitment": "finalized"}])["value"]
    want = None if amount is None else int(D(amount) * LAMPORTS)
    lam = have - SOL_FEE if want is None or want >= have - SOL_FEE else want
    if lam <= 0:
        raise RuntimeError("SOL balance too small for the network fee")
    bh = _sol("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    raw, sig = sol_build(priv_hex, to, lam, bh)
    _sol("sendTransaction", [raw, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    for _ in range(40):
        time.sleep(3)
        st = _sol("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("err"):
            raise RuntimeError(f"solana tx failed: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            return sig, D(SOL_FEE) / LAMPORTS
    raise RuntimeError("solana tx not confirmed in time")
