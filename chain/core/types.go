package core

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"math/big"
)

type Hash [32]byte

func (h Hash) String() string { // display big-endian like Bitcoin
	r := make([]byte, 32)
	for i := range h {
		r[i] = h[31-i]
	}
	return hex.EncodeToString(r)
}

func HashFromString(s string) (Hash, error) {
	var h Hash
	b, err := hex.DecodeString(s)
	if err != nil || len(b) != 32 {
		return h, errors.New("bad hash")
	}
	for i := range b {
		h[i] = b[31-i]
	}
	return h, nil
}

func (h Hash) MarshalText() ([]byte, error) { return []byte(h.String()), nil }
func (h *Hash) UnmarshalText(b []byte) error {
	x, err := HashFromString(string(b))
	*h = x
	return err
}

func Sha256d(b []byte) Hash {
	a := sha256.Sum256(b)
	return sha256.Sum256(a[:])
}

// HashToBig reads the hash little-endian (Bitcoin convention).
func HashToBig(h Hash) *big.Int {
	r := make([]byte, 32)
	for i := range h {
		r[i] = h[31-i]
	}
	return new(big.Int).SetBytes(r)
}

type Header struct {
	Version uint32 `json:"version"`
	Prev    Hash   `json:"prev"`
	Merkle  Hash   `json:"merkle"`
	Time    uint32 `json:"time"`
	Bits    uint32 `json:"bits"`
	Nonce   uint32 `json:"nonce"`
}

func (h *Header) Algo() int { return int(h.Version & 0xff) }

func (h *Header) Bytes() []byte {
	b := make([]byte, 80)
	binary.LittleEndian.PutUint32(b[0:], h.Version)
	copy(b[4:], h.Prev[:])
	copy(b[36:], h.Merkle[:])
	binary.LittleEndian.PutUint32(b[68:], h.Time)
	binary.LittleEndian.PutUint32(b[72:], h.Bits)
	binary.LittleEndian.PutUint32(b[76:], h.Nonce)
	return b
}

func (h *Header) Hash() Hash { return Sha256d(h.Bytes()) }

type Tx struct {
	From   []byte `json:"from"` // ed25519 public key
	To     string `json:"to"`
	Amount uint64 `json:"amount"`
	Fee    uint64 `json:"fee"`
	Nonce  uint64 `json:"nonce"`
	Memo   string `json:"memo,omitempty"`
	Sig    []byte `json:"sig"`
}

func (t *Tx) SigningBytes() []byte {
	var b bytes.Buffer
	b.WriteString("ALTF-TX-v1")
	b.Write(t.From)
	b.WriteByte(byte(len(t.To)))
	b.WriteString(t.To)
	var n [8]byte
	for _, v := range []uint64{t.Amount, t.Fee, t.Nonce} {
		binary.LittleEndian.PutUint64(n[:], v)
		b.Write(n[:])
	}
	b.WriteByte(byte(len(t.Memo)))
	b.WriteString(t.Memo)
	return b.Bytes()
}

func (t *Tx) ID() Hash { return Sha256d(append(t.SigningBytes(), t.Sig...)) }

func (t *Tx) FromAddr() string { return AddressFromPub(t.From) }

func (t *Tx) Sign(priv ed25519.PrivateKey) {
	t.From = priv.Public().(ed25519.PublicKey)
	t.Sig = ed25519.Sign(priv, t.SigningBytes())
}

func (t *Tx) CheckBasic() error {
	if len(t.From) != ed25519.PublicKeySize || len(t.Sig) != ed25519.SignatureSize {
		return errors.New("bad key/sig size")
	}
	if !ValidAddress(t.To) {
		return errors.New("bad recipient address")
	}
	if t.Amount == 0 {
		return errors.New("zero amount")
	}
	if t.Fee < MinFee {
		return errors.New("fee below minimum 0.01 ALTF")
	}
	if len(t.Memo) > 120 {
		return errors.New("memo too long")
	}
	if t.Amount > TotalSupply || t.Fee > TotalSupply {
		return errors.New("amount too large")
	}
	if !ed25519.Verify(t.From, t.SigningBytes(), t.Sig) {
		return errors.New("bad signature")
	}
	return nil
}

type Block struct {
	Header   Header `json:"header"`
	Coinbase []byte `json:"coinbase"`
	Txs      []Tx   `json:"txs"`
}

// Coinbase: magic(4) | height u64LE(8) | miner addr raw(21) | extranonce(8) | tag
const CoinbasePrefixLen = 4 + 8 + 21

func CoinbasePrefix(height uint64, miner string) ([]byte, error) {
	raw, err := DecodeAddress(miner)
	if err != nil {
		return nil, err
	}
	b := make([]byte, 0, CoinbasePrefixLen)
	b = append(b, CoinbaseMagic...)
	b = binary.LittleEndian.AppendUint64(b, height)
	b = append(b, raw...)
	return b, nil
}

func ParseCoinbase(cb []byte) (height uint64, miner string, err error) {
	if len(cb) < CoinbasePrefixLen+8 || len(cb) > 100 || string(cb[:4]) != CoinbaseMagic {
		return 0, "", errors.New("bad coinbase")
	}
	height = binary.LittleEndian.Uint64(cb[4:12])
	miner = EncodeAddress(cb[12:33])
	if !ValidAddress(miner) {
		return 0, "", errors.New("bad coinbase miner address")
	}
	return
}

func MerkleRoot(leaves []Hash) Hash {
	if len(leaves) == 0 {
		return Hash{}
	}
	l := append([]Hash(nil), leaves...)
	for len(l) > 1 {
		if len(l)%2 == 1 {
			l = append(l, l[len(l)-1])
		}
		n := make([]Hash, len(l)/2)
		for i := range n {
			n[i] = Sha256d(append(l[2*i][:], l[2*i+1][:]...))
		}
		l = n
	}
	return l[0]
}

// MerkleBranch returns the stratum merkle branch for leaf 0 given the tx ids.
func MerkleBranch(txids []Hash) []Hash {
	var br []Hash
	l := append([]Hash{{}}, txids...)
	for len(l) > 1 {
		br = append(br, l[1])
		if len(l)%2 == 1 {
			l = append(l, l[len(l)-1])
		}
		n := make([]Hash, len(l)/2)
		for i := 1; i < len(n); i++ {
			n[i] = Sha256d(append(l[2*i][:], l[2*i+1][:]...))
		}
		l = n
	}
	return br
}

func (b *Block) ComputeMerkle() Hash {
	leaves := []Hash{Sha256d(b.Coinbase)}
	for i := range b.Txs {
		leaves = append(leaves, b.Txs[i].ID())
	}
	return MerkleRoot(leaves)
}
