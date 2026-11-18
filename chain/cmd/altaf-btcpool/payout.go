package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const maturity = 101

type payState struct {
	Done     map[string]string `json:"done"`     // block hash -> "credited" | "orphan"
	Owed     map[string]int64  `json:"owed"`     // address -> satoshi not yet sent
	Inflight *payBatch         `json:"inflight"` // batch being sent (crash recovery)
}

type payBatch struct {
	ID      string           `json:"id"`
	Amounts map[string]int64 `json:"amounts"`
}

func statePath() string { return filepath.Join(dataDir, "payouts-state.json") }

func loadState() *payState {
	st := &payState{Done: map[string]string{}, Owed: map[string]int64{}}
	if b, err := os.ReadFile(statePath()); err == nil {
		if err := json.Unmarshal(b, st); err != nil {
			log.Fatalf("payouts-state.json is corrupt: %v", err)
		}
	}
	if st.Done == nil {
		st.Done = map[string]string{}
	}
	if st.Owed == nil {
		st.Owed = map[string]int64{}
	}
	return st
}

func saveState(st *payState) error {
	b, _ := json.MarshalIndent(st, "", " ")
	tmp := statePath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0640); err != nil {
		return err
	}
	return os.Rename(tmp, statePath())
}

func appendLog(name string, v any) {
	b, _ := json.Marshal(v)
	if f, err := os.OpenFile(filepath.Join(dataDir, name), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0640); err == nil {
		f.Write(append(b, '\n'))
		f.Close()
	}
}

type blockRec struct {
	Time   int64            `json:"time"`
	Height int64            `json:"height"`
	Hash   string           `json:"hash"`
	Pay    map[string]int64 `json:"payouts_sat"`
	Err    string           `json:"submit_error"`
	Result any              `json:"result"`
}

func readBlocks() []blockRec {
	f, err := os.Open(filepath.Join(dataDir, "blocks.jsonl"))
	if err != nil {
		return nil
	}
	defer f.Close()
	var out []blockRec
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var r blockRec
		if json.Unmarshal(sc.Bytes(), &r) == nil && r.Hash != "" {
			out = append(out, r)
		}
	}
	return out
}

func btc(sat int64) json.Number { return json.Number(fmt.Sprintf("%d.%08d", sat/1e8, sat%1e8)) }

// recover decides whether an interrupted batch reached the wallet, using the comment stored with the tx.
func recoverInflight(st *payState) error {
	b := st.Inflight
	var txs []struct {
		Txid    string `json:"txid"`
		Comment string `json:"comment"`
	}
	if err := rpcWallet("listtransactions", []any{"*", 1000}, &txs); err != nil {
		return err
	}
	for _, t := range txs {
		if t.Comment == "ALTAF pool payout "+b.ID {
			appendLog("payouts.jsonl", map[string]any{"time": time.Now().Unix(), "batch": b.ID, "txid": t.Txid, "amounts_sat": b.Amounts, "recovered": true})
			st.Inflight = nil
			return saveState(st)
		}
	}
	for a, v := range b.Amounts {
		st.Owed[a] += v
	}
	st.Inflight = nil
	return saveState(st)
}

func payoutOnce(st *payState) error {
	if st.Inflight != nil {
		if err := recoverInflight(st); err != nil {
			return err
		}
	}
	changed := false
	for _, r := range readBlocks() {
		if st.Done[r.Hash] != "" {
			continue
		}
		var hdr struct {
			Confirmations int64 `json:"confirmations"`
		}
		err := rpc("getblockheader", []any{r.Hash}, &hdr)
		switch {
		case err != nil && time.Since(time.Unix(r.Time, 0)) > 6*time.Hour, err == nil && hdr.Confirmations < 0:
			st.Done[r.Hash] = "orphan"
			log.Printf("block %d %s orphaned: no payout", r.Height, r.Hash)
		case err == nil && hdr.Confirmations >= maturity:
			for a, v := range r.Pay {
				st.Owed[a] += v
			}
			st.Done[r.Hash] = "credited"
			log.Printf("block %d %s mature: credited %d miners", r.Height, r.Hash, len(r.Pay))
		default:
			continue
		}
		changed = true
	}
	if changed {
		if err := saveState(st); err != nil {
			return err
		}
	}
	batch := map[string]int64{}
	var total int64
	for a, v := range st.Owed {
		if v >= minPay && v > 0 {
			batch[a] = v
			total += v
		}
	}
	if len(batch) == 0 {
		return nil
	}
	var bal float64
	if err := rpcWallet("getbalance", nil, &bal); err != nil {
		return err
	}
	if int64(bal*1e8+0.5) < total {
		return fmt.Errorf("wallet balance %.8f BTC < payouts %.8f BTC, waiting", bal, float64(total)/1e8)
	}
	id := fmt.Sprintf("%d", time.Now().UnixNano())
	amounts := map[string]json.Number{}
	var addrs []string
	for a, v := range batch {
		amounts[a] = btc(v)
		addrs = append(addrs, a)
		delete(st.Owed, a)
	}
	sort.Strings(addrs)
	st.Inflight = &payBatch{ID: id, Amounts: batch}
	if err := saveState(st); err != nil {
		return err
	}
	var txid string
	err := rpcWallet("sendmany", []any{"", amounts, 1, "ALTAF pool payout " + id, addrs}, &txid)
	if err != nil {
		if strings.Contains(err.Error(), "timeout") || strings.Contains(err.Error(), "connection") {
			return err // outcome unknown: recoverInflight checks the wallet next round
		}
		for a, v := range batch {
			st.Owed[a] += v
		}
		st.Inflight = nil
		saveState(st)
		return fmt.Errorf("sendmany: %v", err)
	}
	appendLog("payouts.jsonl", map[string]any{"time": time.Now().Unix(), "batch": id, "txid": txid, "amounts_sat": batch})
	log.Printf("PAID %d miners %.8f BTC (fee taken from amounts) txid %s", len(batch), float64(total)/1e8, txid)
	st.Inflight = nil
	return saveState(st)
}

func payoutLoop(every time.Duration) {
	st := loadState()
	for {
		if err := payoutOnce(st); err != nil {
			log.Printf("payout: %v", err)
		}
		time.Sleep(every)
	}
}
