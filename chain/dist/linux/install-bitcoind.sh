#!/bin/bash
# Installs a pruned Bitcoin Core node for the ALTAF Bitcoin mining pool.
# Needs: Linux x86_64, systemd, >= 2 CPU, >= 4 GB RAM, >= 30 GB free disk. First sync takes 1-3 days.
#   curl -fsSL https://altafnet.187-126-9-68.sslip.io/install-bitcoind.sh | sudo bash
set -euo pipefail
VER=${BTC_VERSION:-28.1}
PRUNE=${BTC_PRUNE_MB:-10000}
die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || die "only Linux x86_64 is supported"
[ "$(nproc)" -ge 2 ] || echo "WARNING: 1 CPU detected; syncing will be very slow and may slow other services"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
F=bitcoin-$VER-x86_64-linux-gnu.tar.gz
cd "$tmp"
curl -fsSLO "https://bitcoincore.org/bin/bitcoin-core-$VER/$F"
curl -fsSLO "https://bitcoincore.org/bin/bitcoin-core-$VER/SHA256SUMS"
grep " $F\$" SHA256SUMS | sha256sum -c --quiet - || die "checksum mismatch"
tar xzf "$F"
install -m 755 bitcoin-$VER/bin/bitcoind bitcoin-$VER/bin/bitcoin-cli /usr/local/bin/
id bitcoin >/dev/null 2>&1 || useradd --system --home-dir /var/lib/bitcoind --shell /usr/sbin/nologin bitcoin
install -d -o bitcoin -g bitcoin -m 750 /var/lib/bitcoind
PASS=$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9)
if [ ! -f /var/lib/bitcoind/bitcoin.conf ]; then
  cat > /var/lib/bitcoind/bitcoin.conf <<CONF
server=1
prune=$PRUNE
dbcache=512
maxconnections=24
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
rpcuser=altafpool
rpcpassword=$PASS
CONF
  chown bitcoin:bitcoin /var/lib/bitcoind/bitcoin.conf; chmod 600 /var/lib/bitcoind/bitcoin.conf
  umask 077; printf 'BTC_RPC_URL=http://127.0.0.1:8332\nBTC_RPC_USER=altafpool\nBTC_RPC_PASS=%s\n' "$PASS" > /etc/altaf-btcpool.env
fi
cat > /etc/systemd/system/bitcoind.service <<UNIT
[Unit]
Description=Bitcoin Core (pruned) for the ALTAF mining pool
After=network-online.target
[Service]
User=bitcoin
ExecStart=/usr/local/bin/bitcoind -datadir=/var/lib/bitcoind -conf=/var/lib/bitcoind/bitcoin.conf
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now bitcoind >/dev/null
echo "== Bitcoin Core $VER installed (pruned to ${PRUNE} MB). RPC settings: /etc/altaf-btcpool.env"
echo "   Progress: sudo -u bitcoin bitcoin-cli -datadir=/var/lib/bitcoind getblockchaininfo"
echo "   The ALTAF pool (altaf-btcpool) starts accepting miners by itself once the node is synced."
