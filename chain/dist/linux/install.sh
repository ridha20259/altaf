#!/bin/bash
# ALTAF node installer. Usage on a Linux server (Ubuntu/Debian, x86_64):
#   curl -fsSL https://altafnet.187-126-9-68.sslip.io/install.sh | sudo bash
# Options (env): ALTAF_PORT=8545  ALTAF_PUBLIC_URL=http://your-ip:8545
set -euo pipefail
SEED=https://altafnet.187-126-9-68.sslip.io
PORT=${ALTAF_PORT:-8545}
die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || die "only Linux x86_64 is supported"
command -v systemctl >/dev/null || die "systemd is required"
command -v curl >/dev/null || die "curl is required"
if [ -z "${ALTAF_PUBLIC_URL:-}" ]; then
  IP=$(curl -fsS4 --max-time 10 https://api.ipify.org || curl -fsS4 --max-time 10 https://ifconfig.me) || die "cannot detect public IP; set ALTAF_PUBLIC_URL"
  ALTAF_PUBLIC_URL=http://$IP:$PORT
fi
echo "== ALTAF node: installing (public URL $ALTAF_PUBLIC_URL)"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$SEED/altafd-linux-amd64" -o "$tmp/altafd"
curl -fsSL "$SEED/altafd-linux-amd64.sha256" -o "$tmp/sum"
(cd "$tmp" && echo "$(cut -d' ' -f1 sum)  altafd" | sha256sum -c --quiet -) || die "checksum mismatch"
id altafchain >/dev/null 2>&1 || useradd --system --home-dir /var/lib/altafchain --shell /usr/sbin/nologin altafchain
install -d -o altafchain -g altafchain /var/lib/altafchain
systemctl stop altafd 2>/dev/null || true
install -m 755 "$tmp/altafd" /usr/local/bin/altafd
cat > /etc/systemd/system/altafd.service <<UNIT
[Unit]
Description=ALTAF network node
After=network-online.target
Wants=network-online.target
[Service]
User=altafchain
ExecStart=/usr/local/bin/altafd -network mainnet -data /var/lib/altafchain -listen 0.0.0.0:$PORT -public $ALTAF_PUBLIC_URL -peers $SEED
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now altafd >/dev/null
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$PORT/tcp" >/dev/null; fi
echo -n "== waiting for sync"
want=$(curl -fsS "$SEED/status" | grep -o '"height":[0-9]*' | cut -d: -f2)
for i in $(seq 60); do
  h=$(curl -fsS "http://127.0.0.1:$PORT/status" 2>/dev/null | grep -o '"height":[0-9]*' | cut -d: -f2 || true)
  [ -n "$h" ] && [ "$h" -ge "$want" ] && break
  echo -n .; sleep 2
done
echo
echo "== ALTAF node is running. Height: ${h:-?} / network: $want"
echo "   Status:  curl http://127.0.0.1:$PORT/status"
echo "   Logs:    journalctl -u altafd -f"
echo "   Make sure TCP port $PORT is open in your provider's firewall so other nodes can reach you."
