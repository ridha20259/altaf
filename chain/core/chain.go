package core

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	bolt "go.etcd.io/bbolt"
)

type meta struct {
	Height uint64
	Parent Hash
	Time   uint32
	Bits   uint32
	Algo   int
}

type Chain struct {
	mu      sync.RWMutex
	P       Params
	db      *bolt.DB
	index   map[Hash]*meta
	main    []Hash
	state   *State
	undos   map[Hash]*undo
	Mempool map[Hash]Tx
	OnBlock func(*Block)
	tix     *txIndex
}

var bBlocks, bMain = []byte("blocks"), []byte("main")

func GenesisBlock(p Params) *Block {
	cb, _ := CoinbasePrefix(0, p.Founder)
	cb = append(cb, make([]byte, 8)...)
	cb = append(cb, []byte("ALTAF genesis "+p.Network)...)
	b := &Block{Coinbase: cb}
	b.Header = Header{Version: AlgoCPU, Time: p.GenesisTime, Bits: GenesisBits[AlgoCPU]}
	b.Header.Merkle = b.ComputeMerkle()
	return b
}

func OpenChain(path string, p Params) (*Chain, error) {
	db, err := bolt.Open(path, 0600, &bolt.Options{Timeout: 2 * time.Second})
	if err != nil {
		return nil, err
	}
	c := &Chain{P: p, db: db, index: map[Hash]*meta{}, undos: map[Hash]*undo{}, Mempool: map[Hash]Tx{}, state: NewState(p)}
	g := GenesisBlock(p)
	gh := g.Header.Hash()
	err = db.Update(func(tx *bolt.Tx) error {
		bb, _ := tx.CreateBucketIfNotExists(bBlocks)
		bm, _ := tx.CreateBucketIfNotExists(bMain)
		if bb.Get(gh[:]) == nil {
			j, _ := json.Marshal(g)
			bb.Put(gh[:], j)
			bm.Put(hkey(0), gh[:])
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	// load all blocks into index, replay main chain
	var mainHashes []Hash
	db.View(func(tx *bolt.Tx) error {
		bm := tx.Bucket(bMain)
		bm.ForEach(func(k, v []byte) error { var h Hash; copy(h[:], v); mainHashes = append(mainHashes, h); return nil })
		return nil
	})
	if len(mainHashes) == 0 || mainHashes[0] != gh {
		return nil, errors.New("genesis mismatch: wrong network params or data dir")
	}
	c.index[gh] = &meta{Height: 0, Time: g.Header.Time, Bits: g.Header.Bits, Algo: AlgoCPU}
	c.main = []Hash{gh}
	for _, h := range mainHashes[1:] {
		b, err := c.getBlock(h)
		if err != nil {
			return nil, err
		}
		if err := c.connect(b, false); err != nil {
			return nil, fmt.Errorf("replay %s: %w", h, err)
		}
	}
	// index side blocks (best effort)
	db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(bBlocks).ForEach(func(k, v []byte) error {
			var h Hash
			copy(h[:], k)
			if _, ok := c.index[h]; ok {
				return nil
			}
			var b Block
			if json.Unmarshal(v, &b) == nil {
				if pm, ok := c.index[b.Header.Prev]; ok {
					c.index[h] = &meta{Height: pm.Height + 1, Parent: b.Header.Prev, Time: b.Header.Time, Bits: b.Header.Bits, Algo: b.Header.Algo()}
				}
			}
			return nil
		})
	})
	return c, nil
}

func hkey(h uint64) []byte { return binary.BigEndian.AppendUint64(nil, h) }

func (c *Chain) getBlock(h Hash) (*Block, error) {
	var b Block
	err := c.db.View(func(tx *bolt.Tx) error {
		v := tx.Bucket(bBlocks).Get(h[:])
		if v == nil {
			return errors.New("block not found")
		}
		return json.Unmarshal(v, &b)
	})
	return &b, err
}

func (c *Chain) Tip() (Hash, uint64) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.main[len(c.main)-1], uint64(len(c.main) - 1)
}

func (c *Chain) BlockByHeight(h uint64) (*Block, error) {
	c.mu.RLock()
	if h >= uint64(len(c.main)) {
		c.mu.RUnlock()
		return nil, errors.New("no such height")
	}
	x := c.main[h]
	c.mu.RUnlock()
	return c.getBlock(x)
}

func (c *Chain) BlockByHash(h Hash) (*Block, error) { return c.getBlock(h) }

func (c *Chain) Account(a string) Account {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.state.Get(a)
}

func (c *Chain) Globals() Globals {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.state.G
}

func (c *Chain) Height() uint64 { _, h := c.Tip(); return h }

// expectedBits/medianTime for a child of parent.
func (c *Chain) expectedBits(parent Hash, algo int) uint32 {
	var times, bits []uint32
	h, steps := parent, 0
	for steps < 5000 && len(times) <= LWMAWindow {
		m, ok := c.index[h]
		if !ok {
			break
		}
		if m.Algo == algo && m.Height > 0 {
			times = append(times, m.Time)
			bits = append(bits, m.Bits)
		}
		if m.Height == 0 {
			break
		}
		h = m.Parent
		steps++
	}
	for i, j := 0, len(times)-1; i < j; i, j = i+1, j-1 {
		times[i], times[j] = times[j], times[i]
		bits[i], bits[j] = bits[j], bits[i]
	}
	return NextBits(algo, times, bits)
}

func (c *Chain) medianTime(parent Hash) uint32 {
	var ts []uint32
	h := parent
	for i := 0; i < 11; i++ {
		m, ok := c.index[h]
		if !ok {
			break
		}
		ts = append(ts, m.Time)
		if m.Height == 0 {
			break
		}
		h = m.Parent
	}
	sort.Slice(ts, func(i, j int) bool { return ts[i] < ts[j] })
	return ts[len(ts)/2]
}

func (c *Chain) checkHeader(b *Block, pm *meta) error {
	hd := &b.Header
	if hd.Algo() >= NumAlgos {
		return errors.New("unknown algo")
	}
	if hd.Time <= c.medianTime(hd.Prev) {
		return errors.New("time too old")
	}
	if int64(hd.Time) > time.Now().Unix()+MaxFutureTime {
		return errors.New("time too far in future")
	}
	if hd.Bits != c.expectedBits(hd.Prev, hd.Algo()) {
		return errors.New("wrong difficulty")
	}
	if !CheckPow(hd) {
		return errors.New("insufficient proof of work")
	}
	if len(b.Txs) > MaxBlockTxs {
		return errors.New("too many txs")
	}
	if b.ComputeMerkle() != hd.Merkle {
		return errors.New("bad merkle root")
	}
	return nil
}

// connect applies b on top of the current tip (b.Prev must be tip).
func (c *Chain) connect(b *Block, persist bool) error {
	h := b.Header.Hash()
	height := uint64(len(c.main))
	u, err := c.state.ApplyBlock(b, height)
	if err != nil {
		return err
	}
	c.undos[h] = u
	if height > MaxReorgDepth+5 {
		delete(c.undos, c.main[height-MaxReorgDepth-5])
	}
	c.index[h] = &meta{Height: height, Parent: b.Header.Prev, Time: b.Header.Time, Bits: b.Header.Bits, Algo: b.Header.Algo()}
	c.main = append(c.main, h)
	c.indexBlock(b, h, height)
	for i := range b.Txs {
		delete(c.Mempool, b.Txs[i].ID())
	}
	if persist {
		return c.db.Update(func(tx *bolt.Tx) error { return tx.Bucket(bMain).Put(hkey(height), h[:]) })
	}
	return nil
}

func (c *Chain) disconnectTip() error {
	h := c.main[len(c.main)-1]
	u, ok := c.undos[h]
	if !ok {
		return errors.New("no undo data")
	}
	b, err := c.getBlock(h)
	if err != nil {
		return err
	}
	c.state.Undo(u)
	delete(c.undos, h)
	height := uint64(len(c.main) - 1)
	c.unindexBlock(b, height)
	c.main = c.main[:height]
	for _, t := range b.Txs {
		c.Mempool[t.ID()] = t
	}
	return c.db.Update(func(tx *bolt.Tx) error { return tx.Bucket(bMain).Delete(hkey(height)) })
}

var ErrKnown = errors.New("already known")
var ErrOrphan = errors.New("unknown parent")

func (c *Chain) AddBlock(b *Block) error {
	c.mu.Lock()
	h := b.Header.Hash()
	if _, ok := c.index[h]; ok {
		c.mu.Unlock()
		return ErrKnown
	}
	pm, ok := c.index[b.Header.Prev]
	if !ok {
		c.mu.Unlock()
		return ErrOrphan
	}
	if err := c.checkHeader(b, pm); err != nil {
		c.mu.Unlock()
		return err
	}
	height := pm.Height + 1
	if ch, _, err := ParseCoinbase(b.Coinbase); err != nil || ch != height {
		c.mu.Unlock()
		return errors.New("bad coinbase")
	}
	tipH := uint64(len(c.main) - 1)
	if tipH > MaxReorgDepth && height+MaxReorgDepth <= tipH {
		c.mu.Unlock()
		return errors.New("too deep fork")
	}
	j, _ := json.Marshal(b)
	if err := c.db.Update(func(tx *bolt.Tx) error { return tx.Bucket(bBlocks).Put(h[:], j) }); err != nil {
		c.mu.Unlock()
		return err
	}
	tip := c.main[tipH]
	var err error
	if b.Header.Prev == tip {
		err = c.connect(b, true)
		if err != nil {
			c.db.Update(func(tx *bolt.Tx) error { return tx.Bucket(bBlocks).Delete(h[:]) })
		}
	} else {
		c.index[h] = &meta{Height: height, Parent: b.Header.Prev, Time: b.Header.Time, Bits: b.Header.Bits, Algo: b.Header.Algo()}
		if height > tipH {
			err = c.reorg(h)
		}
	}
	cb := c.OnBlock
	c.mu.Unlock()
	if err == nil && cb != nil {
		cb(b)
	}
	return err
}

func (c *Chain) reorg(newTip Hash) error {
	var branch []Hash
	h := newTip
	for {
		m := c.index[h]
		if m.Height < uint64(len(c.main)) && c.main[m.Height] == h {
			break
		}
		branch = append(branch, h)
		h = m.Parent
	}
	fork := c.index[h].Height
	var old []Hash
	for uint64(len(c.main)-1) > fork {
		old = append(old, c.main[len(c.main)-1])
		if err := c.disconnectTip(); err != nil {
			return err
		}
	}
	for i := len(branch) - 1; i >= 0; i-- {
		b, err := c.getBlock(branch[i])
		if err == nil {
			err = c.connect(b, true)
		}
		if err != nil {
			delete(c.index, branch[i])
			for uint64(len(c.main)-1) > fork {
				c.disconnectTip()
			}
			for j := len(old) - 1; j >= 0; j-- {
				ob, _ := c.getBlock(old[j])
				c.connect(ob, true)
			}
			return fmt.Errorf("reorg failed: %w", err)
		}
	}
	return nil
}

func (c *Chain) AddTx(t Tx) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	id := t.ID()
	if _, ok := c.Mempool[id]; ok {
		return ErrKnown
	}
	if len(c.Mempool) > 20000 {
		return errors.New("mempool full")
	}
	// validate against state + pending txs of the same sender
	u := newUndo(c.state.G)
	defer c.state.Undo(u)
	c.state.blk = blockCtx{Height: uint64(len(c.main)), Time: uint64(time.Now().Unix()), Miner: c.P.Founder}
	for _, p := range c.sortedMempool() {
		c.state.ApplyTx(&p, u)
	}
	if _, err := c.state.ApplyTx(&t, u); err != nil {
		return err
	}
	c.Mempool[id] = t
	return nil
}

func (c *Chain) sortedMempool() []Tx {
	l := make([]Tx, 0, len(c.Mempool))
	for _, t := range c.Mempool {
		l = append(l, t)
	}
	sort.Slice(l, func(i, j int) bool {
		if l[i].Nonce != l[j].Nonce {
			return l[i].Nonce < l[j].Nonce
		}
		return l[i].Fee > l[j].Fee
	})
	return l
}

type Template struct {
	Height   uint64   `json:"height"`
	Prev     Hash     `json:"prev"`
	Bits     uint32   `json:"bits"`
	Time     uint32   `json:"time"`
	MinTime  uint32   `json:"min_time"`
	Version  uint32   `json:"version"`
	Prefix   []byte   `json:"coinbase_prefix"`
	Txs      []Tx     `json:"txs"`
	TxIDs    []Hash   `json:"txids"`
	Reward   uint64   `json:"reward"`
	Fees     uint64   `json:"fees"`
}

func (c *Chain) Template(algo int, miner string) (*Template, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if algo < 0 || algo >= NumAlgos {
		return nil, errors.New("bad algo")
	}
	tip := c.main[len(c.main)-1]
	height := uint64(len(c.main))
	pre, err := CoinbasePrefix(height, miner)
	if err != nil {
		return nil, err
	}
	t := &Template{Height: height, Prev: tip, Bits: c.expectedBits(tip, algo), Version: uint32(algo), Prefix: pre}
	t.MinTime = c.medianTime(tip) + 1
	t.Time = uint32(time.Now().Unix())
	if t.Time < t.MinTime {
		t.Time = t.MinTime
	}
	u := newUndo(c.state.G)
	for _, x := range c.sortedMempool() {
		if len(t.Txs) >= MaxBlockTxs {
			break
		}
		if f, err := c.state.ApplyTx(&x, u); err == nil {
			t.Txs = append(t.Txs, x)
			t.TxIDs = append(t.TxIDs, x.ID())
			t.Fees += f
		}
	}
	c.state.Undo(u)
	t.Reward, _ = c.state.Reward(algo)
	return t, nil
}

func (c *Chain) Close() error { return c.db.Close() }

func (c *Chain) Code(a string) []byte {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.state.Code[a]
}

func (c *Chain) Receipt(id Hash) (*Receipt, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	r, ok := c.state.Receipts[id]
	return r, ok
}

// CallContract runs a read-only call on the current state and throws the changes away.
func (c *Chain) CallContract(from, to string, data []byte, value uint64) *Receipt {
	c.mu.Lock()
	defer c.mu.Unlock()
	if from == "" {
		from = c.P.Bridge
	}
	u := newUndo(c.state.G)
	defer c.state.Undo(u)
	c.state.blk = blockCtx{Height: uint64(len(c.main)), Time: uint64(time.Now().Unix()), Miner: c.P.Founder}
	c.state.acc(from, u).Balance += value
	return c.state.runEVM(from, to, data, value, MaxTxGas, u)
}
