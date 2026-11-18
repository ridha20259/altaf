package core

import (
	"encoding/hex"
	"math/big"
	"testing"
)

const tokenBin = "608060405234801561000f575f80fd5b506040516102ff3803806102ff83398101604081905261002e91610047565b335f90815260208190526040902081905560015561005e565b5f60208284031215610057575f80fd5b5051919050565b6102948061006b5f395ff3fe608060405260043610610041575f3560e01c80632ddbd13a1461004c5780633d79d1c81461007457806370a0823114610086578063a9059cbb146100b1575f80fd5b3661004857005b5f80fd5b348015610057575f80fd5b5061006160015481565b6040519081526020015b60405180910390f35b34801561007f575f80fd5b5047610061565b348015610091575f80fd5b506100616100a03660046101dc565b5f6020819052908152604090205481565b3480156100bc575f80fd5b506100d06100cb3660046101fc565b6100e0565b604051901515815260200161006b565b335f908152602081905260408120548211156101285760405162461bcd60e51b81526020600482015260036024820152626c6f7760e81b604482015260640160405180910390fd5b335f9081526020819052604081208054849290610146908490610238565b90915550506001600160a01b0383165f908152602081905260408120805484929061017290849061024b565b90915550506040518281526001600160a01b0384169033907fddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef9060200160405180910390a35060015b92915050565b80356001600160a01b03811681146101d7575f80fd5b919050565b5f602082840312156101ec575f80fd5b6101f5826101c1565b9392505050565b5f806040838503121561020d575f80fd5b610216836101c1565b946020939093013593505050565b634e487b7160e01b5f52601160045260245ffd5b818103818111156101bb576101bb610224565b808201808211156101bb576101bb61022456fea26469706673582212208b7ef9b5cff16ab1df39105a8aefdda4c38fcea7ac1d0ab254cda4026d90de9864736f6c63430008140033"

func word(b []byte) []byte { w := make([]byte, 32); copy(w[32-len(b):], b); return w }

func TestContracts(t *testing.T) {
	c, _ := testChain(t)
	k, me := key(7)
	_, bob := key(8)
	for i := 0; i < 3; i++ {
		if err := c.AddBlock(mine(t, c, AlgoCPU, me, nil)); err != nil {
			t.Fatal(err)
		}
	}
	send := func(tx Tx) Hash {
		tx.Nonce = c.Account(me).Nonce + 1
		if tx.Fee == 0 {
			tx.Fee = MinFee
		}
		tx.Sign(k)
		if err := c.AddTx(tx); err != nil {
			t.Fatal(err)
		}
		if err := c.AddBlock(mine(t, c, AlgoCPU, me, nil)); err != nil {
			t.Fatal(err)
		}
		return tx.ID()
	}
	code, _ := hex.DecodeString(tokenBin)
	id := send(Tx{Data: append(code, word(big.NewInt(1000).Bytes())...)})
	r, ok := c.Receipt(id)
	if !ok || r.Status != 1 || r.Contract == "" {
		t.Fatalf("deploy receipt %+v", r)
	}
	ca := r.Contract
	if len(c.Code(ca)) == 0 {
		t.Fatal("no code stored")
	}
	balOf := func(a string) uint64 {
		sel, _ := hex.DecodeString("70a08231")
		e, _ := EvmAddress(a)
		rr := c.CallContract("", ca, append(sel, word(e.Bytes())...), 0)
		if rr.Status != 1 {
			t.Fatalf("call failed %s", rr.Error)
		}
		return new(big.Int).SetBytes(rr.Return).Uint64()
	}
	if balOf(me) != 1000 {
		t.Fatalf("deployer token balance %d", balOf(me))
	}
	sel, _ := hex.DecodeString("a9059cbb")
	eb, _ := EvmAddress(bob)
	id = send(Tx{To: ca, Data: append(append(sel, word(eb.Bytes())...), word(big.NewInt(250).Bytes())...)})
	if r, _ := c.Receipt(id); r.Status != 1 || len(r.Logs) != 1 {
		t.Fatalf("transfer receipt %+v", r)
	}
	if balOf(me) != 750 || balOf(bob) != 250 {
		t.Fatalf("balances %d %d", balOf(me), balOf(bob))
	}
	// failing call (not enough tokens): fee paid, state unchanged
	before := c.Account(me).Balance
	id = send(Tx{To: ca, Data: append(append(sel, word(eb.Bytes())...), word(big.NewInt(5000).Bytes())...)})
	if r, _ := c.Receipt(id); r.Status != 0 {
		t.Fatal("expected revert")
	}
	if balOf(me) != 750 {
		t.Fatal("reverted call changed state")
	}
	_ = before
	// plain ALTF payment to the contract runs receive()
	send(Tx{To: ca, Amount: 3 * Unit})
	if c.Account(ca).Balance != 3*Unit {
		t.Fatalf("contract ALTF balance %d", c.Account(ca).Balance)
	}
	// undo of the last block restores storage and balances
	c.mu.Lock()
	if err := c.disconnectTip(); err != nil {
		c.mu.Unlock()
		t.Fatal(err)
	}
	c.mu.Unlock()
	if c.Account(ca).Balance != 0 || balOf(me) != 750 {
		t.Fatal("undo failed")
	}
	c.mu.Lock()
	c.disconnectTip()
	c.disconnectTip()
	c.mu.Unlock()
	if balOf(me) != 1000 || balOf(bob) != 0 {
		t.Fatalf("undo of transfer failed %d %d", balOf(me), balOf(bob))
	}
}
