"""📎 File through a real install's proxy (Caddy / nginx in front of status_server).

    upload_check.py <base-url> <password> [--insecure] [--connect HOST:PORT]

Login → POST /api/upload without and with the session → the file's path. Prints
PATH=<path> SHA=<sha256 of the bytes sent>; the caller checks the file on the server.
--connect sends every connection to HOST:PORT (a name that only resolves in the test).
"""
import hashlib, http.cookiejar, json, os, socket, ssl, sys, urllib.parse, urllib.request
base, pw = sys.argv[1].rstrip("/"), sys.argv[2]
if "--connect" in sys.argv:
    _host, _port = sys.argv[sys.argv.index("--connect") + 1].rsplit(":", 1)
    _orig = socket.create_connection
    socket.create_connection = lambda addr, *a, **k: _orig((_host, int(_port)), *a, **k)
ctx = ssl._create_unverified_context() if "--insecure" in sys.argv else None
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                 urllib.request.HTTPSHandler(context=ctx))
op.addheaders = [("User-Agent", "Mozilla/5.0 agentdeck-upload-check")]
fails = 0
def check(name, ok, info=""):
    global fails
    print(("PASS  " if ok else "FAIL  ") + name + (f"  — {info}" if info else ""), flush=True)
    fails += 0 if ok else 1
def post(path, body, opener):
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        r = opener.open(req, timeout=60); return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
anon = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
anon.addheaders = op.addheaders
st, _ = post("/api/upload?name=x.txt", b"x", anon)
check("upload without login refused", st in (302, 401), f"status {st}")
op.open(urllib.request.Request(base + "/login", data=urllib.parse.urlencode({"pass": pw}).encode(),
                               method="POST"), timeout=30)
check("logged in", any(c.name == "agents_session" for c in jar))
data = os.urandom(3 * 1024 * 1024 + 11)
st, body = post("/api/upload?name=" + urllib.parse.quote("отчёт за май.pdf"), data, op)
d = {}
try: d = json.loads(body)
except Exception: pass
check("upload 3 MB through the proxy", st == 200 and bool(d.get("path")), f"status {st} {body[:120]!r}")
check("name kept, made safe", d.get("name") == "отчёт_за_май.pdf", d.get("name"))
check("size", d.get("size") == len(data), d.get("size"))
check("private folder, not a link", "/.sessions/uploads/" in d.get("path", "") and "http" not in d.get("path", ""), d.get("path"))
print(f"PATH={d.get('path','')} SHA={hashlib.sha256(data).hexdigest()}")
sys.exit(1 if fails else 0)
