"""HTTP server for the forge: routing, auth, /healthz, git smart HTTP,
and the minimal repository REST surface Phase 1 needs (create/list/get a
repository — a push cannot invent one, so the suite creates repos here).

Full REST v3 / GraphQL / MCP arrive in Phase 2; anything not listed here
is a JSON 404.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from smart_http import (GitError, advertisement_v0, advertisement_v2,
                        handle_receive_pack, handle_upload_pack_request)
from store import DEFAULT_BRANCH, Store

WALLET = "38AmVqLHGNBwUTbmxKs265pc3XrtLHksuBYk9nmHjn13"

_counter = itertools.count(1)


def repo_view(repo, base_url):
    owner = repo.owner or ""
    full_name = "%s/%s" % (owner, repo.name) if owner else repo.name
    return {
        "id": repo.rid,
        "name": repo.name,
        "full_name": full_name,
        "owner": {"login": owner, "id": 1000, "type": "User"},
        "private": repo.private,
        "description": repo.description,
        "default_branch": DEFAULT_BRANCH,
        "fork": False,
        "html_url": "%s/%s" % (base_url, full_name),
        "clone_url": "%s/%s.git" % (base_url, full_name),
        "git_url": "git://%s/%s.git" % (urlparse(base_url).netloc, full_name),
        "ssh_url": "git@github.com:%s.git" % full_name,
    }


class Forge:
    """Owns the store and the bearer token; the handler delegates here."""

    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self.token = os.environ.get("MAKEFUN_SHOP_TOKEN") or ""
        self._write_lock = threading.Lock()

    def authorized(self, headers):
        if not self.token:
            return True        # local dev: no token configured, accept any
        auth = headers.get("Authorization") or ""
        if auth == "Bearer %s" % self.token:
            return True
        # A real git client can only carry the token as Basic auth (token
        # embedded in the remote URL); accept it as the same credential.
        if auth.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            except Exception:
                return False
            user, _, password = decoded.partition(":")
            return user == self.token or password == self.token
        return False


def make_handler(forge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "github-core-clone/1.0"

        # -- plumbing ------------------------------------------------------
        def log_message(self, *args):      # keep the trial log clean
            pass

        def _read_body(self):
            length = self.headers.get("Content-Length")
            if not length:
                return b""
            try:
                n = int(length)
            except ValueError:
                return b""
            return self.rfile.read(n) if n > 0 else b""

        def _base_url(self):
            host = self.headers.get("Host") or "127.0.0.1"
            return "http://%s" % host

        def _send_json(self, status, payload, extra=None):
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", "req-%d" % next(_counter))
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", "req-%d" % next(_counter))
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self):
            self._send_json(404, {"message": "Not Found"})

        def _unauthorized(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"message": "Unauthorized"}, sort_keys=True).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GitHub-Request-Id", "req-%d" % next(_counter))
            # The challenge header is what makes a git client retry with the
            # credentials embedded in the remote URL; without it curl gives up.
            self.send_header("WWW-Authenticate",
                             'Basic realm="forge", Bearer realm="forge"')
            self.end_headers()
            self.wfile.write(body)

        def _bad_json(self):
            self._send_json(400, {"message": "Malformed JSON"})

        def _json_body(self):
            raw = self._read_body()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            if not isinstance(parsed, dict):
                return None
            return parsed

        def _require_auth(self):
            if not forge.authorized(self.headers):
                self._unauthorized()
                return False
            return True

        # -- routing -------------------------------------------------------
        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def do_PUT(self):
            self._route()

        def do_PATCH(self):
            self._route()

        def do_DELETE(self):
            self._route()

        def _route(self):
            try:
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                method = self.command
                if path == "/healthz" and method == "GET":
                    self._send_json(200, {"ok": True})
                    return
                git = re.match(r"^/([^/]+)/([^/]+)\.git(/.*)?$", path)
                if git:
                    self._route_git(git, parsed, method)
                    return
                if path == "/api/v3/" and method == "GET":
                    if not self._require_auth():
                        return
                    self._send_json(200, {
                        "current_user_url": self._base_url() + "/api/v3/user",
                        "repository_url": self._base_url() + "/api/v3/repos/{owner}/{repo}",
                    }, {"X-OAuth-Scopes": "repo, read:org"})
                    return
                if path == "/api/v3/user/repos":
                    if not self._require_auth():
                        return
                    if method == "GET":
                        repos = [repo_view(r, self._base_url())
                                 for r in forge.store.list_repos()]
                        self._send_json(200, repos)
                        return
                    if method == "POST":
                        body = self._json_body()
                        if body is None:
                            self._bad_json()
                            return
                        name = body.get("name")
                        if not isinstance(name, str) or not name:
                            self._send_json(422, {"message": "Repository name is required"})
                            return
                        repo = forge.store.create_repo(
                            name, body.get("description") or "",
                            bool(body.get("private", False)))
                        if repo is None:
                            self._send_json(422, {"message": "Repository already exists"})
                            return
                        self._send_json(201, repo_view(repo, self._base_url()))
                        return
                    self._not_found()
                    return
                m = re.match(r"^/api/v3/repos/([^/]+)/([^/]+)$", path)
                if m and method == "GET":
                    if not self._require_auth():
                        return
                    repo = forge.store.get_repo(m.group(1), m.group(2))
                    if repo is None:
                        self._not_found()
                        return
                    self._send_json(200, repo_view(repo, self._base_url()))
                    return
                self._not_found()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:       # never die on one bad request
                try:
                    self._send_json(500, {"message": "Internal error: %s" % exc})
                except Exception:
                    pass

        # -- git smart HTTP ------------------------------------------------
        def _route_git(self, git, parsed, method):
            owner, repo_name, suffix = git.group(1), git.group(2), git.group(3) or ""
            if not self._require_auth():
                return
            store = forge.store
            query = parsed.query
            if method == "GET" and suffix == "/info/refs":
                service = None
                for part in query.split("&"):
                    if part.startswith("service="):
                        service = part[len("service="):]
                if service not in ("git-upload-pack", "git-receive-pack"):
                    self._not_found()
                    return
                if store.get_repo(owner, repo_name) is None:
                    self._not_found()
                    return
                version2 = (service == "git-upload-pack"
                            and self.headers.get("Git-Protocol") == "version=2")
                if version2:
                    body = advertisement_v2()
                else:
                    body = advertisement_v0(store, repo_name, service)
                ctype = "application/x-%s-advertisement" % service
                self._send_bytes(200, body, ctype)
                return
            if method == "POST" and suffix in ("/git-upload-pack",
                                               "/git-receive-pack"):
                if store.get_repo(owner, repo_name) is None:
                    self._not_found()
                    return
                body = self._read_body()
                try:
                    if suffix == "/git-upload-pack":
                        version2 = self.headers.get("Git-Protocol") == "version=2"
                        out = handle_upload_pack_request(store, repo_name, body, version2)
                        self._send_bytes(200, out,
                                         "application/x-git-upload-pack-result")
                    else:
                        out = handle_receive_pack(store, repo_name, body)
                        self._send_bytes(200, out,
                                         "application/x-git-receive-pack-result")
                except GitError as exc:
                    self._send_json(exc.status, {"message": exc.message})
                except ValueError as exc:
                    self._send_json(400, {"message": "malformed request: %s" % exc})
                return
            self._not_found()

    return Handler


def serve(data_dir, port):
    forge = Forge(data_dir)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(forge))
    server.daemon_threads = True
    return server, forge
