package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"

	"altafchain/core"
)

func usage() {
	fmt.Println(`altaf-wallet new <keyfile>
altaf-wallet address <keyfile>
altaf-wallet balance <node-url> <address>
altaf-wallet send <node-url> <keyfile> <to-address> <amount-ALTF> [fee-ALTF] [memo]
altaf-wallet deploy <node-url> <keyfile> <bytecode-hex-file> [fee-ALTF]          (Solidity/EVM contract)
altaf-wallet call <node-url> <keyfile> <contract> <calldata-hex> [amount-ALTF] [fee-ALTF]
altaf-wallet read <node-url> <contract> <calldata-hex>                           (free, read-only)
altaf-wallet receipt <node-url> <txid>`)
	os.Exit(1)
}

func load(f string) ed25519.PrivateKey {
	b, err := os.ReadFile(f)
	if err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
	var k struct{ Seed string }
	json.Unmarshal(b, &k)
	s, err := hex.DecodeString(k.Seed)
	if err != nil || len(s) != 32 {
		fmt.Println("bad key file")
		os.Exit(1)
	}
	return ed25519.NewKeyFromSeed(s)
}

func units(s string) uint64 {
	f, err := strconv.ParseFloat(s, 64)
	if err != nil || f <= 0 {
		fmt.Println("bad amount", s)
		os.Exit(1)
	}
	return uint64(math.Round(f * float64(core.Unit)))
}

func main() {
	if len(os.Args) < 3 {
		usage()
	}
	switch os.Args[1] {
	case "new":
		if _, err := os.Stat(os.Args[2]); err == nil {
			fmt.Println("file exists, refusing to overwrite")
			os.Exit(1)
		}
		seed := make([]byte, 32)
		rand.Read(seed)
		k := ed25519.NewKeyFromSeed(seed)
		a := core.AddressFromPub(k.Public().(ed25519.PublicKey))
		j, _ := json.Marshal(map[string]string{"address": a, "seed": hex.EncodeToString(seed)})
		os.WriteFile(os.Args[2], j, 0600)
		fmt.Println(a)
	case "address":
		fmt.Println(core.AddressFromPub(load(os.Args[2]).Public().(ed25519.PublicKey)))
	case "balance":
		if len(os.Args) < 4 {
			usage()
		}
		r, err := http.Get(os.Args[2] + "/account/" + os.Args[3])
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		var a core.Account
		json.NewDecoder(r.Body).Decode(&a)
		fmt.Printf("%.6f ALTF (mined %.6f)\n", float64(a.Balance)/1e6, float64(a.Mined)/1e6)
	case "send":
		if len(os.Args) < 6 {
			usage()
		}
		k := load(os.Args[3])
		from := core.AddressFromPub(k.Public().(ed25519.PublicKey))
		r, err := http.Get(os.Args[2] + "/account/" + from)
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		var a core.Account
		json.NewDecoder(r.Body).Decode(&a)
		t := core.Tx{To: os.Args[4], Amount: units(os.Args[5]), Fee: core.MinFee, Nonce: a.Nonce + 1}
		if len(os.Args) > 6 {
			t.Fee = units(os.Args[6])
		}
		if len(os.Args) > 7 {
			t.Memo = os.Args[7]
		}
		t.Sign(k)
		j, _ := json.Marshal(t)
		r, err = http.Post(os.Args[2]+"/tx", "application/json", bytes.NewReader(j))
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		out, _ := io.ReadAll(r.Body)
		fmt.Println(string(out))
	case "deploy", "call":
		if len(os.Args) < 5 || (os.Args[1] == "call" && len(os.Args) < 6) {
			usage()
		}
		k := load(os.Args[3])
		from := core.AddressFromPub(k.Public().(ed25519.PublicKey))
		var a core.Account
		if err := getJSON(os.Args[2]+"/account/"+from, &a); err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		t := core.Tx{Fee: core.MinFee, Nonce: a.Nonce + 1}
		feeArg := 5
		if os.Args[1] == "deploy" {
			b, err := os.ReadFile(os.Args[4])
			if err != nil {
				fmt.Println(err)
				os.Exit(1)
			}
			t.Data = hexArg(strings.TrimSpace(string(b)))
		} else {
			t.To, t.Data, feeArg = os.Args[4], hexArg(os.Args[5]), 7
			if len(os.Args) > 6 && os.Args[6] != "0" {
				t.Amount = units(os.Args[6])
			}
		}
		if len(os.Args) > feeArg {
			t.Fee = units(os.Args[feeArg])
		}
		t.Sign(k)
		j, _ := json.Marshal(t)
		r, err := http.Post(os.Args[2]+"/tx", "application/json", bytes.NewReader(j))
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		out, _ := io.ReadAll(r.Body)
		fmt.Println(string(out))
	case "read":
		if len(os.Args) < 5 {
			usage()
		}
		j, _ := json.Marshal(map[string]any{"to": os.Args[3], "data": core.HexBytes(hexArg(os.Args[4]))})
		r, err := http.Post(os.Args[2]+"/call", "application/json", bytes.NewReader(j))
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		out, _ := io.ReadAll(r.Body)
		fmt.Println(string(out))
	case "receipt":
		if len(os.Args) < 4 {
			usage()
		}
		r, err := http.Get(os.Args[2] + "/receipt/" + os.Args[3])
		if err != nil {
			fmt.Println(err)
			os.Exit(1)
		}
		out, _ := io.ReadAll(r.Body)
		fmt.Println(string(out))
	default:
		usage()
	}
}

func hexArg(s string) []byte {
	b, err := hex.DecodeString(strings.TrimPrefix(strings.TrimPrefix(s, "0x"), "0X"))
	if err != nil {
		fmt.Println("bad hex:", err)
		os.Exit(1)
	}
	return b
}

func getJSON(url string, v any) error {
	r, err := http.Get(url)
	if err != nil {
		return err
	}
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(v)
}
