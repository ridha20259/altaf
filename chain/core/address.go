package core

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"math/big"
)

const b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

func b58enc(b []byte) string {
	n := new(big.Int).SetBytes(b)
	var out []byte
	m, z := new(big.Int), big.NewInt(58)
	for n.Sign() > 0 {
		n.DivMod(n, z, m)
		out = append(out, b58[m.Int64()])
	}
	for _, c := range b {
		if c != 0 {
			break
		}
		out = append(out, '1')
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return string(out)
}

func b58dec(s string) ([]byte, error) {
	n := new(big.Int)
	for _, c := range []byte(s) {
		i := bytes.IndexByte([]byte(b58), c)
		if i < 0 {
			return nil, errors.New("bad base58")
		}
		n.Mul(n, big.NewInt(58)).Add(n, big.NewInt(int64(i)))
	}
	b := n.Bytes()
	for _, c := range []byte(s) {
		if c != '1' {
			break
		}
		b = append([]byte{0}, b...)
	}
	return b, nil
}

func EncodeAddress(raw []byte) string {
	c := sha256.Sum256(raw)
	c = sha256.Sum256(c[:])
	return b58enc(append(append([]byte(nil), raw...), c[:4]...))
}

func DecodeAddress(a string) ([]byte, error) {
	b, err := b58dec(a)
	if err != nil || len(b) != 25 || b[0] != AddrVersion {
		return nil, errors.New("bad address")
	}
	c := sha256.Sum256(b[:21])
	c = sha256.Sum256(c[:])
	if !bytes.Equal(c[:4], b[21:]) {
		return nil, errors.New("bad address checksum")
	}
	return b[:21], nil
}

func ValidAddress(a string) bool { _, err := DecodeAddress(a); return err == nil }

func AddressFromPub(pub []byte) string {
	h := sha256.Sum256(pub)
	return EncodeAddress(append([]byte{AddrVersion}, h[:20]...))
}
