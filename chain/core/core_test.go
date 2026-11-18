package core

import (
	"crypto/ed25519"
	"path/filepath"
	"testing"
	"time"
)

func key(seed byte) (ed25519.PrivateKey, string) {
	s := make([]byte, 32)
	s[0] = seed
	k := ed25519.NewKeyFromSeed(s)
	return k, AddressFromPub(k.Public().(ed25519.PublicKey))
}

func testChain(t *testing.T) (*Chain, Params) {
	UseNetwork("regtest")
	_, f := key(1)
	_, br := key(2)
	p := Params{Founder: f, Bridge: br, GenesisTime: uint32(time.Now().Unix()) - 100000, Network: "regtest"}
	c, err := OpenChain(filepath.Join(t.TempDir(), "c.db"), p)
	if err != nil {
		t.Fatal(err)
	}
	return c, p
}

var clock = uint32(time.Now().Unix()) - 90000

func mine(t *testing.T, c *Chain, algo int, miner string, prev *Hash) *Block {
	tp, err := c.Template(algo, miner)
	if err != nil {
		t.Fatal(err)
	}
	if prev != nil {
		c.mu.RLock()
		pm := c.index[*prev]
		c.mu.RUnlock()
		pre, _ := CoinbasePrefix(pm.Height+1, miner)
		tp.Prev, tp.Prefix, tp.Txs = *prev, pre, nil
		c.mu.RLock()
		tp.Bits = c.expectedBits(*prev, algo)
		c.mu.RUnlock()
	}
	clock += 240
	b := &Block{Coinbase: append(append(tp.Prefix, make([]byte, 8)...), "t"...), Txs: tp.Txs}
	b.Header = Header{Version: tp.Version, Prev: tp.Prev, Time: clock, Bits: tp.Bits}
	b.Header.Merkle = b.ComputeMerkle()
	for !CheckPow(&b.Header) {
		b.Header.Nonce++
	}
	return b
}

func TestAddressRoundtrip(t *testing.T) {
	_, a := key(9)
	if a[0] != 'A' || !ValidAddress(a) {
		t.Fatal("address", a)
	}
	if ValidAddress(a[:len(a)-1] + "x") {
		t.Fatal("checksum not enforced")
	}
}

func TestCompact(t *testing.T) {
	for _, c := range []uint32{0x1d00ffff, 0x1f00ffff, 0x207fffff, 0x1b0404cb} {
		if BigToCompact(CompactToBig(c)) != c {
			t.Fatalf("%x", c)
		}
	}
}

func TestMiningRewardsTxBurnAndReorg(t *testing.T) {
	c, p := testChain(t)
	mk, m := key(3)
	for i := 0; i < 4; i++ {
		b := mine(t, c, i%2, m, nil)
		if err := c.AddBlock(b); err != nil {
			t.Fatal(i, err)
		}
	}
	minerPart := Phase1Reward - Phase1Reward*FounderPercent/100
	if got := c.Account(m).Balance; got != 4*minerPart {
		t.Fatalf("miner balance %d", got)
	}
	if got := c.Account(p.Founder).Balance; got != 4*(Phase1Reward*FounderPercent/100) {
		t.Fatalf("founder %d", got)
	}
	if g := c.Globals(); g.PoolLeft != MiningPool-4*Phase1Reward || g.Supply != TotalSupply {
		t.Fatalf("globals %+v", g)
	}
	_, r := key(4)
	tx := Tx{To: r, Amount: 5 * Unit, Fee: MinFee, Nonce: 1}
	tx.Sign(mk)
	if err := c.AddTx(tx); err != nil {
		t.Fatal(err)
	}
	dup := tx
	dup.Nonce = 1
	dup.Sign(mk)
	if err := c.AddTx(dup); err != ErrKnown {
		t.Fatal("dup accepted", err)
	}
	fork, _ := c.Tip()
	b := mine(t, c, AlgoSHA, m, nil)
	if len(b.Txs) != 1 {
		t.Fatal("tx not in template")
	}
	if err := c.AddBlock(b); err != nil {
		t.Fatal(err)
	}
	if c.Account(r).Balance != 5*Unit {
		t.Fatal("recipient")
	}
	g := c.Globals()
	if g.Burned != MinFee/2 || g.Supply != TotalSupply-MinFee/2 {
		t.Fatalf("burn %+v", g)
	}
	// competing fork of 2 blocks from `fork` without the tx -> reorg
	_, m2 := key(5)
	b1 := mine(t, c, AlgoCPU, m2, &fork)
	if err := c.AddBlock(b1); err != nil {
		t.Fatal(err)
	}
	h1 := b1.Header.Hash()
	b2 := mine(t, c, AlgoSHA, m2, &h1)
	if err := c.AddBlock(b2); err != nil {
		t.Fatal(err)
	}
	if tip, _ := c.Tip(); tip != b2.Header.Hash() {
		t.Fatal("no reorg")
	}
	if c.Account(r).Balance != 0 || len(c.Mempool) != 1 || c.Globals().Burned != 0 {
		t.Fatal("reorg state wrong")
	}
	if c.Account(m).Balance != 4*minerPart {
		t.Fatal("miner after reorg")
	}
	// reopen from disk: replay must give same state
	g1 := c.Globals()
	path := c.db.Path()
	c.Close()
	c2, err := OpenChain(path, p)
	if err != nil {
		t.Fatal(err)
	}
	if c2.Globals() != g1 || c2.Height() != 6 || c2.Account(m2).Balance != 2*minerPart {
		t.Fatalf("replay mismatch %+v %d", c2.Globals(), c2.Height())
	}
}

func TestBadBlocksRejected(t *testing.T) {
	c, _ := testChain(t)
	_, m := key(3)
	b := mine(t, c, AlgoSHA, m, nil)
	bad := *b
	bad.Header.Bits = 0x207ffffe
	if c.AddBlock(&bad) == nil {
		t.Fatal("wrong bits accepted")
	}
	bad = *b
	bad.Coinbase = append([]byte(nil), b.Coinbase...)
	bad.Coinbase[len(bad.Coinbase)-1] = 'x'
	if c.AddBlock(&bad) == nil {
		t.Fatal("bad merkle accepted")
	}
	if err := c.AddBlock(b); err != nil {
		t.Fatal(err)
	}
}

func TestMinerCapAndPhase2(t *testing.T) {
	_, f := key(1)
	_, br := key(2)
	_, m := key(3)
	s := NewState(Params{Founder: f, Bridge: br})
	s.Accounts[m] = &Account{Mined: MinerCap - 1}
	cb, _ := CoinbasePrefix(1, m)
	b := &Block{Coinbase: append(cb, make([]byte, 8)...)}
	if _, err := s.ApplyBlock(b, 1); err != nil {
		t.Fatal(err)
	}
	if s.Get(m).Balance != 1 || s.Get(m).Mined != MinerCap {
		t.Fatalf("cap %+v", s.Get(m))
	}
	// phase 2: pool empty, budget = 90% of previous epoch burn
	s.G.PoolLeft = 0
	s.G.PrevBurn = Unit
	s.G.Supply = TotalSupply - Unit
	if r, p1 := s.Reward(AlgoCPU); p1 || r != Phase2PerBlock[AlgoCPU] {
		t.Fatal("phase2 reward", r)
	}
	s.G.EpochMint = Unit*9/10 - 5
	if r, _ := s.Reward(AlgoSHA); r != 5 {
		t.Fatal("budget clamp", r)
	}
	s.G.PrevBurn = 0
	if r, _ := s.Reward(AlgoSHA); r != 0 {
		t.Fatal("no burn should mean no mint")
	}
}

func TestPhase1Duration(t *testing.T) {
	blocks := MiningPool / Phase1Reward
	days := float64(blocks) * AlgoSpacing / NumAlgos / 86400
	if days < 360 || days > 370 {
		t.Fatalf("phase 1 lasts %.1f days", days)
	}
}

func TestLWMA(t *testing.T) {
	UseNetwork("mainnet")
	defer UseNetwork("regtest")
	var ts, bs []uint32
	for i := 0; i < 46; i++ {
		ts = append(ts, uint32(i*24)) // 10x faster than target
		bs = append(bs, 0x1d00ffff)
	}
	nb := NextBits(AlgoSHA, ts, bs)
	if CompactToBig(nb).Cmp(CompactToBig(0x1d00ffff)) >= 0 {
		t.Fatal("difficulty did not rise")
	}
}
