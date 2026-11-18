package core

type TxLoc struct {
	TxID   Hash   `json:"txid"`
	Height uint64 `json:"height"`
	Block  Hash   `json:"block"`
	Index  int    `json:"index"`
	Tx     *Tx    `json:"tx,omitempty"`
	Kind   string `json:"kind"` // send | receive | mined
	Amount uint64 `json:"amount"`
}

type txIndex struct {
	byID   map[Hash]TxLoc
	byAddr map[string][]TxLoc
}

func (c *Chain) idx() *txIndex {
	if c.tix == nil {
		c.tix = &txIndex{byID: map[Hash]TxLoc{}, byAddr: map[string][]TxLoc{}}
	}
	return c.tix
}

func (c *Chain) indexBlock(b *Block, h Hash, height uint64) {
	x := c.idx()
	if _, miner, err := ParseCoinbase(b.Coinbase); err == nil {
		x.byAddr[miner] = append(x.byAddr[miner], TxLoc{Height: height, Block: h, Index: -1, Kind: "mined"})
	}
	for i := range b.Txs {
		t := &b.Txs[i]
		id := t.ID()
		l := TxLoc{TxID: id, Height: height, Block: h, Index: i, Amount: t.Amount}
		x.byID[id] = l
		s, r := l, l
		s.Kind, r.Kind = "send", "receive"
		from := t.FromAddr()
		x.byAddr[from] = append(x.byAddr[from], s)
		x.byAddr[t.To] = append(x.byAddr[t.To], r)
	}
}

func (c *Chain) unindexBlock(b *Block, height uint64) {
	x := c.idx()
	drop := func(a string) {
		l := x.byAddr[a]
		for len(l) > 0 && l[len(l)-1].Height == height {
			l = l[:len(l)-1]
		}
		x.byAddr[a] = l
	}
	if _, miner, err := ParseCoinbase(b.Coinbase); err == nil {
		drop(miner)
	}
	for i := range b.Txs {
		delete(x.byID, b.Txs[i].ID())
		drop(b.Txs[i].FromAddr())
		drop(b.Txs[i].To)
	}
}

func (c *Chain) TxLocation(id Hash) (TxLoc, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	l, ok := c.idx().byID[id]
	if ok {
		if b, err := c.getBlock(l.Block); err == nil && l.Index < len(b.Txs) {
			l.Tx = &b.Txs[l.Index]
		}
		l.Kind = "tx"
	} else if t, inPool := c.Mempool[id]; inPool {
		return TxLoc{TxID: id, Tx: &t, Kind: "pending", Index: -1}, true
	}
	return l, ok
}

func (c *Chain) History(a string) []TxLoc {
	c.mu.RLock()
	defer c.mu.RUnlock()
	l := c.idx().byAddr[a]
	out := make([]TxLoc, 0, 100)
	for i := len(l) - 1; i >= 0 && len(out) < 100; i-- {
		out = append(out, l[i])
	}
	return out
}

func (c *Chain) MempoolSize() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.Mempool)
}

func (c *Chain) MempoolTxs() []Tx {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.sortedMempool()
}
