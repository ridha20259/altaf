#!/bin/bash
# Local 2-node regtest: node1 + CPU miner, node2 + SHA miner.
B=$(cd "$(dirname "$0")" && pwd)/bin
D=${D:-/tmp/rt}
[ -f $D/pids ] && kill $(cat $D/pids) 2>/dev/null
rm -rf $D && mkdir -p $D && cd $D
$B/altafd -network regtest -data n1 -listen 127.0.0.1:18001 -public http://127.0.0.1:18001 -peers http://127.0.0.1:18002 > n1.log 2>&1 & echo $! >> pids
$B/altafd -network regtest -data n2 -listen 127.0.0.1:18002 -public http://127.0.0.1:18002 -peers http://127.0.0.1:18001 > n2.log 2>&1 & echo $! >> pids
sleep 1
$B/altaf-miner -network regtest -node http://127.0.0.1:18001 -address $1 -algo cpu -threads 2 > m1.log 2>&1 & echo $! >> pids
$B/altaf-miner -network regtest -node http://127.0.0.1:18002 -address $2 -algo sha -threads 1 > m2.log 2>&1 & echo $! >> pids
