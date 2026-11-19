"""Fake env + lifecycle + load smoke test for the Phase 1 forge.

1. Fake env server: /register records the port, /done answers false twice
   then true. The forge must register, then exit 0.
2. 200-commit repo: push 200 commits through the git client, then time a
   shallow clone and 50 concurrent shallow clones.
Run: python3 tests/test_lifecycle_load.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "load-token"
OWNER = "alice"


def run(cmd, cwd=None, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True,
                          text=True, timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state = {"port": None, "done_calls": 0}

    class EnvHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _body(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if self.path == "/register":
                state["port"] = json.loads(raw.decode())["port"]
                self._body({"ok": True})
            else:
                self._body({"message": "Not Found"}, 404)

        def do_GET(self):
            if self.path == "/done":
                state["done_calls"] += 1
                self._body({"done": state["done_calls"] >= 3})
            else:
                self._body({"message": "Not Found"}, 404)

    env_server = ThreadingHTTPServer(("127.0.0.1", 0), EnvHandler)
    env_server.daemon_threads = True
    threading.Thread(target=env_server.serve_forever, daemon=True).start()
    env_port = env_server.server_address[1]

    data_dir = tempfile.mkdtemp(prefix="forge-life-")
    env = dict(os.environ, MAKEFUN_SHOP_TOKEN=TOKEN, FORGE_DATA_DIR=data_dir,
               MAKEFUN_ENV_URL="http://127.0.0.1:%d" % env_port)
    proc = subprocess.run([sys.executable, "main.py"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    print("lifecycle exit code:", proc.returncode)
    print("lifecycle registered port:", state["port"], "done polls:", state["done_calls"])
    ok_life = proc.returncode == 0 and state["port"] and state["done_calls"] >= 3
    print(("PASS" if ok_life else "FAIL") + " lifecycle register/done/exit-0")
    env_server.shutdown()
    if not ok_life:
        print(proc.stdout[-2000:], proc.stderr[-2000:])
        return 1

    # --- load smoke test on a fresh local server (no env) ---
    data_dir2 = tempfile.mkdtemp(prefix="forge-load-")
    env2 = dict(os.environ, MAKEFUN_SHOP_TOKEN=TOKEN, FORGE_DATA_DIR=data_dir2)
    server = subprocess.Popen([sys.executable, "main.py", "--port", "0"],
                              cwd=root, env=env2, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    try:
        port = None
        deadline = time.time() + 20
        while time.time() < deadline:
            line = server.stdout.readline()
            if "listening on port" in line:
                port = int(line.split("port")[1].split(",")[0].strip())
                break
        if not port:
            print("FAIL server did not start"); return 1
        base = "http://127.0.0.1:%d" % port
        import urllib.request
        req = urllib.request.Request(base + "/api/v3/user/repos", data=b'{"name":"big"}', method="POST")
        req.add_header("Authorization", "Bearer " + TOKEN)
        req.add_header("Content-Type", "application/json")
        print("create big repo:", urllib.request.urlopen(req, timeout=10).status)

        work = tempfile.mkdtemp(prefix="forge-big-")
        remote = "http://token:%s@127.0.0.1:%d/%s/big.git" % (TOKEN, port, OWNER)
        run(["git", "clone", "-q", remote, os.path.join(work, "w")])
        w = os.path.join(work, "w")
        run(["git", "-C", w, "config", "user.email", "a@example.invalid"])
        run(["git", "-C", w, "config", "user.name", "Alice"])
        # 200 commits, each changing a 20 KiB mostly-repeated file (delta-friendly).
        t0 = time.time()
        for i in range(200):
            body = ("line %d of revision %d\n" % (i, i)) * 700
            with open(os.path.join(w, "big.txt"), "w") as fh:
                fh.write("revision %d\n" % i + body)
            run(["git", "-C", w, "add", "."])
            run(["git", "-C", w, "commit", "-qm", "rev %d" % i,
                 "--author=Alice <a@example.invalid>", "--date=%d +0000" % (1700000000 + i)],
                env={"GIT_COMMITTER_DATE": "%d +0000" % (1700000000 + i)})
        run(["git", "-C", w, "branch", "-M", "main"])
        rc, out, err = run(["git", "-C", w, "push", "-q", "origin", "main"])
        print("push 200 commits rc=%d in %.1fs" % (rc, time.time() - t0), err.strip()[:200])
        if rc != 0:
            print("FAIL push 200 commits"); return 1
        print("PASS push 200 commits (delta/thin pack from real git)")

        rc, head, _ = run(["git", "-C", w, "rev-parse", "HEAD"])
        t0 = time.time()
        rc, out, err = run(["git", "clone", "-q", "--depth", "1", remote, os.path.join(work, "s1")])
        print("shallow clone of 200-commit repo: rc=%d in %.2fs" % (rc, time.time() - t0))
        rc, head2, _ = run(["git", "-C", os.path.join(work, "s1"), "rev-parse", "HEAD"])
        print(("PASS" if head.strip() == head2.strip() else "FAIL") + " shallow clone tip matches")
        with open(os.path.join(work, "s1", "big.txt")) as fh:
            content = fh.read()
        print(("PASS" if content.startswith("revision 199\n") else "FAIL") + " shallow file bytes match")

        # 50 concurrent shallow clones
        def clone_one(n):
            return run(["git", "clone", "-q", "--depth", "1", remote,
                        os.path.join(work, "c%d" % n)])
        t0 = time.time()
        results = []
        def clone_cap(n):
            results.append(run(["git", "clone", "-q", "--depth", "1", remote,
                                os.path.join(work, "c%d" % n)])[0])
        threads = [threading.Thread(target=clone_cap, args=(n,)) for n in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.time() - t0
        print("50 concurrent shallow clones: %d/50 ok in %.2fs" % (results.count(0), dt))
        print(("PASS" if results.count(0) == 50 else "FAIL") + " 50 concurrent shallow clones")
        return 0 if results.count(0) == 50 else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
