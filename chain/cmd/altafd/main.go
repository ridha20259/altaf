package main

import (
	"flag"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"altafchain/core"
	"altafchain/node"
)

func main() {
	network := flag.String("network", "mainnet", "mainnet | testnet | regtest")
	data := flag.String("data", "./altaf-data", "data directory")
	listen := flag.String("listen", "127.0.0.1:8545", "HTTP listen address (RPC + P2P)")
	self := flag.String("public", "", "public URL of this node for peers, e.g. http://1.2.3.4:8545")
	peers := flag.String("peers", "", "comma-separated peer URLs")
	flag.Parse()
	core.UseNetwork(*network)
	p, ok := core.Networks[*network]
	if !ok {
		log.Fatalf("unknown network %q", *network)
	}
	os.MkdirAll(*data, 0700)
	c, err := core.OpenChain(filepath.Join(*data, *network+".db"), p)
	if err != nil {
		log.Fatal(err)
	}
	n := node.New(c, *self, strings.Split(*peers, ","))
	go n.SyncLoop()
	h, ht := c.Tip()
	log.Printf("ALTAF %s node: height %d tip %s, listening on %s", *network, ht, h, *listen)
	log.Fatal(http.ListenAndServe(*listen, n.Handler()))
}
