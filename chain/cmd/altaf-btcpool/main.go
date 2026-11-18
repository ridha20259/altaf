// altaf-btcpool: Bitcoin stratum v1 mining pool for ALTAF.
// Waits until a synced bitcoind answers on RPC, then accepts ASIC miners on -listen.
// Blocks pay the pool address; every found block is written to blocks.jsonl with the PPLNS split
// (pool fee kept) so the exchange can credit miners after 100 confirmations.
package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	rpcURL  = env("BTC_RPC_URL", "http://127.0.0.1:8332")
	rpcUser = env("BTC_RPC_USER", "")
	rpcPass = env("BTC_RPC_PASS", "")
	payAddr = env("BTC_POOL_ADDRESS", "")
	feePct  = 2.0
	dataDir = "/var/lib/altaf-btcpool"
	diff0   = 65536.0
	tag     = []byte("/ALTAF Pool/")
	diff1   = new(big.Int).Lsh(big.NewInt(0xFFFF), 208)
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func sha256d(b []byte) []byte { a := sha256.Sum256(b); c := sha256.Sum256(a[:]); return c[:] }

func rev(b []byte) []byte {
	o := make([]byte, len(b))
	for i := range b {
		o[i] = b[len(b)-1-i]
	}
	return o
}

func unhex(s string) []byte { b, _ := hex.DecodeString(s); return b }

func varint(n uint64) []byte {
	switch {
	case n < 0xfd:
		return []byte{byte(n)}
	case n <= 0xffff:
		return binary.LittleEndian.AppendUint16([]byte{0xfd}, uint16(n))
	case n <= 0xffffffff:
		return binary.LittleEndian.AppendUint32([]byte{0xfe}, uint32(n))
	}
	return binary.LittleEndian.AppendUint64([]byte{0xff}, n)
}

// ---------- bitcoind RPC ----------

func rpc(method string, params []any, out any) error {
	body, _ := json.Marshal(map[string]any{"jsonrpc": "1.0", "id": "pool", "method": method, "params": params})
	req, _ := http.NewRequest("POST", rpcURL, bytes.NewReader(body))
	req.SetBasicAuth(rpcUser, rpcPass)
	req.Header.Set("Content-Type", "application/json")
	r, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	var res struct {
		Result json.RawMessage
		Error  *struct{ Message string }
	}
	if err := json.NewDecoder(r.Body).Decode(&res); err != nil {
		return fmt.Errorf("rpc %s: %s", method, r.Status)
	}
	if res.Error != nil {
		return errors.New(res.Error.Message)
	}
	if out != nil {
		return json.Unmarshal(res.Result, out)
	}
	return nil
}

type gbt struct {
	Version           uint32 `json:"version"`
	PreviousBlockHash string `json:"previousblockhash"`
	Transactions      []struct {
		Data string `json:"data"`
		Txid string `json:"txid"`
	} `json:"transactions"`
	CoinbaseValue            int64  `json:"coinbasevalue"`
	Bits                     string `json:"bits"`
	Height                   int64  `json:"height"`
	CurTime                  uint32 `json:"curtime"`
	DefaultWitnessCommitment string `json:"default_witness_commitment"`
}

// ---------- jobs ----------

type job struct {
	ID             string
	T              gbt
	Coinb1, Coinb2 []byte
	Branch         [][]byte
	Target         *big.Int
	Witness        bool
}

const en1Size, en2Size = 4, 4

func pushInt(n int64) []byte { // BIP34 height push
	var b []byte
	for n > 0 {
		b = append(b, byte(n&0xff))
		n >>= 8
	}
	if len(b) > 0 && b[len(b)-1]&0x80 != 0 {
		b = append(b, 0)
	}
	return append([]byte{byte(len(b))}, b...)
}

func compactToBig(c uint32) *big.Int {
	m, e := big.NewInt(int64(c&0x007fffff)), uint(c>>24)
	if e <= 3 {
		return m.Rsh(m, 8*(3-e))
	}
	return m.Lsh(m, 8*(e-3))
}

func buildJob(t gbt, payScript []byte, id string) *job {
	h := pushInt(t.Height)
	scriptLen := len(h) + en1Size + en2Size + len(tag)
	c1 := binary.LittleEndian.AppendUint32(nil, 2)
	c1 = append(c1, 1)
	c1 = append(c1, make([]byte, 32)...)
	c1 = append(c1, 0xff, 0xff, 0xff, 0xff)
	c1 = append(c1, varint(uint64(scriptLen))...)
	c1 = append(c1, h...)
	c2 := append([]byte(nil), tag...)
	c2 = append(c2, 0xff, 0xff, 0xff, 0xff)
	outs := [][]byte{append(append(binary.LittleEndian.AppendUint64(nil, uint64(t.CoinbaseValue)), varint(uint64(len(payScript)))...), payScript...)}
	wit := t.DefaultWitnessCommitment != ""
	if wit {
		ws := unhex(t.DefaultWitnessCommitment)
		outs = append(outs, append(append(binary.LittleEndian.AppendUint64(nil, 0), varint(uint64(len(ws)))...), ws...))
	}
	c2 = append(c2, varint(uint64(len(outs)))...)
	for _, o := range outs {
		c2 = append(c2, o...)
	}
	c2 = append(c2, 0, 0, 0, 0)
	// merkle branch for leaf 0 (coinbase)
	var level [][]byte
	level = append(level, nil)
	for _, tx := range t.Transactions {
		level = append(level, rev(unhex(tx.Txid)))
	}
	var br [][]byte
	for len(level) > 1 {
		br = append(br, level[1])
		if len(level)%2 == 1 {
			level = append(level, level[len(level)-1])
		}
		next := [][]byte{nil}
		for i := 1; i < len(level)/2; i++ {
			next = append(next, sha256d(append(append([]byte(nil), level[2*i]...), level[2*i+1]...)))
		}
		level = next
	}
	bits, _ := strconv.ParseUint(t.Bits, 16, 32)
	return &job{ID: id, T: t, Coinb1: c1, Coinb2: c2, Branch: br, Target: compactToBig(uint32(bits)), Witness: wit}
}

// ---------- pool state ----------

type share struct {
	User string
	Diff float64
}

type pool struct {
	mu        sync.Mutex
	jobs      map[string]*job
	cur       *job
	seq       int
	payScript []byte
	conns     map[*client]bool
	window    []share // PPLNS window
	maxWindow float64
	seen      map[string]bool
}

type client struct {
	c      net.Conn
	w      *bufio.Writer
	mu     sync.Mutex
	en1    []byte
	user   string
	diff   float64
	authed bool
}

func (cl *client) send(v any) {
	j, _ := json.Marshal(v)
	cl.mu.Lock()
	cl.w.Write(append(j, '\n'))
	cl.w.Flush()
	cl.mu.Unlock()
}

func wordSwap(b []byte) []byte { // stratum prevhash: internal bytes with each 4-byte word reversed
	o := make([]byte, 32)
	for i := 0; i < 8; i++ {
		copy(o[i*4:], rev(b[i*4:i*4+4]))
	}
	return o
}

func (p *pool) notify(cl *client, j *job, clean bool) {
	br := []string{}
	for _, b := range j.Branch {
		br = append(br, hex.EncodeToString(b))
	}
	prev := wordSwap(rev(unhex(j.T.PreviousBlockHash)))
	cl.send(map[string]any{"id": nil, "method": "mining.notify", "params": []any{j.ID, hex.EncodeToString(prev),
		hex.EncodeToString(j.Coinb1), hex.EncodeToString(j.Coinb2), br, fmt.Sprintf("%08x", j.T.Version), j.T.Bits,
		fmt.Sprintf("%08x", j.T.CurTime), clean}})
}

func (p *pool) refresh() error {
	var t gbt
	if err := rpc("getblocktemplate", []any{map[string]any{"rules": []string{"segwit"}}}, &t); err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	clean := p.cur == nil || p.cur.T.PreviousBlockHash != t.PreviousBlockHash
	if !clean && time.Since(time.Unix(int64(p.cur.T.CurTime), 0)) < 30*time.Second {
		return nil
	}
	p.seq++
	j := buildJob(t, p.payScript, strconv.FormatInt(int64(p.seq), 16))
	if clean {
		p.jobs = map[string]*job{}
		p.seen = map[string]bool{}
	}
	p.jobs[j.ID], p.cur = j, j
	for cl := range p.conns {
		if cl.authed {
			go p.notify(cl, j, clean)
		}
	}
	return nil
}

func (p *pool) submit(cl *client, params []string) (bool, error) {
	if len(params) < 5 {
		return false, errors.New("bad params")
	}
	p.mu.Lock()
	j := p.jobs[params[1]]
	key := strings.Join(params[1:5], "/") + hex.EncodeToString(cl.en1)
	dup := p.seen[key]
	p.seen[key] = true
	p.mu.Unlock()
	if j == nil {
		return false, errors.New("stale job")
	}
	if dup {
		return false, errors.New("duplicate share")
	}
	en2, ntime, nonce := unhex(params[2]), unhex(params[3]), unhex(params[4])
	if len(en2) != en2Size || len(ntime) != 4 || len(nonce) != 4 {
		return false, errors.New("bad share")
	}
	cb := append(append(append(append([]byte(nil), j.Coinb1...), cl.en1...), en2...), j.Coinb2...)
	root := sha256d(cb)
	for _, b := range j.Branch {
		root = sha256d(append(root, b...))
	}
	hdr := binary.LittleEndian.AppendUint32(nil, j.T.Version)
	hdr = append(hdr, rev(unhex(j.T.PreviousBlockHash))...)
	hdr = append(hdr, root...)
	hdr = append(hdr, rev(ntime)...)
	bits, _ := strconv.ParseUint(j.T.Bits, 16, 32)
	hdr = binary.LittleEndian.AppendUint32(hdr, uint32(bits))
	hdr = append(hdr, rev(nonce)...)
	hv := new(big.Int).SetBytes(rev(sha256d(hdr)))
	shareTarget, _ := new(big.Float).Quo(new(big.Float).SetInt(diff1), big.NewFloat(cl.diff)).Int(nil)
	if hv.Cmp(shareTarget) > 0 {
		return false, errors.New("low difficulty share")
	}
	p.addShare(cl.user, cl.diff)
	if hv.Cmp(j.Target) <= 0 {
		go p.foundBlock(j, hdr, cb)
	}
	return true, nil
}

func (p *pool) addShare(user string, d float64) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.window = append(p.window, share{user, d})
	var tot float64
	for i := len(p.window) - 1; i >= 0; i-- {
		tot += p.window[i].Diff
		if tot > p.maxWindow {
			p.window = p.window[i:]
			break
		}
	}
}

func (p *pool) foundBlock(j *job, hdr, cb []byte) {
	blk := append([]byte(nil), hdr...)
	blk = append(blk, varint(uint64(len(j.T.Transactions)+1))...)
	if j.Witness { // coinbase with the 32-byte witness reserved value
		w := append([]byte(nil), cb[:4]...)
		w = append(w, 0, 1)
		w = append(w, cb[4:len(cb)-4]...)
		w = append(w, 1, 32)
		w = append(w, make([]byte, 32)...)
		w = append(w, cb[len(cb)-4:]...)
		blk = append(blk, w...)
	} else {
		blk = append(blk, cb...)
	}
	for _, tx := range j.T.Transactions {
		blk = append(blk, unhex(tx.Data)...)
	}
	hash := hex.EncodeToString(rev(sha256d(hdr)))
	var res any
	err := rpc("submitblock", []any{hex.EncodeToString(blk)}, &res)
	p.mu.Lock()
	split := map[string]float64{}
	var tot float64
	for _, s := range p.window {
		split[s.User] += s.Diff
		tot += s.Diff
	}
	p.mu.Unlock()
	reward := float64(j.T.CoinbaseValue) * (1 - feePct/100)
	pay := map[string]int64{}
	for u, d := range split {
		pay[u] = int64(reward * d / tot)
	}
	rec, _ := json.Marshal(map[string]any{"time": time.Now().Unix(), "height": j.T.Height, "hash": hash,
		"reward_sat": j.T.CoinbaseValue, "fee_pct": feePct, "payouts_sat": pay, "submit_error": fmt.Sprint(err), "result": res})
	f, _ := os.OpenFile(filepath.Join(dataDir, "blocks.jsonl"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0640)
	if f != nil {
		f.Write(append(rec, '\n'))
		f.Close()
	}
	log.Printf("BLOCK %d %s submit err=%v result=%v", j.T.Height, hash, err, res)
	p.refresh()
}

func (p *pool) handle(c net.Conn) {
	defer c.Close()
	defer func() {
		if r := recover(); r != nil {
			log.Printf("client %s: %v", c.RemoteAddr(), r)
		}
	}()
	en1 := make([]byte, en1Size)
	rand.Read(en1)
	cl := &client{c: c, w: bufio.NewWriter(c), en1: en1, diff: diff0}
	p.mu.Lock()
	p.conns[cl] = true
	p.mu.Unlock()
	defer func() { p.mu.Lock(); delete(p.conns, cl); p.mu.Unlock() }()
	sc := bufio.NewScanner(c)
	sc.Buffer(make([]byte, 64<<10), 64<<10)
	for {
		c.SetReadDeadline(time.Now().Add(10 * time.Minute))
		if !sc.Scan() {
			return
		}
		var m struct {
			ID     any               `json:"id"`
			Method string            `json:"method"`
			Params []json.RawMessage `json:"params"`
		}
		if json.Unmarshal(sc.Bytes(), &m) != nil {
			return
		}
		ps := make([]string, len(m.Params))
		for i, r := range m.Params {
			json.Unmarshal(r, &ps[i])
		}
		switch m.Method {
		case "mining.subscribe":
			cl.send(map[string]any{"id": m.ID, "error": nil, "result": []any{[][]string{{"mining.notify", "altaf"}}, hex.EncodeToString(en1), en2Size}})
		case "mining.authorize":
			if len(ps) == 0 || ps[0] == "" {
				cl.send(map[string]any{"id": m.ID, "error": []any{24, "username required (ALTAF account e-mail or BTC address)", nil}, "result": false})
				continue
			}
			cl.user, cl.authed = strings.SplitN(ps[0], ".", 2)[0], true
			cl.send(map[string]any{"id": m.ID, "error": nil, "result": true})
			cl.send(map[string]any{"id": nil, "method": "mining.set_difficulty", "params": []any{cl.diff}})
			p.mu.Lock()
			j := p.cur
			p.mu.Unlock()
			if j != nil {
				p.notify(cl, j, true)
			}
		case "mining.suggest_difficulty":
			if d, err := strconv.ParseFloat(strings.TrimSpace(string(m.Params[0])), 64); err == nil && d > 0 {
				cl.diff = d
				cl.send(map[string]any{"id": nil, "method": "mining.set_difficulty", "params": []any{d}})
			}
		case "mining.submit":
			ok, err := p.submit(cl, ps)
			if err != nil {
				cl.send(map[string]any{"id": m.ID, "error": []any{23, err.Error(), nil}, "result": false})
			} else {
				cl.send(map[string]any{"id": m.ID, "error": nil, "result": ok})
			}
		default:
			cl.send(map[string]any{"id": m.ID, "error": []any{20, "unsupported", nil}, "result": nil})
		}
	}
}

func main() {
	listen := flag.String("listen", "0.0.0.0:3334", "stratum listen address")
	flag.Float64Var(&feePct, "fee", feePct, "pool fee percent")
	flag.Float64Var(&diff0, "diff", diff0, "starting share difficulty")
	flag.StringVar(&dataDir, "data", dataDir, "data directory")
	flag.Parse()
	os.MkdirAll(dataDir, 0750)
	if payAddr == "" {
		log.Fatal("BTC_POOL_ADDRESS not set")
	}
	p := &pool{jobs: map[string]*job{}, conns: map[*client]bool{}, seen: map[string]bool{}, maxWindow: 1e15}
	log.Printf("waiting for a synced bitcoind at %s", rpcURL)
	for {
		var info struct {
			IBD    bool `json:"initialblockdownload"`
			Blocks int  `json:"blocks"`
		}
		var va struct {
			IsValid      bool   `json:"isvalid"`
			ScriptPubKey string `json:"scriptPubKey"`
		}
		if rpc("getblockchaininfo", nil, &info) == nil && !info.IBD && rpc("validateaddress", []any{payAddr}, &va) == nil && va.IsValid {
			p.payScript = unhex(va.ScriptPubKey)
			log.Printf("bitcoind synced at %d blocks; pool address %s", info.Blocks, payAddr)
			break
		}
		time.Sleep(60 * time.Second)
	}
	if err := p.refresh(); err != nil {
		log.Fatal(err)
	}
	go func() {
		for {
			time.Sleep(5 * time.Second)
			if err := p.refresh(); err != nil {
				log.Printf("template: %v", err)
			}
		}
	}()
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("ALTAF Bitcoin pool listening on %s (fee %.1f%%)", *listen, feePct)
	for {
		c, err := ln.Accept()
		if err == nil {
			go p.handle(c)
		}
	}
}
