package core

import (
	"math/big"

	"golang.org/x/crypto/argon2"
)

var cpuSalt = []byte("ALTAF-CPU-POW-v1")

func PowHash(h *Header) Hash {
	if h.Algo() == AlgoCPU {
		var out Hash
		copy(out[:], argon2.IDKey(h.Bytes(), cpuSalt, 1, 32*1024, 1, 32))
		return out
	}
	return h.Hash()
}

func CompactToBig(c uint32) *big.Int {
	mant := int64(c & 0x007fffff)
	exp := uint(c >> 24)
	n := big.NewInt(mant)
	if exp <= 3 {
		return n.Rsh(n, 8*(3-exp))
	}
	return n.Lsh(n, 8*(exp-3))
}

func BigToCompact(n *big.Int) uint32 {
	if n.Sign() <= 0 {
		return 0
	}
	b := n.Bytes()
	exp := uint32(len(b))
	var mant uint32
	if exp <= 3 {
		mant = uint32(n.Uint64()) << (8 * (3 - exp))
	} else {
		mant = uint32(new(big.Int).Rsh(n, uint(8*(exp-3))).Uint64())
	}
	if mant&0x00800000 != 0 {
		mant >>= 8
		exp++
	}
	return exp<<24 | mant
}

func CheckPow(h *Header) bool {
	t := CompactToBig(h.Bits)
	if t.Sign() <= 0 || t.Cmp(CompactToBig(PowLimitBits[h.Algo()])) > 0 {
		return false
	}
	return HashToBig(PowHash(h)).Cmp(t) <= 0
}

// Work = 2^256 / (target+1)
func Work(bits uint32) *big.Int {
	t := CompactToBig(bits)
	return new(big.Int).Div(new(big.Int).Lsh(big.NewInt(1), 256), t.Add(t, big.NewInt(1)))
}

// NextBits implements LWMA over the last blocks of the same algo (times/bits oldest→newest).
func NextBits(algo int, times []uint32, bits []uint32) uint32 {
	n := len(times) - 1
	if n < 2 {
		return GenesisBits[algo]
	}
	if n > LWMAWindow {
		times, bits, n = times[len(times)-LWMAWindow-1:], bits[len(bits)-LWMAWindow-1:], LWMAWindow
	}
	T := int64(AlgoSpacing)
	var sumW int64
	sumT := new(big.Int)
	for i := 1; i <= n; i++ {
		st := int64(times[i]) - int64(times[i-1])
		if st < -6*T {
			st = -6 * T
		}
		if st > 6*T {
			st = 6 * T
		}
		sumW += st * int64(i)
		sumT.Add(sumT, CompactToBig(bits[i]))
	}
	if sumW < int64(n*(n+1))*T/20 {
		sumW = int64(n*(n+1)) * T / 20
	}
	k := int64(n*(n+1)) * T / 2
	next := new(big.Int).Mul(sumT, big.NewInt(sumW))
	next.Div(next, big.NewInt(k*int64(n)))
	lim := CompactToBig(PowLimitBits[algo])
	if next.Cmp(lim) > 0 {
		next = lim
	}
	return BigToCompact(next)
}
