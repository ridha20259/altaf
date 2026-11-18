#!/bin/bash
# Installs a pruned Bitcoin Core node + the ALTAF Bitcoin mining pool (altaf-btcpool, stratum port 3334).
# The pool waits until the node is synced, then accepts miners (username = their Bitcoin address)
# and pays them automatically from the pool wallet after each block matures.
# Needs: Linux x86_64, systemd, >= 2 CPU, >= 4 GB RAM, >= 30 GB free disk. First sync takes 1-3 days.
#   curl -fsSL https://raw.githubusercontent.com/ridha20259/altaf/main/chain/dist/linux/install-bitcoind.sh | sudo bash
set -euo pipefail
VER=${BTC_VERSION:-28.1}
PRUNE=${BTC_PRUNE_MB:-10000}
POOL_URL=${POOL_URL:-https://github.com/ridha20259/altaf/releases/download/btcpool-v2/altaf-btcpool-linux-amd64}
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
# ---- pool wallet + altaf-btcpool ----
. /etc/altaf-btcpool.env
CLI="/usr/local/bin/bitcoin-cli -rpcuser=$BTC_RPC_USER -rpcpassword=$BTC_RPC_PASS"
for _ in $(seq 60); do $CLI getblockchaininfo >/dev/null 2>&1 && break; sleep 3; done
$CLI getblockchaininfo >/dev/null || die "bitcoind did not start (journalctl -u bitcoind)"
if ! $CLI listwallets | grep -q '"altafpool"'; then
  $CLI loadwallet altafpool true >/dev/null 2>&1 || $CLI -named createwallet wallet_name=altafpool load_on_startup=true >/dev/null
fi
if ! grep -q '^BTC_POOL_ADDRESS=' /etc/altaf-btcpool.env; then
  ADDR=$($CLI -rpcwallet=altafpool getnewaddress pool bech32)
  printf 'BTC_WALLET=altafpool\nBTC_POOL_ADDRESS=%s\n' "$ADDR" >> /etc/altaf-btcpool.env
fi
install -d -m 700 /root/altaf-btcpool-backup
$CLI -rpcwallet=altafpool backupwallet /var/lib/bitcoind/altafpool-wallet.bak
mv /var/lib/bitcoind/altafpool-wallet.bak /root/altaf-btcpool-backup/altafpool-wallet.$(date +%s).bak
cd "$tmp"
curl -fsSL "$POOL_URL" -o altaf-btcpool
curl -fsSL "$POOL_URL.sha256" -o altaf-btcpool.sha256
[ "$(sha256sum altaf-btcpool | cut -d' ' -f1)" = "$(cut -d' ' -f1 altaf-btcpool.sha256)" ] || die "altaf-btcpool checksum mismatch"
install -m 755 altaf-btcpool /usr/local/bin/altaf-btcpool
install -d -o bitcoin -g bitcoin -m 750 /var/lib/altaf-btcpool
cat > /etc/systemd/system/altaf-btcpool.service <<UNIT
[Unit]
Description=ALTAF Bitcoin mining pool (stratum :3334, automatic payouts)
After=bitcoind.service
Wants=bitcoind.service
[Service]
User=bitcoin
EnvironmentFile=/etc/altaf-btcpool.env
ExecStart=/usr/local/bin/altaf-btcpool -listen 0.0.0.0:3334 -data /var/lib/altaf-btcpool
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable altaf-btcpool >/dev/null
systemctl restart altaf-btcpool
. /etc/altaf-btcpool.env
echo "== Bitcoin Core $VER installed (pruned to ${PRUNE} MB). RPC settings: /etc/altaf-btcpool.env"
echo "   Progress: sudo -u bitcoin bitcoin-cli -datadir=/var/lib/bitcoind getblockchaininfo"
echo "== ALTAF pool installed: stratum+tcp://<this server>:3334, username = miner's Bitcoin address, password = x"
echo "   Pool wallet address (block rewards): $BTC_POOL_ADDRESS"
echo "   Wallet backup: /root/altaf-btcpool-backup (copy it somewhere safe)"
echo "   The pool starts accepting miners by itself once the node is synced; payouts are automatic"
echo "   (after 101 confirmations, minimum 10000 sat; smaller amounts carry over). Logs: journalctl -u altaf-btcpool"
