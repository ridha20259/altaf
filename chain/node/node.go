package node

import (
	"bytes"
	"embed"
	"io/fs"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"altafchain/core"
)

type Node struct {
	C      *core.Chain
	mu     sync.Mutex
	Peers  map[string]bool
	Self   string
	client *http.Client
}

func New(c *core.Chain, self string, peers []string) *Node {
	n := &Node{C: c, Peers: map[string]bool{}, Self: self, client: &http.Client{Timeout: 15 * time.Second}}
	for _, p := range peers {
		if p = strings.TrimRight(strings.TrimSpace(p), "/"); p != "" {
			n.Peers[p] = true
		}
	}
	return n
}

func (n *Node) peerList() []string {
	n.mu.Lock()
	defer n.mu.Unlock()
	var l []string
	for p := range n.Peers {
		l = append(l, p)
	}
	return l
}

func (n *Node) broadcast(path string, v any) {
	j, _ := json.Marshal(v)
	for _, p := range n.peerList() {
		go func(p string) {
			r, err := n.client.Post(p+path, "application/json", bytes.NewReader(j))
			if err == nil {
				r.Body.Close()
			}
		}(p)
	}
}

func (n *Node) getJSON(url string, v any) error {
	r, err := n.client.Get(url)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return fmt.Errorf("%s: %s", url, r.Status)
	}
	return json.NewDecoder(r.Body).Decode(v)
}

// SubmitBlock validates, stores and relays a block (fetching unknown parents from peer if given).
func (n *Node) SubmitBlock(b *core.Block, from string) error {
	err := n.C.AddBlock(b)
	if err == core.ErrOrphan && from != "" {
		if err = n.fetchAncestors(from, b.Header.Prev, 0); err == nil {
			err = n.C.AddBlock(b)
		}
	}
	if err == nil {
		h, _ := n.C.Tip()
		log.Printf("block %d %s algo=%d", n.C.Height(), h, b.Header.Algo())
		n.broadcast("/p2p/block?from="+n.Self, b)
	}
	return err
}

func (n *Node) fetchAncestors(peer string, h core.Hash, depth int) error {
	if depth > core.MaxReorgDepth+5 {
		return errors.New("fork too deep")
	}
	var b core.Block
	if err := n.getJSON(peer+"/block/"+h.String(), &b); err != nil {
		return err
	}
	err := n.C.AddBlock(&b)
	if err == core.ErrOrphan {
		if err = n.fetchAncestors(peer, b.Header.Prev, depth+1); err == nil {
			err = n.C.AddBlock(&b)
		}
	}
	if err == core.ErrKnown {
		return nil
	}
	return err
}

type Status struct {
	Network string       `json:"network"`
	Height  uint64       `json:"height"`
	Tip     core.Hash    `json:"tip"`
	G       core.Globals `json:"supply"`
	Peers   int          `json:"peers"`
	Mempool int          `json:"mempool"`
	Bits    [2]uint32    `json:"bits"`
	Reward  [2]uint64    `json:"next_reward"`
}

func (n *Node) status() Status {
	h, ht := n.C.Tip()
	s := Status{Network: n.C.P.Network, Height: ht, Tip: h, G: n.C.Globals(), Peers: len(n.peerList()), Mempool: n.C.MempoolSize()}
	for a := 0; a < core.NumAlgos; a++ {
		if t, err := n.C.Template(a, n.C.P.Founder); err == nil {
			s.Bits[a], s.Reward[a] = t.Bits, t.Reward
		}
	}
	return s
}

// SyncLoop pulls missing blocks from peers.
func (n *Node) SyncLoop() {
	for {
		for _, p := range n.peerList() {
			var s Status
			if err := n.getJSON(p+"/status", &s); err != nil || s.Network != n.C.P.Network {
				continue
			}
			if n.Self != "" {
				if r, err := n.client.Post(p+"/p2p/hello?url="+url.QueryEscape(n.Self), "application/json", nil); err == nil {
					r.Body.Close()
				}
			}
			for n.C.Height() < s.Height {
				next := n.C.Height() + 1
				var b core.Block
				if err := n.getJSON(p+"/block/"+strconv.FormatUint(next, 10), &b); err != nil {
					break
				}
				if err := n.SubmitBlock(&b, p); err != nil && err != core.ErrKnown {
					log.Printf("sync from %s at %d: %v", p, next, err)
					break
				}
			}
			var more []string
			if n.getJSON(p+"/peers", &more) == nil {
				n.mu.Lock()
				for _, x := range more {
					if x != n.Self && x != "" && len(n.Peers) < 64 {
						n.Peers[strings.TrimRight(x, "/")] = true
					}
				}
				n.mu.Unlock()
			}
		}
		time.Sleep(10 * time.Second)
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, err error) { writeJSON(w, 400, map[string]string{"error": err.Error()}) }

//go:embed web
var webFS embed.FS

type BlockSummary struct {
	Height uint64    `json:"height"`
	Hash   core.Hash `json:"hash"`
	Algo   int       `json:"algo"`
	Time   uint32    `json:"time"`
	Txs    int       `json:"txs"`
	Miner  string    `json:"miner"`
}

func (n *Node) Handler() http.Handler {
	m := http.NewServeMux()
	sub, _ := fs.Sub(webFS, "web")
	m.Handle("GET /{$}", http.FileServerFS(sub))
	m.Handle("GET /i18n.js", http.FileServerFS(sub))
	m.HandleFunc("GET /blocks", func(w http.ResponseWriter, r *http.Request) {
		top := n.C.Height()
		if v, err := strconv.ParseUint(r.URL.Query().Get("from"), 10, 64); err == nil && v < top {
			top = v
		}
		out := []BlockSummary{}
		for h := int64(top); h >= 0 && len(out) < 20; h-- {
			b, err := n.C.BlockByHeight(uint64(h))
			if err != nil {
				break
			}
			_, miner, _ := core.ParseCoinbase(b.Coinbase)
			out = append(out, BlockSummary{uint64(h), b.Header.Hash(), b.Header.Algo(), b.Header.Time, len(b.Txs), miner})
		}
		writeJSON(w, 200, out)
	})
	m.HandleFunc("GET /status", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, n.status()) })
	m.HandleFunc("GET /peers", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, n.peerList()) })
	m.HandleFunc("GET /account/{addr}", func(w http.ResponseWriter, r *http.Request) {
		a := r.PathValue("addr")
		if !core.ValidAddress(a) {
			fail(w, errors.New("bad address"))
			return
		}
		writeJSON(w, 200, n.C.Account(a))
	})
	m.HandleFunc("GET /block/{id}", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		var b *core.Block
		var err error
		if len(id) == 64 {
			var h core.Hash
			if h, err = core.HashFromString(id); err == nil {
				b, err = n.C.BlockByHash(h)
			}
		} else {
			var ht uint64
			if ht, err = strconv.ParseUint(id, 10, 64); err == nil {
				b, err = n.C.BlockByHeight(ht)
			}
		}
		if err != nil {
			writeJSON(w, 404, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, b)
	})
	m.HandleFunc("GET /mempool", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, n.C.MempoolTxs()) })
	m.HandleFunc("GET /template", func(w http.ResponseWriter, r *http.Request) {
		algo, _ := strconv.Atoi(r.URL.Query().Get("algo"))
		t, err := n.C.Template(algo, r.URL.Query().Get("miner"))
		if err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, 200, t)
	})
	block := func(w http.ResponseWriter, r *http.Request) {
		var b core.Block
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(&b); err != nil {
			fail(w, err)
			return
		}
		from := r.URL.Query().Get("from")
		if from != "" && !n.knownPeer(from) {
			from = ""
		}
		if err := n.SubmitBlock(&b, from); err != nil && err != core.ErrKnown {
			fail(w, err)
			return
		}
		writeJSON(w, 200, map[string]string{"hash": b.Header.Hash().String()})
	}
	m.HandleFunc("POST /submit", block)
	m.HandleFunc("POST /p2p/block", block)
	m.HandleFunc("POST /p2p/hello", func(w http.ResponseWriter, r *http.Request) {
		u := strings.TrimRight(strings.TrimSpace(r.URL.Query().Get("url")), "/")
		pu, err := url.Parse(u)
		if err != nil || (pu.Scheme != "http" && pu.Scheme != "https") || pu.Host == "" || u == n.Self {
			fail(w, errors.New("bad peer url"))
			return
		}
		if n.knownPeer(u) {
			writeJSON(w, 200, map[string]string{"peer": "known"})
			return
		}
		var s Status
		if err := n.getJSON(u+"/status", &s); err != nil || s.Network != n.C.P.Network {
			fail(w, errors.New("peer not reachable or wrong network"))
			return
		}
		n.mu.Lock()
		added := len(n.Peers) < 64
		if added {
			n.Peers[u] = true
		}
		n.mu.Unlock()
		if !added {
			fail(w, errors.New("peer list full"))
			return
		}
		log.Printf("new peer %s", u)
		writeJSON(w, 200, map[string]string{"peer": "added"})
	})
	tx := func(w http.ResponseWriter, r *http.Request) {
		var t core.Tx
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256<<10)).Decode(&t); err != nil {
			fail(w, err)
			return
		}
		err := n.C.AddTx(t)
		if err == nil {
			n.broadcast("/p2p/tx", t)
		} else if err != core.ErrKnown {
			fail(w, err)
			return
		}
		writeJSON(w, 200, map[string]string{"txid": t.ID().String()})
	}
	m.HandleFunc("POST /tx", tx)
	m.HandleFunc("POST /p2p/tx", tx)
	m.HandleFunc("GET /tx/{id}", func(w http.ResponseWriter, r *http.Request) {
		h, err := core.HashFromString(r.PathValue("id"))
		if err != nil {
			fail(w, err)
			return
		}
		loc, ok := n.C.TxLocation(h)
		if !ok {
			writeJSON(w, 404, map[string]string{"error": "not found"})
			return
		}
		writeJSON(w, 200, loc)
	})
	m.HandleFunc("GET /receipt/{id}", func(w http.ResponseWriter, r *http.Request) {
		h, err := core.HashFromString(r.PathValue("id"))
		if err != nil {
			fail(w, err)
			return
		}
		rc, ok := n.C.Receipt(h)
		if !ok {
			writeJSON(w, 404, map[string]string{"error": "no contract receipt for this tx"})
			return
		}
		writeJSON(w, 200, rc)
	})
	m.HandleFunc("GET /contract/{addr}", func(w http.ResponseWriter, r *http.Request) {
		a := r.PathValue("addr")
		e, err := core.EvmAddress(a)
		if err != nil {
			fail(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"address": a, "evm_address": e.Hex(), "balance": n.C.Account(a).Balance,
			"code": core.HexBytes(n.C.Code(a)), "chain_id": core.ChainID})
	})
	m.HandleFunc("POST /call", func(w http.ResponseWriter, r *http.Request) {
		var q struct {
			From  string        `json:"from"`
			To    string        `json:"to"`
			Data  core.HexBytes `json:"data"`
			Value uint64        `json:"value"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256<<10)).Decode(&q); err != nil {
			fail(w, err)
			return
		}
		if (q.To != "" && !core.ValidAddress(q.To)) || (q.From != "" && !core.ValidAddress(q.From)) {
			fail(w, errors.New("bad address"))
			return
		}
		writeJSON(w, 200, n.C.CallContract(q.From, q.To, q.Data, q.Value))
	})
	m.HandleFunc("GET /history/{addr}", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, n.C.History(r.PathValue("addr")))
	})
	return m
}

func (n *Node) knownPeer(p string) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.Peers[strings.TrimRight(p, "/")]
}
