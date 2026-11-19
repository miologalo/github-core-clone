"""Entry point: bind an ephemeral port, register with the suite env,
long-poll /done, exit 0. Local dev (no MAKEFUN_ENV_URL): serve forever.

Never binds 8080: the port comes from the kernel (listen on 0) or from an
explicit --port flag used only for local testing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from server import serve


def _http_json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _register(env_url, port):
    delay = 0.25
    while True:
        try:
            status, _body = _http_json(env_url.rstrip("/") + "/register",
                                       {"port": port})
            if status in (200, 409):
                return
            raise RuntimeError("register answered HTTP %d" % status)
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 409):
                return
            if exc.code in (400, 405):
                raise
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


def _wait_done(env_url):
    delay = 0.5
    while True:
        try:
            _status, body = _http_json(env_url.rstrip("/") + "/done")
            if body.get("done") is True:
                return
            delay = 0.5
        except (urllib.error.URLError, OSError, ValueError):
            pass
        except urllib.error.HTTPError:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="github-core-clone forge")
    parser.add_argument("--port", type=int, default=0,
                        help="port to bind (0 = ephemeral; local testing only)")
    parser.add_argument("--data-dir", default=None,
                        help="store directory (default ./data or FORGE_DATA_DIR)")
    args = parser.parse_args(argv)

    data_dir = args.data_dir or os.environ.get("FORGE_DATA_DIR") or "./data"
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        data_dir = "/tmp/github-core-clone-data"
        os.makedirs(data_dir, exist_ok=True)

    server, _forge = serve(data_dir, args.port)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("[forge] listening on port %d, data in %s" % (port, data_dir),
          flush=True)

    env_url = os.environ.get("MAKEFUN_ENV_URL") or ""
    if not env_url:
        print("[forge] MAKEFUN_ENV_URL unset: serving locally, Ctrl-C to stop",
              flush=True)
        stop = threading.Event()

        def _sig(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)
        while not stop.wait(1):
            pass
        server.shutdown()
        return 0

    _register(env_url, port)
    print("[forge] registered port %d with %s" % (port, env_url), flush=True)
    _wait_done(env_url)
    print("[forge] done; exiting 0", flush=True)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
