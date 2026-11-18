package core

import (
	"errors"
	"fmt"

	"github.com/ethereum/go-ethereum/common"
)

type Account struct {
	Balance uint64 `json:"balance"`
	Nonce   uint64 `json:"nonce"`
	Mined   uint64 `json:"mined"` // phase-1 miner rewards received (cap 100,000)
}

type Globals struct {
	PoolLeft     uint64 `json:"pool_left"`
	Supply       uint64 `json:"supply"`
	Burned       uint64 `json:"burned"`
	Phase2Minted uint64 `json:"phase2_minted"`
	EpochBurn    uint64 `json:"epoch_burn"`      // burn in current epoch
	PrevBurn     uint64 `json:"prev_epoch_burn"` // burn in previous epoch
	EpochMint    uint64 `json:"epoch_mint"`      // phase-2 mint in current epoch
	Epoch        uint64 `json:"epoch"`
}

type State struct {
	P        Params
	Accounts map[string]*Account
	G        Globals
	Code     map[string][]byte
	Storage  map[string]map[common.Hash]common.Hash
	Receipts map[Hash]*Receipt
	blk      blockCtx
	record   bool // store receipts (only while applying blocks)
}

type undo struct {
	g       Globals
	accs    map[string]*Account // nil = did not exist
	code    map[string][]byte
	codeHad map[string]bool
	stor    map[storKey]common.Hash
	storHad map[storKey]bool
}

func newUndo(g Globals) *undo {
	return &undo{g: g, accs: map[string]*Account{}, code: map[string][]byte{}, codeHad: map[string]bool{},
		stor: map[storKey]common.Hash{}, storHad: map[storKey]bool{}}
}

func (s *State) setCode(a string, code []byte, u *undo) {
	if u != nil {
		if _, seen := u.codeHad[a]; !seen {
			old, had := s.Code[a]
			u.code[a], u.codeHad[a] = old, had
		}
	}
	if code == nil {
		delete(s.Code, a)
	} else {
		s.Code[a] = code
	}
}

func (s *State) getStorage(k storKey) common.Hash { return s.Storage[k.A][k.K] }

func (s *State) setStorage(k storKey, v common.Hash, u *undo) {
	m := s.Storage[k.A]
	if u != nil {
		if _, seen := u.storHad[k]; !seen {
			old, had := m[k.K]
			u.stor[k], u.storHad[k] = old, had
		}
	}
	if v == (common.Hash{}) {
		if m != nil {
			delete(m, k.K)
			if len(m) == 0 {
				delete(s.Storage, k.A)
			}
		}
		return
	}
	if m == nil {
		m = map[common.Hash]common.Hash{}
		s.Storage[k.A] = m
	}
	m[k.K] = v
}

func NewState(p Params) *State {
	s := &State{P: p, Accounts: map[string]*Account{}, Code: map[string][]byte{}, Storage: map[string]map[common.Hash]common.Hash{}, Receipts: map[Hash]*Receipt{}}
	s.G.PoolLeft = MiningPool
	s.G.Supply = TotalSupply
	s.Accounts[p.Bridge] = &Account{Balance: BridgeGenesis}
	return s
}

func (s *State) acc(a string, u *undo) *Account {
	x, ok := s.Accounts[a]
	if u != nil {
		if _, seen := u.accs[a]; !seen {
			if ok {
				c := *x
				u.accs[a] = &c
			} else {
				u.accs[a] = nil
			}
		}
	}
	if !ok {
		x = &Account{}
		s.Accounts[a] = x
	}
	return x
}

func (s *State) Get(a string) Account {
	if x, ok := s.Accounts[a]; ok {
		return *x
	}
	return Account{}
}

// Reward is the subsidy (before the founder split) for the next block of algo.
func (s *State) Reward(algo int) (subsidy uint64, phase1 bool) {
	if s.G.PoolLeft > 0 {
		r := Phase1Reward
		if r > s.G.PoolLeft {
			r = s.G.PoolLeft
		}
		return r, true
	}
	budget := s.G.PrevBurn * BurnMintPercent / 100
	if s.G.EpochMint >= budget {
		return 0, false
	}
	r := Phase2PerBlock[algo]
	if r > budget-s.G.EpochMint {
		r = budget - s.G.EpochMint
	}
	if s.G.Supply+r > TotalSupply {
		r = TotalSupply - s.G.Supply
	}
	return r, false
}

func (s *State) ApplyTx(t *Tx, u *undo) (fee uint64, err error) {
	if err := t.CheckBasic(); err != nil {
		return 0, err
	}
	from := t.FromAddr()
	a := s.Get(from)
	if t.Nonce != a.Nonce+1 {
		return 0, fmt.Errorf("bad nonce: want %d", a.Nonce+1)
	}
	if a.Balance < t.Amount+t.Fee {
		return 0, errors.New("insufficient balance")
	}
	if t.IsContract() || len(s.Code[t.To]) > 0 {
		gas := t.Fee * GasPerUnit
		if gas > MaxTxGas {
			gas = MaxTxGas
		}
		if gas < intrinsicGas(t.Data, t.To == "") {
			return 0, fmt.Errorf("fee too low for this contract tx: need at least %d units", intrinsicGas(t.Data, t.To == "")/GasPerUnit+1)
		}
		fa := s.acc(from, u)
		fa.Balance -= t.Fee
		nonce := fa.Nonce
		if t.To != "" {
			fa.Nonce++
		}
		rec := s.runEVM(from, t.To, t.Data, t.Amount, gas, u)
		if fa = s.acc(from, u); fa.Nonce == nonce {
			fa.Nonce++ // a failed creation still consumes the nonce
		}
		if s.record {
			s.Receipts[t.ID()] = rec
		}
		return t.Fee, nil
	}
	fa := s.acc(from, u)
	fa.Balance -= t.Amount + t.Fee
	fa.Nonce++
	s.acc(t.To, u).Balance += t.Amount
	return t.Fee, nil
}

// ApplyBlock applies txs + rewards. Returns undo record. Caller checks header/PoW.
func (s *State) ApplyBlock(b *Block, height uint64) (*undo, error) {
	u := newUndo(s.G)
	ch, miner, err := ParseCoinbase(b.Coinbase)
	if err != nil {
		return nil, err
	}
	s.blk, s.record = blockCtx{Height: height, Time: uint64(b.Header.Time), Miner: miner}, true
	defer func() { s.record = false }()
	if ch != height {
		return nil, errors.New("coinbase height mismatch")
	}
	if height/EpochBlocks != s.G.Epoch {
		s.G.PrevBurn, s.G.EpochBurn, s.G.EpochMint, s.G.Epoch = s.G.EpochBurn, 0, 0, height/EpochBlocks
	}
	var fees uint64
	for i := range b.Txs {
		f, err := s.ApplyTx(&b.Txs[i], u)
		if err != nil {
			s.Undo(u)
			return nil, fmt.Errorf("tx %d: %w", i, err)
		}
		fees += f
	}
	burn := fees / 2
	if s.G.Supply <= MinSupply {
		burn = 0
	} else if s.G.Supply-burn < MinSupply {
		burn = s.G.Supply - MinSupply
	}
	s.G.Supply -= burn
	s.G.Burned += burn
	s.G.EpochBurn += burn
	s.acc(miner, u).Balance += fees - burn

	sub, phase1 := s.Reward(b.Header.Algo())
	if sub > 0 {
		founder := sub * FounderPercent / 100
		minerPart := sub - founder
		if phase1 {
			m := s.acc(miner, u)
			if m.Mined+minerPart > MinerCap {
				if m.Mined >= MinerCap {
					minerPart = 0
				} else {
					minerPart = MinerCap - m.Mined
				}
			}
			m.Mined += minerPart
			m.Balance += minerPart
			s.acc(s.P.Founder, u).Balance += founder
			s.G.PoolLeft -= minerPart + founder
		} else {
			s.acc(miner, u).Balance += minerPart
			s.acc(s.P.Founder, u).Balance += founder
			s.G.Supply += sub
			s.G.Phase2Minted += sub
			s.G.EpochMint += sub
		}
	}
	return u, nil
}

func (s *State) Undo(u *undo) {
	for a, had := range u.codeHad {
		if had {
			s.Code[a] = u.code[a]
		} else {
			delete(s.Code, a)
		}
	}
	for k, had := range u.storHad {
		if had {
			s.setStorage(k, u.stor[k], nil)
		} else {
			s.setStorage(k, common.Hash{}, nil)
		}
	}
	for a, v := range u.accs {
		if v == nil {
			delete(s.Accounts, a)
		} else {
			c := *v
			s.Accounts[a] = &c
		}
	}
	s.G = u.g
}
