"""ALTAF partner node. Run on your server:  python3 altaf_node.py  (port 8787)
First run creates node_key.json — this TRON address receives the 1,000,000 ALTF allocation. Keep the file safe."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from tronpy.keys import PrivateKey

KEY = os.environ.get("ALTAF_NODE_KEY", "node_key.json")
if not os.path.exists(KEY):
    k = PrivateKey.random()
    json.dump({"private": k.hex(), "address": k.public_key.to_base58check_address()}, open(KEY, "w"))
    os.chmod(KEY, 0o600)
cfg = json.load(open(KEY))
priv = PrivateKey(bytes.fromhex(cfg["private"]))
print("ALTAF node address:", cfg["address"], flush=True)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/altaf-node/health":
            self.send_response(404); self.end_headers(); return
        c = parse_qs(u.query).get("c", [""])[0][:64]
        body = json.dumps({"address": cfg["address"], "sig": priv.sign_msg(c.encode()).hex(), "version": 1}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8787), H).serve_forever()
