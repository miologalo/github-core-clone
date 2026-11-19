"""Local end-to-end test for the Phase 1 forge.

Starts main.py on a scratch data dir (no MAKEFUN_ENV_URL, so it serves
locally), then drives it with the system git client:
  1. /healthz + auth (401 without token, 200 with)
  2. info/refs 404 for a missing repo
  3. create a repo over REST, clone the empty repo
  4. push a commit, clone back, verify HEAD + file bytes match
  5. second commit + push, fetch/clone sees the new tip
  6. shallow clone (depth 1) carries only the tip
  7. non-fast-forward push is refused and the ref does not move
  8. tag push + clone sees the tag; branch delete works
Prints PASS/FAIL per check and exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TOKEN = "test-token-123"
OWNER = "alice"
REPO = "hello"
BASE = None  # set once the server is up


def http(method, path, body=None, token=TOKEN, headers=None):
    req = urllib.request.Request(BASE + path, method=method)
    if token is not None:
        req.add_header("Authorization", "Bearer %s" % token)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def run(cmd, cwd=None, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True,
                          text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail else ""))


def main():
    global BASE
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = tempfile.mkdtemp(prefix="forge-test-")
    work = tempfile.mkdtemp(prefix="forge-work-")
    env = dict(os.environ, MAKEFUN_SHOP_TOKEN=TOKEN,
               FORGE_DATA_DIR=data_dir)
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "0"], cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        port = None
        deadline = time.time() + 20
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "listening on port" in line:
                port = int(line.split("port")[1].split(",")[0].strip())
                break
        if not port:
            check("server starts", False, "no listening line")
            return 1
        BASE = "http://127.0.0.1:%d" % port

        status, _ = http("GET", "/healthz", token=None)
        check("healthz no auth", status == 200, "HTTP %d" % status)
        status, _ = http("GET", "/api/v3/", token=None)
        check("unauthenticated REST is 401", status == 401, "HTTP %d" % status)
        status, _ = http("GET", "/api/v3/")
        check("authenticated REST root 200", status == 200, "HTTP %d" % status)
        status, _ = http("GET", "/%s/%s.git/info/refs?service=git-upload-pack"
                         % (OWNER, "nope"))
        check("missing repo info/refs 404", status == 404, "HTTP %d" % status)

        status, body = http("POST", "/api/v3/user/repos",
                            {"name": REPO, "description": "test", "private": False})
        check("create repo 201", status == 201, "HTTP %d %s" % (status, body[:120]))
        status, body = http("POST", "/api/v3/user/repos", {"name": REPO})
        check("duplicate repo 422", status == 422, "HTTP %d" % status)
        status, _ = http("GET", "/api/v3/repos/%s/%s" % (OWNER, REPO))
        check("own repo GET 200 (binds owner)", status == 200, "HTTP %d" % status)
        status, _ = http("GET", "/api/v3/repos/bob/%s" % REPO)
        check("same name under other owner 404", status == 404, "HTTP %d" % status)

        remote = "http://token:%s@%s/%s/%s.git" % (TOKEN, BASE.split("://", 1)[1], OWNER, REPO)
        empty = os.path.join(work, "empty")
        rc, out, err = run(["git", "clone", remote, empty])
        check("clone empty repo", rc == 0, (err.strip() or out.strip())[:200])

        # First commit + push.
        rc, out, err = run(["git", "-C", empty, "config", "user.email", "a@example.invalid"])
        run(["git", "-C", empty, "config", "user.name", "Alice"])
        run(["git", "-C", empty, "config", "commit.gpgsign", "false"])
        with open(os.path.join(empty, "hello.txt"), "w") as fh:
            fh.write("hello forge\n")
        run(["git", "-C", empty, "add", "."])
        rc, out, err = run(["git", "-C", empty, "commit", "-m", "first",
                            "--author=Alice <a@example.invalid>",
                            "--date=1700000000 +0000"],
                           env={"GIT_COMMITTER_DATE": "1700000000 +0000"})
        check("local commit", rc == 0, err.strip()[:200])
        rc, out, err = run(["git", "-C", empty, "branch", "-M", "main"])
        rc, out, err = run(["git", "-C", empty, "push", "origin", "main"])
        check("push first commit", rc == 0, (err.strip() or out.strip())[:300])
        rc, head1, _ = run(["git", "-C", empty, "rev-parse", "HEAD"])

        clone2 = os.path.join(work, "clone2")
        rc, out, err = run(["git", "clone", remote, clone2])
        check("clone back", rc == 0, (err.strip() or out.strip())[:200])
        rc, head2, _ = run(["git", "-C", clone2, "rev-parse", "HEAD"])
        check("clone HEAD matches pushed HEAD",
              head1.strip() == head2.strip() and head1.strip() != "",
              "%s vs %s" % (head1.strip(), head2.strip()))
        with open(os.path.join(clone2, "hello.txt")) as fh:
            content = fh.read()
        check("cloned file bytes match", content == "hello forge\n", repr(content))

        # Second commit, push, clone sees new tip.
        with open(os.path.join(empty, "second.txt"), "w") as fh:
            fh.write("second file\n")
        run(["git", "-C", empty, "add", "."])
        run(["git", "-C", empty, "commit", "-m", "second",
             "--author=Alice <a@example.invalid>", "--date=1700000100 +0000"],
            env={"GIT_COMMITTER_DATE": "1700000100 +0000"})
        rc, out, err = run(["git", "-C", empty, "push", "origin", "main"])
        check("push second commit", rc == 0, (err.strip() or out.strip())[:200])
        rc, head3, _ = run(["git", "-C", empty, "rev-parse", "HEAD"])
        clone3 = os.path.join(work, "clone3")
        rc, out, err = run(["git", "clone", remote, clone3])
        rc, head4, _ = run(["git", "-C", clone3, "rev-parse", "HEAD"])
        check("second clone sees new tip", head3.strip() == head4.strip(),
              "%s vs %s" % (head3.strip(), head4.strip()))

        # Shallow clone depth 1: only the tip commit.
        shallow = os.path.join(work, "shallow")
        rc, out, err = run(["git", "clone", "--depth", "1", remote, shallow])
        rc, count, _ = run(["git", "-C", shallow, "rev-list", "--count", "HEAD"])
        check("shallow clone depth 1 has 1 commit",
              rc == 0 and count.strip() == "1", "count=%s err=%s" % (count.strip(), err.strip()[:120]))
        rc, head5, _ = run(["git", "-C", shallow, "rev-parse", "HEAD"])
        check("shallow tip matches", head5.strip() == head3.strip())

        # Non-fast-forward: reset local main to the first commit, commit a
        # divergent change, push must be refused and the ref must not move.
        run(["git", "-C", empty, "reset", "--hard", head1.strip()])
        with open(os.path.join(empty, "divergent.txt"), "w") as fh:
            fh.write("divergent\n")
        run(["git", "-C", empty, "add", "."])
        run(["git", "-C", empty, "commit", "-m", "divergent",
             "--author=Alice <a@example.invalid>", "--date=1700000200 +0000"],
            env={"GIT_COMMITTER_DATE": "1700000200 +0000"})
        rc, out, err = run(["git", "-C", empty, "push", "origin", "main"])
        refused = rc != 0 and "non-fast-forward" in (err + out)
        check("non-fast-forward push refused", refused,
              "rc=%d %s" % (rc, (err.strip() or out.strip())[:200]))
        status, body = http("GET", "/api/v3/repos/%s/%s" % (OWNER, REPO))
        clone4 = os.path.join(work, "clone4")
        run(["git", "clone", remote, clone4])
        rc, head6, _ = run(["git", "-C", clone4, "rev-parse", "HEAD"])
        check("ref unchanged after refused push", head6.strip() == head3.strip(),
              "%s vs %s" % (head6.strip(), head3.strip()))

        # Annotated tag push + clone sees it.
        run(["git", "-C", empty, "checkout", "main"])
        run(["git", "-C", empty, "reset", "--hard", head3.strip()])
        run(["git", "-C", empty, "tag", "-a", "v1", "-m", "version one"])
        rc, out, err = run(["git", "-C", empty, "push", "origin", "v1"])
        check("push annotated tag", rc == 0, (err.strip() or out.strip())[:200])
        rc, out, err = run(["git", "-C", clone4, "fetch", "--tags", "origin"])
        rc, tags, _ = run(["git", "-C", clone4, "tag", "-l"])
        check("tag visible after fetch", "v1" in tags.split(), tags.strip())

        # Branch create + delete over the wire.
        rc, out, err = run(["git", "-C", empty, "push", "origin", "main:feature"])
        check("push new branch", rc == 0, (err.strip() or out.strip())[:200])
        rc, out, err = run(["git", "-C", empty, "push", "origin", ":feature"])
        check("delete branch", rc == 0, (err.strip() or out.strip())[:200])
        rc, branches, _ = run(["git", "-C", clone4, "ls-remote", "--heads", remote])
        check("deleted branch gone from ls-remote",
              "feature" not in branches, branches.strip()[:200])

        # Server still healthy after all of it.
        status, _ = http("GET", "/healthz", token=None)
        check("server healthy at end", status == 200, "HTTP %d" % status)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [name for name, ok, _ in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
