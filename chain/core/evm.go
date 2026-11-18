package core

// Smart contracts: the Ethereum Virtual Machine (go-ethereum core/vm) running on ALTAF state.
// 1 EVM wei = 1 ALTF unit (6 decimals). EVM address = the 20-byte hash inside an ALTAF address.
// Gas is bought with the tx fee: gas limit = fee * GasPerUnit; the whole fee is charged.

import (
	"encoding/hex"
	"errors"
	"math/big"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/stateless"
	"github.com/ethereum/go-ethereum/core/tracing"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/ethereum/go-ethereum/core/vm"
	"github.com/ethereum/go-ethereum/crypto"
	"github.com/ethereum/go-ethereum/params"
	"github.com/ethereum/go-ethereum/trie/utils"
	"github.com/holiman/uint256"
)

const (
	ChainID      = 41_343 // EIP-155 style id of ALTAF mainnet contracts
	GasPerUnit   = 100    // gas bought per 0.000001 ALTF of fee
	MaxTxGas     = 30_000_000
	MaxCodeInput = 49_152
)

type HexBytes []byte

func (h HexBytes) MarshalText() ([]byte, error) { return []byte("0x" + hex.EncodeToString(h)), nil }
func (h *HexBytes) UnmarshalText(b []byte) error {
	s := string(b)
	if len(s) >= 2 && (s[:2] == "0x" || s[:2] == "0X") {
		s = s[2:]
	}
	d, err := hex.DecodeString(s)
	*h = d
	return err
}

type Log struct {
	Address string     `json:"address"`
	Topics  []HexBytes `json:"topics"`
	Data    HexBytes   `json:"data"`
}

type Receipt struct {
	Status   uint8    `json:"status"` // 1 success, 0 failed (fee still paid)
	GasUsed  uint64   `json:"gas_used"`
	Contract string   `json:"contract,omitempty"`
	Return   HexBytes `json:"return,omitempty"`
	Error    string   `json:"error,omitempty"`
	Logs     []Log    `json:"logs"`
}

type blockCtx struct {
	Height uint64
	Time   uint64
	Miner  string
}

type storKey struct {
	A string
	K common.Hash
}

func EvmAddress(a string) (common.Address, error) {
	raw, err := DecodeAddress(a)
	if err != nil {
		return common.Address{}, err
	}
	return common.BytesToAddress(raw[1:]), nil
}

func FromEvm(a common.Address) string { return EncodeAddress(append([]byte{AddrVersion}, a.Bytes()...)) }

func mustEvm(a string) common.Address { e, _ := EvmAddress(a); return e }

var chainConfig = func() *params.ChainConfig {
	z, t := big.NewInt(0), uint64(0)
	return &params.ChainConfig{ChainID: big.NewInt(ChainID), HomesteadBlock: z, EIP150Block: z, EIP155Block: z, EIP158Block: z,
		ByzantiumBlock: z, ConstantinopleBlock: z, PetersburgBlock: z, IstanbulBlock: z, MuirGlacierBlock: z, BerlinBlock: z,
		LondonBlock: z, ArrowGlacierBlock: z, GrayGlacierBlock: z, MergeNetsplitBlock: z, TerminalTotalDifficulty: z,
		ShanghaiTime: &t, CancunTime: &t}
}()

func intrinsicGas(data []byte, create bool) uint64 {
	g := uint64(21_000)
	if create {
		g += 32_000 + 2*uint64((len(data)+31)/32)
	}
	for _, b := range data {
		if b == 0 {
			g += 4
		} else {
			g += 16
		}
	}
	return g
}

// ---------- vm.StateDB adapter ----------

type evmState struct {
	s          *State
	u          *undo
	journal    []func()
	refund     uint64
	logs       []*types.Log
	transient  map[storKey]common.Hash
	orig       map[storKey]common.Hash
	accAddr    map[common.Address]bool
	accSlot    map[storKey]bool
	created    map[common.Address]bool
	destructed map[common.Address]bool
}

func newEvmState(s *State, u *undo) *evmState {
	return &evmState{s: s, u: u, transient: map[storKey]common.Hash{}, orig: map[storKey]common.Hash{},
		accAddr: map[common.Address]bool{}, accSlot: map[storKey]bool{}, created: map[common.Address]bool{}, destructed: map[common.Address]bool{}}
}

func (e *evmState) acc(a common.Address) *Account {
	x := e.s.acc(FromEvm(a), e.u)
	prev := *x
	e.journal = append(e.journal, func() { *e.s.acc(FromEvm(a), e.u) = prev })
	return x
}

func u256(v uint64) *uint256.Int { return uint256.NewInt(v) }

func (e *evmState) CreateAccount(a common.Address)  { e.acc(a) }
func (e *evmState) CreateContract(a common.Address) {
	e.created[a] = true
	e.journal = append(e.journal, func() { delete(e.created, a) })
}
func (e *evmState) SubBalance(a common.Address, v *uint256.Int, _ tracing.BalanceChangeReason) uint256.Int {
	x := e.acc(a)
	p := *u256(x.Balance)
	x.Balance -= v.Uint64()
	return p
}
func (e *evmState) AddBalance(a common.Address, v *uint256.Int, _ tracing.BalanceChangeReason) uint256.Int {
	x := e.acc(a)
	p := *u256(x.Balance)
	x.Balance += v.Uint64()
	return p
}
func (e *evmState) GetBalance(a common.Address) *uint256.Int { return u256(e.s.Get(FromEvm(a)).Balance) }
func (e *evmState) GetNonce(a common.Address) uint64         { return e.s.Get(FromEvm(a)).Nonce }
func (e *evmState) SetNonce(a common.Address, n uint64)      { e.acc(a).Nonce = n }
func (e *evmState) GetCode(a common.Address) []byte          { return e.s.Code[FromEvm(a)] }
func (e *evmState) GetCodeSize(a common.Address) int         { return len(e.s.Code[FromEvm(a)]) }
func (e *evmState) GetCodeHash(a common.Address) common.Hash {
	if !e.Exist(a) {
		return common.Hash{}
	}
	c := e.s.Code[FromEvm(a)]
	if len(c) == 0 {
		return types.EmptyCodeHash
	}
	return crypto.Keccak256Hash(c)
}
func (e *evmState) SetCode(a common.Address, code []byte) {
	k := FromEvm(a)
	prev, had := e.s.Code[k]
	e.journal = append(e.journal, func() {
		if had {
			e.s.setCode(k, prev, e.u)
		} else {
			e.s.setCode(k, nil, e.u)
		}
	})
	e.s.setCode(k, code, e.u)
}
func (e *evmState) AddRefund(g uint64) {
	p := e.refund
	e.journal = append(e.journal, func() { e.refund = p })
	e.refund += g
}
func (e *evmState) SubRefund(g uint64) {
	p := e.refund
	e.journal = append(e.journal, func() { e.refund = p })
	if g > e.refund {
		g = e.refund
	}
	e.refund -= g
}
func (e *evmState) GetRefund() uint64 { return e.refund }
func (e *evmState) GetCommittedState(a common.Address, k common.Hash) common.Hash {
	sk := storKey{FromEvm(a), k}
	if v, ok := e.orig[sk]; ok {
		return v
	}
	return e.s.getStorage(sk)
}
func (e *evmState) GetState(a common.Address, k common.Hash) common.Hash {
	return e.s.getStorage(storKey{FromEvm(a), k})
}
func (e *evmState) SetState(a common.Address, k, v common.Hash) common.Hash {
	sk := storKey{FromEvm(a), k}
	prev := e.s.getStorage(sk)
	if _, ok := e.orig[sk]; !ok {
		e.orig[sk] = prev
	}
	e.journal = append(e.journal, func() { e.s.setStorage(sk, prev, e.u) })
	e.s.setStorage(sk, v, e.u)
	return prev
}
func (e *evmState) GetStorageRoot(a common.Address) common.Hash {
	if len(e.s.Storage[FromEvm(a)]) == 0 {
		return common.Hash{}
	}
	return common.Hash{1}
}
func (e *evmState) GetTransientState(a common.Address, k common.Hash) common.Hash {
	return e.transient[storKey{FromEvm(a), k}]
}
func (e *evmState) SetTransientState(a common.Address, k, v common.Hash) {
	sk := storKey{FromEvm(a), k}
	p := e.transient[sk]
	e.journal = append(e.journal, func() { e.transient[sk] = p })
	e.transient[sk] = v
}
func (e *evmState) SelfDestruct(a common.Address) uint256.Int {
	x := e.acc(a)
	p := *u256(x.Balance)
	was := e.destructed[a]
	e.journal = append(e.journal, func() {
		if !was {
			delete(e.destructed, a)
		}
	})
	e.destructed[a] = true
	x.Balance = 0
	return p
}
func (e *evmState) HasSelfDestructed(a common.Address) bool { return e.destructed[a] }
func (e *evmState) SelfDestruct6780(a common.Address) (uint256.Int, bool) {
	if e.created[a] {
		return e.SelfDestruct(a), true
	}
	return *e.GetBalance(a), false
}
func (e *evmState) Exist(a common.Address) bool {
	k := FromEvm(a)
	_, ok := e.s.Accounts[k]
	return ok || len(e.s.Code[k]) > 0 || e.destructed[a]
}
func (e *evmState) Empty(a common.Address) bool {
	x := e.s.Get(FromEvm(a))
	return x.Balance == 0 && x.Nonce == 0 && len(e.s.Code[FromEvm(a)]) == 0
}
func (e *evmState) AddressInAccessList(a common.Address) bool { return e.accAddr[a] }
func (e *evmState) SlotInAccessList(a common.Address, k common.Hash) (bool, bool) {
	return e.accAddr[a], e.accSlot[storKey{FromEvm(a), k}]
}
func (e *evmState) AddAddressToAccessList(a common.Address) {
	if !e.accAddr[a] {
		e.accAddr[a] = true
		e.journal = append(e.journal, func() { delete(e.accAddr, a) })
	}
}
func (e *evmState) AddSlotToAccessList(a common.Address, k common.Hash) {
	e.AddAddressToAccessList(a)
	sk := storKey{FromEvm(a), k}
	if !e.accSlot[sk] {
		e.accSlot[sk] = true
		e.journal = append(e.journal, func() { delete(e.accSlot, sk) })
	}
}
func (e *evmState) PointCache() *utils.PointCache { return nil }
func (e *evmState) Prepare(rules params.Rules, sender, coinbase common.Address, dest *common.Address, precompiles []common.Address, _ types.AccessList) {
	e.AddAddressToAccessList(sender)
	if dest != nil {
		e.AddAddressToAccessList(*dest)
	}
	for _, p := range precompiles {
		e.AddAddressToAccessList(p)
	}
	if rules.IsShanghai {
		e.AddAddressToAccessList(coinbase)
	}
}
func (e *evmState) Snapshot() int { return len(e.journal) }
func (e *evmState) RevertToSnapshot(id int) {
	for i := len(e.journal) - 1; i >= id; i-- {
		e.journal[i]()
	}
	e.journal = e.journal[:id]
}
func (e *evmState) AddLog(l *types.Log) {
	n := len(e.logs)
	e.journal = append(e.journal, func() { e.logs = e.logs[:n] })
	e.logs = append(e.logs, l)
}
func (e *evmState) AddPreimage(common.Hash, []byte)  {}
func (e *evmState) Witness() *stateless.Witness       { return nil }
func (e *evmState) Finalise(bool) {
	for a := range e.destructed {
		k := FromEvm(a)
		e.s.setCode(k, nil, e.u)
		for slot := range e.s.Storage[k] {
			e.s.setStorage(storKey{k, slot}, common.Hash{}, e.u)
		}
		e.s.acc(k, e.u).Balance = 0
	}
}

func (s *State) newEVM(st *evmState, origin common.Address) *vm.EVM {
	bc := vm.BlockContext{
		CanTransfer: func(db vm.StateDB, a common.Address, v *uint256.Int) bool { return db.GetBalance(a).Cmp(v) >= 0 },
		Transfer: func(db vm.StateDB, from, to common.Address, v *uint256.Int) {
			db.SubBalance(from, v, tracing.BalanceChangeTransfer)
			db.AddBalance(to, v, tracing.BalanceChangeTransfer)
		},
		GetHash:     func(uint64) common.Hash { return common.Hash{} },
		Coinbase:    mustEvm(s.blk.Miner),
		GasLimit:    MaxTxGas,
		BlockNumber: new(big.Int).SetUint64(s.blk.Height),
		Time:        s.blk.Time,
		Difficulty:  big.NewInt(0),
		BaseFee:     big.NewInt(0),
		BlobBaseFee: big.NewInt(0),
		Random:      &common.Hash{},
	}
	return vm.NewEVM(bc, vm.TxContext{Origin: origin, GasPrice: big.NewInt(0)}, st, chainConfig, vm.Config{NoBaseFee: true})
}

// runEVM executes a contract creation (to == "") or call. The caller has already checked funds and charged the fee.
func (s *State) runEVM(from, to string, data []byte, value, gas uint64, u *undo) *Receipt {
	st := newEvmState(s, u)
	sender := mustEvm(from)
	evm := s.newEVM(st, sender)
	rules := chainConfig.Rules(evm.Context.BlockNumber, true, evm.Context.Time)
	var dest *common.Address
	if to != "" {
		d := mustEvm(to)
		dest = &d
	}
	st.Prepare(rules, sender, evm.Context.Coinbase, dest, vm.ActivePrecompiles(rules), nil)
	rec := &Receipt{Logs: []Log{}}
	ig := intrinsicGas(data, to == "")
	var ret []byte
	var left uint64
	var err error
	if gas < ig {
		err = errors.New("fee too low: intrinsic gas not covered")
	} else if to == "" {
		var ca common.Address
		ret, ca, left, err = evm.Create(vm.AccountRef(sender), data, gas-ig, u256(value))
		if err == nil {
			rec.Contract = FromEvm(ca)
		}
	} else {
		ret, left, err = evm.Call(vm.AccountRef(sender), *dest, data, gas-ig, u256(value))
	}
	st.Finalise(true)
	rec.GasUsed = gas - left
	rec.Return = ret
	if err != nil {
		rec.Error = err.Error()
	} else {
		rec.Status = 1
		for _, l := range st.logs {
			lg := Log{Address: FromEvm(l.Address), Data: l.Data, Topics: []HexBytes{}}
			for _, t := range l.Topics {
				lg.Topics = append(lg.Topics, HexBytes(t.Bytes()))
			}
			rec.Logs = append(rec.Logs, lg)
		}
	}
	return rec
}
