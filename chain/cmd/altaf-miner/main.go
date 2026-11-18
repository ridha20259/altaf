package main

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	"altafchain/core"
)

func main() {
	nodeURL := flag.String("node", "http://127.0.0.1:8545", "ALTAF node URL")
	addr := flag.String("address", "", "your ALTAF address (rewards go here)")
	algo := flag.String("algo", "cpu", "cpu (ordinary computer) | sha (SHA-256)")
	threads := flag.Int("threads", runtime.NumCPU(), "mining threads")
	network := flag.String("network", "mainnet", "network (for difficulty limits)")
	flag.Parse()
	core.UseNetwork(*network)
	if !core.ValidAddress(*addr) {
		log.Fatal("give a valid -address (create one with: altaf-wallet new key.json)")
	}
	a := core.AlgoCPU
	if *algo == "sha" {
		a = core.AlgoSHA
	}
	var hashes atomic.Uint64
	go func() {
		for range time.Tick(30 * time.Second) {
			log.Printf("hashrate %.1f H/s", float64(hashes.Swap(0))/30)
		}
	}()
	for {
		r, err := http.Get(fmt.Sprintf("%s/template?algo=%d&miner=%s", *nodeURL, a, *addr))
		if err != nil {
			log.Print(err)
			time.Sleep(10 * time.Second)
			continue
		}
		var t core.Template
		err = json.NewDecoder(r.Body).Decode(&t)
		r.Body.Close()
		if err != nil {
			log.Print(err)
			time.Sleep(10 * time.Second)
			continue
		}
		var found atomic.Pointer[core.Block]
		stop := time.Now().Add(20 * time.Second)
		var wg sync.WaitGroup
		for i := 0; i < *threads; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				en := make([]byte, 8)
				rand.Read(en)
				b := &core.Block{Coinbase: append(append(append([]byte(nil), t.Prefix...), en...), "altaf-miner"...), Txs: t.Txs}
				b.Header = core.Header{Version: t.Version, Prev: t.Prev, Time: t.Time, Bits: t.Bits}
				b.Header.Merkle = b.ComputeMerkle()
				for n := uint64(0); found.Load() == nil && time.Now().Before(stop); n++ {
					b.Header.Nonce = uint32(n)
					hashes.Add(1)
					if core.CheckPow(&b.Header) {
						found.Store(b)
						return
					}
					if n == 0xffffffff {
						binary.LittleEndian.PutUint64(b.Coinbase[core.CoinbasePrefixLen:], binary.LittleEndian.Uint64(en)+1)
						b.Header.Merkle = b.ComputeMerkle()
					}
				}
			}()
		}
		wg.Wait()
		if b := found.Load(); b != nil {
			j, _ := json.Marshal(b)
			r, err := http.Post(*nodeURL+"/submit", "application/json", bytes.NewReader(j))
			if err == nil {
				out, _ := io.ReadAll(r.Body)
				r.Body.Close()
				log.Printf("found block %d (reward %.6f ALTF before 10%% founder share): %s", t.Height, float64(t.Reward)/1e6, bytes.TrimSpace(out))
			}
		}
	}
}
