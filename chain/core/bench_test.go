package core

import "testing"

func BenchmarkCPUPow(b *testing.B) {
	h := Header{Version: AlgoCPU}
	for i := 0; i < b.N; i++ {
		h.Nonce = uint32(i)
		PowHash(&h)
	}
}
