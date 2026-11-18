// altaf-stratum: Stratum v1 server so standard SHA-256 ASIC miners can mine ALTAF.
// Worker username = the ALTAF address that receives the reward (optionally "address.rig1").
package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"altafchain/core"
)

var (
	nodeURL  = flag.String("node", "http://127.0.0.1:8545", "ALTAF node URL")
	listen   = flag.String("listen", "0.0.0.0:3333", "stratum listen address")
	startDif = flag.Float64("diff", 65536, "starting share difficulty")
	minDif   = flag.Float64("mindiff", 1, "minimum share difficulty")
	network  = flag.String("network", "mainnet", "network")
	tipGen   atomic.Uint64
	jobSeq   atomic.Uint64
	diff1    = new(big.Int).Lsh(big.NewInt(0xffff), 208)
)

type job struct {
	id    string
	t     *core.Template
	coinb []byte // prefix (coinb1)
	tag   []byte // coinb2
	br    []core.Hash
}

type conn struct {
	c      net.Conn
	w      *bufio.Writer
	mu     sync.Mutex
	en1    []byte
	addr   string
	diff   float64
	bits   uint32
	jobs   map[string]*job
	order  []string
	shares int
	since  time.Time
	seen   map[string]bool
}

func (s *conn) send(v any) {
	j, _ := json.Marshal(v)
	s.mu.Lock()
	s.w.Write(append(j, '\n'))
	s.w.Flush()
	s.mu.Unlock()
}

func getTemplate(addr string) (*core.Template, error) {
	r, err := http.Get(fmt.Sprintf("%s/template?algo=%d&miner=%s", *nodeURL, core.AlgoSHA, addr))
	if err != nil {
		return nil, err
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		b, _ := io.ReadAll(r.Body)
		return nil, errors.New(string(b))
	}
	var t core.Template
	return &t, json.NewDecoder(r.Body).Decode(&t)
}

func be32(v uint32) string { b := make([]byte, 4); binary.BigEndian.PutUint32(b, v); return hex.EncodeToString(b) }

func stratumPrev(h core.Hash) string {
	b := make([]byte, 32)
	for i := 0; i < 8; i++ {
		for j := 0; j < 4; j++ {
			b[i*4+j] = h[i*4+3-j]
		}
	}
	return hex.EncodeToString(b)
}

func (s *conn) newJob(clean bool) {
	t, err := getTemplate(s.addr)
	if err != nil {
		log.Printf("template: %v", err)
		return
	}
	t.Version |= 0x20000000 // looks like a normal Bitcoin header to ASIC firmware; algo = low byte (0 = SHA)
	j := &job{id: fmt.Sprintf("%x", jobSeq.Add(1)), t: t, coinb: t.Prefix, tag: []byte("altaf-stratum"), br: core.MerkleBranch(t.TxIDs)}
	s.mu.Lock()
	s.jobs[j.id] = j
	s.order = append(s.order, j.id)
	if len(s.order) > 8 {
		delete(s.jobs, s.order[0])
		s.order = s.order[1:]
	}
	if clean {
		s.seen = map[string]bool{}
	}
	s.bits = t.Bits
	s.mu.Unlock()
	if bd := blockDiff(t.Bits); s.diff > bd {
		s.setDiff(bd)
	}
	br := make([]string, len(j.br))
	for i, h := range j.br {
		br[i] = hex.EncodeToString(h[:])
	}
	s.send(map[string]any{"id": nil, "method": "mining.notify", "params": []any{
		j.id, stratumPrev(t.Prev), hex.EncodeToString(j.coinb), hex.EncodeToString(j.tag), br,
		be32(t.Version), be32(t.Bits), be32(t.Time), clean}})
}

func (s *conn) setDiff(d float64) {
	if d < *minDif {
		d = *minDif
	}
	if s.bits != 0 && d > blockDiff(s.bits) {
		d = blockDiff(s.bits)
	}
	s.diff = d
	s.send(map[string]any{"id": nil, "method": "mining.set_difficulty", "params": []any{d}})
}

// blockDiff is the share difficulty whose target equals the block target, so every block is submitted as a share.
func blockDiff(bits uint32) float64 {
	d, _ := new(big.Float).Quo(new(big.Float).SetInt(diff1), new(big.Float).SetInt(core.CompactToBig(bits))).Float64()
	return d
}

func shareTarget(d float64) *big.Int {
	f := new(big.Float).Quo(new(big.Float).SetInt(diff1), big.NewFloat(d))
	t, _ := f.Int(nil)
	return t
}

func parseHex32(s string) (uint32, error) {
	b, err := hex.DecodeString(s)
	if err != nil || len(b) != 4 {
		return 0, errors.New("bad hex32")
	}
	return binary.BigEndian.Uint32(b), nil
}

func (s *conn) submit(p []string) (bool, error) {
	if len(p) < 5 || s.addr == "" {
		return false, errors.New("unauthorized or bad params")
	}
	s.mu.Lock()
	j := s.jobs[p[1]]
	key := strings.Join(p[1:5], ":")
	dup := s.seen[key]
	s.seen[key] = true
	s.mu.Unlock()
	if j == nil {
		return false, errors.New("job not found")
	}
	if dup {
		return false, errors.New("duplicate share")
	}
	en2, err := hex.DecodeString(p[2])
	if err != nil || len(en2) != 4 {
		return false, errors.New("bad extranonce2")
	}
	ntime, err1 := parseHex32(p[3])
	nonce, err2 := parseHex32(p[4])
	if err1 != nil || err2 != nil {
		return false, errors.New("bad ntime/nonce")
	}
	if ntime < j.t.MinTime || int64(ntime) > time.Now().Unix()+600 {
		return false, errors.New("ntime out of range")
	}
	b := &core.Block{Txs: j.t.Txs}
	b.Coinbase = append(append(append(append([]byte(nil), j.coinb...), s.en1...), en2...), j.tag...)
	b.Header = core.Header{Version: j.t.Version, Prev: j.t.Prev, Time: ntime, Bits: j.t.Bits, Nonce: nonce}
	b.Header.Merkle = b.ComputeMerkle()
	h := core.HashToBig(b.Header.Hash())
	if h.Cmp(shareTarget(s.diff)) > 0 {
		return false, errors.New("low difficulty share")
	}
	s.shares++
	if core.CheckPow(&b.Header) {
		jb, _ := json.Marshal(b)
		r, err := http.Post(*nodeURL+"/submit", "application/json", bytes.NewReader(jb))
		if err == nil {
			out, _ := io.ReadAll(r.Body)
			r.Body.Close()
			log.Printf("BLOCK %d by %s: %s", j.t.Height, s.addr, bytes.TrimSpace(out))
		}
	}
	return true, nil
}

func (s *conn) vardiff() {
	el := time.Since(s.since).Seconds()
	if el < 60 {
		return
	}
	rate := float64(s.shares) / el // target 1 share / 10 s
	nd := s.diff
	if rate > 0.2 {
		nd = s.diff * rate * 10
	} else if rate < 0.05 {
		nd = s.diff / 2
		if rate > 0 {
			nd = s.diff * rate * 10
		}
	}
	s.shares, s.since = 0, time.Now()
	if nd != s.diff {
		s.setDiff(nd)
	}
}

func handle(c net.Conn) {
	defer c.Close()
	s := &conn{c: c, w: bufio.NewWriter(c), en1: make([]byte, 4), jobs: map[string]*job{}, seen: map[string]bool{}, diff: *startDif, since: time.Now()}
	rand.Read(s.en1)
	done := make(chan struct{})
	defer close(done)
	go func() {
		last := tipGen.Load()
		tick := time.NewTicker(time.Second)
		defer tick.Stop()
		n := 0
		for {
			select {
			case <-done:
				return
			case <-tick.C:
			}
			if s.addr == "" {
				continue
			}
			n++
			if g := tipGen.Load(); g != last {
				last = g
				s.newJob(true)
				n = 0
			} else if n >= 30 {
				s.newJob(false)
				n = 0
			}
			s.vardiff()
		}
	}()
	rd := bufio.NewReaderSize(c, 4096)
	for {
		c.SetReadDeadline(time.Now().Add(10 * time.Minute))
		line, err := rd.ReadBytes('\n')
		if err != nil {
			return
		}
		var req struct {
			ID     any               `json:"id"`
			Method string            `json:"method"`
			Params []json.RawMessage `json:"params"`
		}
		if json.Unmarshal(line, &req) != nil {
			continue
		}
		var ps []string
		for _, p := range req.Params {
			var x string
			json.Unmarshal(p, &x)
			ps = append(ps, x)
		}
		reply := func(res any, e error) {
			var ev any
			if e != nil {
				ev = []any{20, e.Error(), nil}
			}
			s.send(map[string]any{"id": req.ID, "result": res, "error": ev})
		}
		switch req.Method {
		case "mining.subscribe":
			reply([]any{[][]string{{"mining.set_difficulty", "1"}, {"mining.notify", "1"}}, hex.EncodeToString(s.en1), 4}, nil)
		case "mining.authorize":
			a := ""
			if len(ps) > 0 {
				a = strings.SplitN(ps[0], ".", 2)[0]
			}
			if !core.ValidAddress(a) {
				reply(false, errors.New("username must be your ALTAF address"))
				continue
			}
			s.addr = a
			reply(true, nil)
			log.Printf("miner %s connected from %s", a, c.RemoteAddr())
			s.setDiff(s.diff)
			s.newJob(true)
		case "mining.submit":
			ok, err := s.submit(ps)
			reply(ok, err)
		case "mining.configure", "mining.extranonce.subscribe":
			reply(false, nil)
		default:
			reply(nil, errors.New("unknown method"))
		}
	}
}

func main() {
	flag.Parse()
	core.UseNetwork(*network)
	go func() {
		var tip string
		for {
			r, err := http.Get(*nodeURL + "/status")
			if err == nil {
				var st struct{ Tip string }
				json.NewDecoder(r.Body).Decode(&st)
				r.Body.Close()
				if st.Tip != tip {
					tip = st.Tip
					tipGen.Add(1)
				}
			}
			time.Sleep(time.Second)
		}
	}()
	l, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("ALTAF stratum (SHA-256) on %s -> %s", *listen, *nodeURL)
	for {
		c, err := l.Accept()
		if err == nil {
			go handle(c)
		}
	}
}
