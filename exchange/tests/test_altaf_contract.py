import json
from web3 import Web3, EthereumTesterProvider

art = json.load(open("token/Altaf.json"))
tp = EthereumTesterProvider()
w3 = Web3(tp)
a = w3.eth.accounts
founder, operator, s1, s2, buyer, hacker = a[0], a[1], a[2], a[3], a[4], a[5]
U = 10**6
C = w3.eth.contract(abi=art["abi"], bytecode=art["bin"])
tx = C.constructor(founder, operator).transact({"from": founder})
t = w3.eth.contract(address=w3.eth.get_transaction_receipt(tx).contractAddress, abi=art["abi"])
bal = lambda x: t.functions.balanceOf(x).call() // U

def fails(fn, frm):
    try:
        fn.transact({"from": frm}); return False
    except Exception:
        return True

def warp(days):
    tp.ethereum_tester.time_travel(w3.eth.get_block("latest").timestamp + int(days * 86400)); tp.ethereum_tester.mine_blocks(1)

assert t.functions.totalSupply().call() == 21_000_000 * U
assert bal(founder) == 5_000_000 and t.functions.reserve().call() == 16_000_000 * U
assert fails(t.functions.openServer(s1), hacker)
t.functions.openServer(s1).transact({"from": operator})
assert bal(s1) == 1_000_000 and t.functions.activeServers().call() == 1
assert fails(t.functions.openServer(s1), operator)
assert t.functions.transferable(s1).call() == 100 * U
t.functions.transfer(buyer, 60 * U).transact({"from": s1})
t.functions.transfer(buyer, 40 * U).transact({"from": s1})
assert fails(t.functions.transfer(buyer, 1 * U), s1), "over quota"
# extra coins bought on the market stay free
t.functions.transfer(s1, 500 * U).transact({"from": founder})
t.functions.transfer(buyer, 500 * U).transact({"from": s1})
assert fails(t.functions.transfer(buyer, 1 * U), s1)
# approve path is locked too
t.functions.approve(hacker, 10**30).transact({"from": s1})
assert fails(t.functions.transferFrom(s1, hacker, 1 * U), hacker)
warp(91)
t.functions.transfer(buyer, 100 * U).transact({"from": s1})
assert fails(t.functions.transfer(buyer, 1 * U), s1)
# close: remaining locked returns to reserve
t.functions.closeServer(s1).transact({"from": operator})
assert bal(s1) == 0 and t.functions.reserve().call() == (16_000_000 - 1_000_000 + 999_800) * U
assert t.functions.activeServers().call() == 0
# re-use for another server
t.functions.openServer(s2).transact({"from": operator})
assert bal(s2) == 1_000_000
# max 16
for i in range(15):
    t.functions.openServer(w3.to_checksum_address("0x" + f"{i+100:040x}")).transact({"from": operator})
last = w3.to_checksum_address("0x" + f"{114:040x}")
assert bal(last) == 999_800, bal(last)  # last slot gets what is left after partners sold 200
assert t.functions.reserve().call() == 0
assert fails(t.functions.openServer(a[6]), operator)
# founder free, can't be a server, only owner changes operator
t.functions.transfer(buyer, 1_000_000 * U).transact({"from": founder})
assert fails(t.functions.openServer(founder), operator)
assert fails(t.functions.setOperator(hacker), operator)
t.functions.setOperator(a[7]).transact({"from": founder})
assert fails(t.functions.closeServer(s2), operator)
# no mint function exists
assert not any(f.get("name", "").lower().startswith("mint") for f in art["abi"])
print("ALL CONTRACT TESTS PASSED")
