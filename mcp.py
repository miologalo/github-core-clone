"""MCP (/mcp): JSON-RPC 2.0 over Streamable HTTP.

Speaks the modern revision 2026-07-28 (server/discover, no handshake, no
session) and the legacy handshake era (initialize +
notifications/initialized, 2025-11-25 and earlier). Answers are plain
application/json bodies; each request is answered inside the call. Tools
act on the same store as REST, so a repo or issue created over MCP reads
back over /api/v3 with the same ids.

Standard library only.
"""

from __future__ import annotations

import json

from rest_v3 import issue_view, pull_view, repo_view, squash_merge

SERVER_NAME = "github-core-clone"
SERVER_VERSION = "1.0.0"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18",
                      "2025-03-26", "2024-11-05"]
CAPABILITIES = {"tools": {}}

TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a new repository owned by the token user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name"},
                "owner": {"type": "string",
                          "description": "Owner login (the token user)"},
                "description": {"type": "string",
                                "description": "Repository description"},
                "private": {"type": "boolean",
                            "description": "Whether the repository is private"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "full_name": {"type": "string",
                              "description": "owner/repo"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "full_name": {"type": "string",
                              "description": "owner/repo"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "full_name": {"type": "string",
                              "description": "owner/repo"},
                "state": {"type": "string",
                          "description": "open, closed or all"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Open a pull request from a head branch into a base branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "full_name": {"type": "string",
                              "description": "owner/repo"},
                "title": {"type": "string",
                          "description": "Pull request title"},
                "body": {"type": "string",
                         "description": "Pull request body"},
                "head": {"type": "string",
                         "description": "Head branch name"},
                "base": {"type": "string",
                         "description": "Base branch name"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Squash-merge a pull request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "full_name": {"type": "string",
                              "description": "owner/repo"},
                "pull_number": {"type": "integer",
                                "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
    {
        "name": "get_issue",
        "description": "Get one issue by owner, repository and number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "number": {"type": "integer",
                           "description": "Issue number"},
                "issue_number": {"type": "integer",
                                 "description": "Issue number"},
            },
            "required": ["owner", "repo", "number"],
        },
    },
    {
        "name": "list_repos",
        "description": "List repositories owned by the token user.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _tool_result(payload, is_error=False):
    text = json.dumps(payload, sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload, "isError": is_error}


def _split_full_name(args):
    full = args.get("full_name")
    if isinstance(full, str) and "/" in full:
        owner, _, repo = full.partition("/")
        args.setdefault("owner", owner)
        args.setdefault("repo", repo)
    return args


def _repo_of(forge, args):
    owner = args.get("owner") or args.get("owner_login") or ""
    repo_name = args.get("repo") or args.get("repository") or ""
    if not owner or not repo_name:
        return None, None, "owner and repo are required"
    repo = forge.store.get_repo(owner, repo_name)
    if repo is None:
        return None, None, "repository %s/%s not found" % (owner, repo_name)
    return repo, repo_name, None


def _call_tool(forge, name, args, base_url):
    """Run one tool; return (payload, is_error)."""
    store = forge.store
    args = _split_full_name(dict(args or {}))
    canonical = name.lower().replace("-", "_")
    for tool in TOOLS:
        if canonical.endswith(tool["name"]) or canonical == tool["name"]:
            canonical = tool["name"]
            break
    else:
        return None, None  # unknown tool (caller turns it into -32602)

    def missing(*keys):
        for key in keys:
            if args.get(key) in (None, ""):
                return "Missing required argument: %s" % key
        return None

    if canonical == "create_repo":
        problem = missing("name")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo_name = args["name"]
        with forge._write_lock:
            repo = store.repos.get(repo_name)
            if repo is None:
                repo = store.create_repo(
                    repo_name, args.get("description") or "",
                    bool(args.get("private", False)))
            owner = args.get("owner") or ""
            if owner:
                repo = store.get_repo(owner, repo_name) or repo
        return _tool_result(repo_view(repo, base_url)), False

    if canonical == "get_repo":
        problem = missing("owner", "repo")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo, _, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        return _tool_result(repo_view(repo, base_url)), False

    if canonical == "create_issue":
        problem = missing("owner", "repo", "title")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo, repo_name, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        with forge._write_lock:
            record = store.create_issue(
                repo_name, args["title"], args.get("body") or "", [],
                repo.owner or "")
        return _tool_result(issue_view(store, repo, record, base_url)), False

    if canonical == "list_issues":
        problem = missing("owner", "repo")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo, repo_name, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        state = (args.get("state") or "open").lower()
        issues = store.list_issues(repo_name)
        if state != "all":
            issues = [i for i in issues if i["state"] == state]
        payload = {"issues": [issue_view(store, repo, i, base_url)
                              for i in issues]}
        return _tool_result(payload), False

    if canonical == "create_pull_request":
        problem = missing("owner", "repo", "title", "head", "base")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo, repo_name, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        head = args["head"]
        base = args["base"]
        if ":" in head:
            head = head.split(":", 1)[1]
        if store.get_ref(repo_name, "refs/heads/" + head) is None:
            return _tool_result(
                {"error": "head branch %s does not exist" % head}, True), True
        with forge._write_lock:
            existing = [p for p in store.list_pulls(repo_name)
                        if p["head"] == head and p["state"] == "open"]
            if existing:
                record = existing[0]
            else:
                record = store.create_pull(
                    repo_name, args["title"], args.get("body") or "",
                    head, base, repo.owner or "")
        return _tool_result(pull_view(store, repo, record, base_url)), False

    if canonical == "merge_pull_request":
        problem = missing("owner", "repo", "pull_number")
        if problem:
            return _tool_result({"error": problem}, True), True
        repo, repo_name, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        try:
            number = int(args["pull_number"])
        except (TypeError, ValueError):
            return _tool_result(
                {"error": "pull_number must be an integer"}, True), True
        record = store.get_pull(repo_name, number)
        if record is None:
            return _tool_result(
                {"error": "pull request %d not found" % number}, True), True
        if record["state"] != "open":
            return _tool_result(
                {"error": "pull request is already closed"}, True), True
        with forge._write_lock:
            commit_sha, conflict = squash_merge(store, repo_name, record)
            if conflict is not None:
                return _tool_result({"error": conflict}, True), True
            from rest_v3 import EPOCH
            store.patch_pull(repo_name, number, {
                "state": "closed", "merged": True, "merged_at": EPOCH,
                "merge_commit_sha": commit_sha})
        return _tool_result({"merged": True, "sha": commit_sha,
                             "number": number, "state": "closed"}), False

    if canonical == "get_issue":
        number = args.get("number", args.get("issue_number"))
        if args.get("owner") in (None, "") or args.get("repo") in (None, "") \
                or number is None:
            return _tool_result(
                {"error": "Missing required argument: owner, repo, number"},
                True), True
        repo, repo_name, err = _repo_of(forge, args)
        if err:
            return _tool_result({"error": err}, True), True
        record = store.get_issue(repo_name, int(number))
        if record is None:
            return _tool_result(
                {"error": "issue %s not found" % number}, True), True
        return _tool_result(issue_view(store, repo, record, base_url)), False

    if canonical == "list_repos":
        payload = {"repositories": [repo_view(r, base_url)
                                    for r in store.list_repos()]}
        return _tool_result(payload), False

    return None, None


def handle_mcp(handler):
    """POST /mcp (DELETE answers 204 for a legacy session teardown)."""
    if handler.command == "DELETE":
        handler._send_no_content(204)
        return True
    if handler.command != "POST":
        handler._send_json(404, {"message": "Not Found"})
        return True
    if not handler._require_auth():
        return True
    raw = handler._read_body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        handler.send_response(400)
        handler.send_header("Content-Type", "application/json")
        payload = json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"}}).encode("utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True
    if not isinstance(body, dict):
        handler._send_json(400, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": "Parse error"}})
        return True

    request_id = body.get("id")
    method = body.get("method") or ""
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # Notifications carry no id: acknowledge with 202 and no body.
    if request_id is None:
        handler._send_no_content(202)
        return True

    def result(payload):
        handler._send_json(200, {"jsonrpc": "2.0", "id": request_id,
                                  "result": payload})

    def error(code, message):
        handler._send_json(200, {"jsonrpc": "2.0", "id": request_id,
                                  "error": {"code": code, "message": message}})

    if method == "server/discover":
        result({
            "resultType": "DiscoverResult",
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "_meta": {"io.modelcontextprotocol/serverInfo":
                      {"name": SERVER_NAME, "version": SERVER_VERSION}},
        })
        return True
    if method == "initialize":
        offered = params.get("protocolVersion")
        version = offered if offered in SUPPORTED_VERSIONS else "2025-11-25"
        result({
            "protocolVersion": version,
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return True
    if method == "ping":
        result({})
        return True
    if method == "tools/list":
        result({"tools": TOOLS})
        return True
    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        payload, is_error = _call_tool(handler.forge, name, args,
                                       handler._base_url())
        if payload is None:
            error(-32602, "Unknown tool: %s" % name)
            return True
        result(payload)
        return True
    error(-32601, "Method not found: %s" % method)
    return True
