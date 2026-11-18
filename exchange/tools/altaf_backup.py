"""Daily encrypted ALTAF backup -> Backblaze B2 (prefix altaf/). Run as root from cron."""
import base64, datetime, hashlib, json, os, sqlite3, subprocess, sys, tarfile, tempfile, time, urllib.parse, urllib.request

PREFIX = "altaf/"
KEEP = int(os.environ.get("ALTAF_BACKUP_KEEP", "30"))
KEYFILE = os.environ.get("ALTAF_BACKUP_KEYFILE", "/root/altaf-backup.key")
DB = "/opt/altaf/data/altaf.db"


def _pg_db():
    """Database name when the exchange runs on PostgreSQL (ALTAF_DB in /etc/altaf.env)."""
    try:
        for l in open("/etc/altaf.env"):
            if l.startswith("ALTAF_DB=postgresql"):
                return l.strip().rsplit("/", 1)[-1]
    except OSError:
        pass
    return ""


PG_DB = _pg_db()
PATHS = ["/var/lib/altafchain", "/var/lib/altaf-signer", "/etc/altaf.env", "/etc/altaf-signer.env",
         "/root/altaf-secrets", "/opt/altaf/app", "/opt/altaf/static", "/opt/altaf/token", "/opt/altaf/node",
         "/opt/altaf/tools", "/opt/altaf/requirements.txt", "/opt/altaf/NOTES.md", "/opt/altafchain/bin"]
PATHS += [f"/etc/systemd/system/{n}" for n in os.listdir("/etc/systemd/system") if n.startswith("altaf")]


def skip(ti):
    n = ti.name
    if "__pycache__" in n or n.endswith(".tgz") or "/app.bak" in n or "/prebridge-" in n:
        return None
    return ti


def call(url, body=None, auth="", headers=None, data=None):
    h = {"Authorization": auth, **(headers or {})}
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def b2_auth():
    basic = base64.b64encode((os.environ["B2_KEY_ID"] + ":" + os.environ["B2_APP_KEY"]).encode()).decode()
    d = json.loads(call("https://api.backblazeb2.com/b2api/v2/b2_authorize_account", auth="Basic " + basic))
    return {"api": d["apiUrl"], "dl": d["downloadUrl"], "tok": d["authorizationToken"],
            "bucket": d["allowed"]["bucketId"], "name": d["allowed"]["bucketName"]}


def b2_list(a):
    out, start = [], None
    while True:
        q = {"bucketId": a["bucket"], "prefix": PREFIX, "maxFileCount": 1000, **({"startFileName": start} if start else {})}
        d = json.loads(call(a["api"] + "/b2api/v2/b2_list_file_names", q, a["tok"]))
        out += d.get("files", [])
        start = d.get("nextFileName")
        if not start:
            return out


def b2_put(a, name, data):
    u = json.loads(call(a["api"] + "/b2api/v2/b2_get_upload_url", {"bucketId": a["bucket"]}, a["tok"]))
    call(u["uploadUrl"], auth=u["authorizationToken"], data=data,
         headers={"X-Bz-File-Name": urllib.parse.quote(name), "Content-Type": "application/octet-stream",
                  "X-Bz-Content-Sha1": hashlib.sha1(data).hexdigest()})


def b2_get(a, name):
    return call(a["dl"] + "/file/" + a["name"] + "/" + urllib.parse.quote(name), auth=a["tok"])


def build():
    with tempfile.TemporaryDirectory() as tmp:
        tgz = os.path.join(tmp, "b.tgz")
        if PG_DB:
            snap, arc = os.path.join(tmp, "altaf.pgdump"), "opt/altaf/data/altaf.pgdump"
            with open(snap, "wb") as f:
                subprocess.run(["runuser", "-u", "postgres", "--", "pg_dump", "-Fc", PG_DB], check=True, stdout=f)
            rows = int(subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-Atd", PG_DB, "-c",
                                       "SELECT COALESCE(SUM(n_live_tup),0) FROM pg_stat_user_tables"],
                                      check=True, capture_output=True, text=True).stdout.strip() or 0)
        else:
            snap, arc = os.path.join(tmp, "altaf.db"), "opt/altaf/data/altaf.db"
            src, dst = sqlite3.connect(DB), sqlite3.connect(snap)
            src.backup(dst)
            rows = sum(dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                       for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            src.close(); dst.close()
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(snap, arc)
            for p in PATHS:
                if os.path.exists(p):
                    tf.add(p, p.lstrip("/"), filter=skip)
        enc = subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--symmetric",
                              "--cipher-algo", "AES256", "--passphrase-file", KEYFILE, "-o", "-", tgz],
                             check=True, capture_output=True).stdout
        return enc, rows


def main():
    t0 = time.time()
    data, rows = build()
    name = PREFIX + datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S") + ".tgz.gpg"
    a = b2_auth()
    b2_put(a, name, data)
    files = sorted(f["fileName"] for f in b2_list(a))
    old = [f for f in b2_list(a) if f["fileName"] in files[:-KEEP]] if len(files) > KEEP else []
    for f in old:
        call(a["api"] + "/b2api/v2/b2_delete_file_version", {"fileName": f["fileName"], "fileId": f["fileId"]}, a["tok"])
    print(json.dumps({"ok": name, "bytes": len(data), "db_rows": rows, "kept": min(len(files), KEEP),
                      "deleted": len(old), "s": round(time.time() - t0, 1)}))


def restore(name, out):
    a = b2_auth()
    if name == "latest":
        name = sorted(f["fileName"] for f in b2_list(a))[-1]
    with open(out, "wb") as f:
        f.write(b2_get(a, name))
    print("downloaded", name, "->", out, "| decrypt: gpg -d --passphrase-file", KEYFILE, out, "| tar xz")


if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for f in b2_list(b2_auth()):
            print(f["fileName"], f.get("contentLength"))
    elif sys.argv[1:2] == ["restore"]:
        restore(sys.argv[2] if len(sys.argv) > 2 else "latest", sys.argv[3] if len(sys.argv) > 3 else "/root/altaf-restore.tgz.gpg")
    else:
        try:
            main()
        except Exception as e:
            print(json.dumps({"err": str(e)[:300]}))
            sys.exit(1)
