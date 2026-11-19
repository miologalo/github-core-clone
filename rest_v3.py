"""REST v3 (/api/v3): repositories, branches, commits, contents, labels,
issues, comments and pull requests, on top of the Phase 1 store.

Standard library only. Every reply is JSON; every error carries a string
``message``. Writes are serialized through the forge's write lock so the
2,000-read workload and concurrent clones never race a mutation.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urlparse

from git_objects import (ZERO_OID, build_commit, build_tree, hash_object,
                         parse_commit, parse_tree)
from store import DEFAULT_BRANCH

AUTHOR = "Forge <forge@example.invalid> 0 +0000"
EPOCH = "1970-01-01T00:00:00Z"


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


def issue_view(store, repo, record, base_url):
    labels = [{"name": n,
               "color": (store.labels.get(repo.name, {}).get(n) or {})
               .get("color", "ededed")}
              for n in sorted(record["labels"])]
    return {
        "id": record["id"],
        "number": record["number"],
        "title": record["title"],
        "body": record["body"],
        "state": record["state"],
        "labels": labels,
        "user": {"login": record.get("user") or repo.owner or "",
                 "id": 1000, "type": "User"},
        "html_url": "%s/%s/%s/issues/%d" % (
            base_url, repo.owner or "", repo.name, record["number"]),
        "created_at": EPOCH,
        "updated_at": EPOCH,
    }


def pull_view(store, repo, record, base_url):
    head_sha = store.get_ref(repo.name, "refs/heads/" + record["head"]) or ""
    base_sha = store.get_ref(repo.name, "refs/heads/" + record["base"]) or ""
    owner = repo.owner or ""
    return {
        "id": record["id"],
        "number": record["number"],
        "title": record["title"],
        "body": record["body"],
        "state": record["state"],
        "merged": record["merged"],
        "merged_at": record["merged_at"],
        "merge_commit_sha": record["merge_commit_sha"],
        "head": {"ref": record["head"], "sha": head_sha,
                 "label": "%s:%s" % (owner, record["head"]),
                 "user": {"login": owner, "id": 1000, "type": "User"}},
        "base": {"ref": record["base"], "sha": base_sha,
                 "label": "%s:%s" % (owner, record["base"]),
                 "user": {"login": owner, "id": 1000, "type": "User"}},
        "user": {"login": record.get("user") or owner,
                 "id": 1000, "type": "User"},
        "html_url": "%s/%s/%s/pull/%d" % (
            base_url, owner, repo.name, record["number"]),
        "created_at": EPOCH,
        "updated_at": EPOCH,
    }


# --------------------------------------------------------------------------
# git tree helpers (flat path -> (mode, oid) maps)

def _tree_map(store, tree_oid):
    """Flatten a tree object into {path: (mode, oid)}."""
    out = {}
    stack = [("", tree_oid)]
    while stack:
        prefix, oid = stack.pop()
        entry = store.get_object(oid)
        if entry is None or entry[0] != "tree":
            continue
        for mode, name, child in parse_tree(entry[1]):
            path = prefix + name
            if mode == "40000":
                stack.append((path + "/", child))
            elif mode == "160000":
                continue
            else:
                out[path] = (mode, child)
    return out


def _build_tree_from_map(store, flat):
    """Build nested tree objects from a flat map; return the root tree oid."""
    entries = []
    subdirs = {}
    for path, (mode, oid) in flat.items():
        if "/" in path:
            top, rest = path.split("/", 1)
            subdirs.setdefault(top, {})[rest] = (mode, oid)
        else:
            entries.append((mode, path, oid))
    for top, sub in sorted(subdirs.items()):
        entries.append(("40000", top, _build_tree_from_map(store, sub)))
    return store.put_object("tree", build_tree(entries))


def _commit_at(store, repo_name, ref):
    """Resolve a branch name / sha / ref to a commit oid, or None."""
    if not ref:
        ref = DEFAULT_BRANCH
    oid = store.get_ref(repo_name, "refs/heads/" + ref)
    if oid:
        return store.peel(oid)
    oid = store.get_ref(repo_name, ref)
    if oid:
        return store.peel(oid)
    if len(ref) == 40 and store.get_object(ref):
        return store.peel(ref)
    return None


def _commit_file(store, repo_name, branch, path, content, message,
                 expected_sha=None, delete=False):
    """Create/update/delete one file on a branch; return (commit_sha,
    blob_sha_or_None) or raise ValueError(status, message)."""
    if not branch:
        branch = DEFAULT_BRANCH
    tip = store.get_ref(repo_name, "refs/heads/" + branch)
    if tip is None and branch != DEFAULT_BRANCH:
        # A contents write may name a branch that does not exist yet: start
        # it from the default branch tip (the MCP setup path relies on this).
        tip = store.get_ref(repo_name, "refs/heads/" + DEFAULT_BRANCH)
    parents = []
    flat = {}
    if tip:
        commit_oid = store.peel(tip)
        entry = store.get_object(commit_oid)
        if entry is None or entry[0] != "commit":
            raise ValueError((400, "branch tip is not a commit"))
        commit = parse_commit(entry[1])
        flat = _tree_map(store, commit["tree"])
        parents = [commit_oid]
    current = flat.get(path)
    if delete:
        if current is None:
            raise ValueError((404, "Not Found"))
        if expected_sha and current[1].lower() != expected_sha.lower():
            raise ValueError((409, "sha does not match the current file"))
        del flat[path]
        blob_sha = None
    else:
        blob_sha = store.put_object("blob", content)
        if current is not None:
            if expected_sha is not None and \
                    current[1].lower() != expected_sha.lower():
                raise ValueError((409, "sha does not match the current file"))
            if expected_sha is None:
                raise ValueError((422, "sha is required to update a file"))
        mode = current[0] if current else "100644"
        flat[path] = (mode, blob_sha)
    tree_oid = _build_tree_from_map(store, flat)
    data = build_commit(tree_oid, parents, AUTHOR, AUTHOR, message)
    commit_sha = store.put_object("commit", data)
    store.set_ref(repo_name, "refs/heads/" + branch, commit_sha)
    return commit_sha, blob_sha


def _ancestors(store, tip):
    """All commit oids reachable from tip (tip first, parents after)."""
    out = []
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop(0)
        if oid in seen or oid == ZERO_OID:
            continue
        seen.add(oid)
        entry = store.get_object(oid)
        if entry is None or entry[0] != "commit":
            continue
        out.append(oid)
        stack.extend(parse_commit(entry[1])["parents"])
    return out


def _merge_base(store, a, b):
    ancestors_a = set(_ancestors(store, a))
    for oid in _ancestors(store, b):
        if oid in ancestors_a:
            return oid
    return None


def _diff_maps(old, new):
    """[{path, status, old, new}] between two flat tree maps."""
    out = []
    for path in sorted(set(old) | set(new)):
        old_entry = old.get(path)
        new_entry = new.get(path)
        if old_entry is None:
            out.append({"path": path, "status": "added",
                        "old": None, "new": new_entry[1]})
        elif new_entry is None:
            out.append({"path": path, "status": "removed",
                        "old": old_entry[1], "new": None})
        elif old_entry[1] != new_entry[1]:
            out.append({"path": path, "status": "modified",
                        "old": old_entry[1], "new": new_entry[1]})
    return out


def _line_count(store, oid):
    if oid is None:
        return 0
    entry = store.get_object(oid)
    if entry is None:
        return 0
    return entry[1].count(b"\n") + (1 if entry[1] and not entry[1].endswith(b"\n") else 0)


def pr_files(store, repo_name, record):
    """Changed files of a pull request (merge-base tree vs head tree)."""
    head_tip = store.get_ref(repo_name, "refs/heads/" + record["head"])
    base_tip = store.get_ref(repo_name, "refs/heads/" + record["base"])
    if not head_tip or not base_tip:
        return []
    head_commit = store.peel(head_tip)
    base_commit = store.peel(base_tip)
    mb = _merge_base(store, base_commit, head_commit)
    mb_tree = ""
    if mb:
        entry = store.get_object(mb)
        if entry and entry[0] == "commit":
            mb_tree = parse_commit(entry[1])["tree"]
    head_entry = store.get_object(head_commit)
    head_tree = parse_commit(head_entry[1])["tree"] if head_entry else ""
    out = []
    for change in _diff_maps(_tree_map(store, mb_tree),
                             _tree_map(store, head_tree)):
        additions = _line_count(store, change["new"])
        deletions = _line_count(store, change["old"])
        if change["status"] == "added":
            deletions = 0
        elif change["status"] == "removed":
            additions = 0
        out.append({
            "filename": change["path"],
            "status": change["status"],
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
        })
    return out


def mergeable_state(store, repo_name, record):
    """MERGEABLE / CONFLICTING / UNKNOWN for an open pull request."""
    if record["state"] != "open":
        return "UNKNOWN"
    head_tip = store.get_ref(repo_name, "refs/heads/" + record["head"])
    base_tip = store.get_ref(repo_name, "refs/heads/" + record["base"])
    if not head_tip or not base_tip:
        return "UNKNOWN"
    return "CONFLICTING" if _squash_conflict(
        store, repo_name, record) is not None else "MERGEABLE"


def _squash_conflict(store, repo_name, record):
    """Return a conflict detail string, or None when the squash is clean."""
    head_tip = store.get_ref(repo_name, "refs/heads/" + record["head"])
    base_tip = store.get_ref(repo_name, "refs/heads/" + record["base"])
    if not head_tip or not base_tip:
        return "head or base branch is missing"
    head_commit = store.peel(head_tip)
    base_commit = store.peel(base_tip)
    mb = _merge_base(store, base_commit, head_commit)
    if mb is None:
        return "no merge base"
    mb_entry = store.get_object(mb)
    mb_tree = parse_commit(mb_entry[1])["tree"] if mb_entry else ""
    base_entry = store.get_object(base_commit)
    head_entry = store.get_object(head_commit)
    base_tree = parse_commit(base_entry[1])["tree"] if base_entry else ""
    head_tree = parse_commit(head_entry[1])["tree"] if head_entry else ""
    mb_map = _tree_map(store, mb_tree)
    base_map = _tree_map(store, base_tree)
    head_map = _tree_map(store, head_tree)
    base_changes = {c["path"]: c for c in _diff_maps(mb_map, base_map)}
    head_changes = {c["path"]: c for c in _diff_maps(mb_map, head_map)}
    for path, hchange in head_changes.items():
        bchange = base_changes.get(path)
        if bchange is None:
            continue
        if hchange["new"] != bchange["new"]:
            return "conflict in %s" % path
    return None


def squash_merge(store, repo_name, record):
    """Squash-merge an open pull request onto its base branch.

    Returns (commit_sha, None) on success or (None, detail) on conflict."""
    conflict = _squash_conflict(store, repo_name, record)
    if conflict is not None:
        return None, conflict
    head_tip = store.get_ref(repo_name, "refs/heads/" + record["head"])
    base_tip = store.get_ref(repo_name, "refs/heads/" + record["base"])
    head_commit = store.peel(head_tip)
    base_commit = store.peel(base_tip)
    mb = _merge_base(store, base_commit, head_commit)
    mb_entry = store.get_object(mb)
    mb_tree = parse_commit(mb_entry[1])["tree"] if mb_entry else ""
    base_entry = store.get_object(base_commit)
    head_entry = store.get_object(head_commit)
    base_tree = parse_commit(base_entry[1])["tree"] if base_entry else ""
    head_tree = parse_commit(head_entry[1])["tree"] if head_entry else ""
    mb_map = _tree_map(store, mb_tree)
    base_map = _tree_map(store, base_tree)
    head_map = _tree_map(store, head_tree)
    new_map = dict(base_map)
    for change in _diff_maps(mb_map, head_map):
        if change["new"] is None:
            new_map.pop(change["path"], None)
        else:
            new_map[change["path"]] = (
                head_map[change["path"]][0], change["new"])
    tree_oid = _build_tree_from_map(store, new_map)
    message = ("Merge pull request #%d from %s\n\n%s"
               % (record["number"], record["head"], record["title"]))
    data = build_commit(tree_oid, [base_commit], AUTHOR, AUTHOR, message)
    commit_sha = store.put_object("commit", data)
    store.set_ref(repo_name, "refs/heads/" + record["base"], commit_sha)
    return commit_sha, None


# --------------------------------------------------------------------------
# routing

def _page_params(query):
    try:
        per_page = int((query.get("per_page") or ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int((query.get("page") or ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    return per_page, page


def _paginate(handler, items, query):
    per_page, page = _page_params(query)
    total = len(items)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    extra = None
    if start + per_page < total:
        from urllib.parse import urlencode
        q = dict(query)
        q["page"] = [str(page + 1)]
        q["per_page"] = [str(per_page)]
        flat = {k: v[0] for k, v in q.items()}
        url = "%s%s?%s" % (handler._base_url(), handler._path_only(),
                           urlencode(flat))
        extra = {"Link": '<%s>; rel="next"' % url}
    return chunk, extra


def handle_rest(handler, method, path, query):
    """Handle one /api/v3 request. Returns True when handled (a response
    was sent), False for 'not a REST route' (caller sends the JSON 404)."""
    forge = handler.forge
    store = forge.store
    if not path.startswith("/api/v3"):
        return False
    if not handler._require_auth():
        return True

    def send(status, payload, extra=None):
        handler._send_json(status, payload, extra)

    def err(status, message):
        send(status, {"message": message})

    # -- API root and meta -------------------------------------------------
    if path == "/api/v3/" and method == "GET":
        send(200, {
            "current_user_url": handler._base_url() + "/api/v3/user",
            "repository_url": handler._base_url() + "/api/v3/repos/{owner}/{repo}",
        }, {"X-OAuth-Scopes": "repo, read:org"})
        return True
    if path == "/api/v3/meta" and method == "GET":
        send(200, {"installed_version": "1.0.0",
                   "verifiable_password_authentication": True})
        return True
    if path == "/api/v3/user" and method == "GET":
        login = store.learned_login
        if not login:
            err(404, "Not Found")
            return True
        send(200, {"login": login, "id": 1000, "type": "User"})
        return True

    # -- /user/repos -------------------------------------------------------
    if path == "/api/v3/user/repos":
        if method == "GET":
            repos = [repo_view(r, handler._base_url())
                     for r in store.list_repos()]
            chunk, extra = _paginate(handler, repos, query)
            send(200, chunk, extra)
            return True
        if method == "POST":
            body = handler._json_body()
            if body is None:
                handler._bad_json()
                return True
            name = body.get("name")
            if not isinstance(name, str) or not name:
                err(422, "Repository name is required")
                return True
            with forge._write_lock:
                repo = store.create_repo(
                    name, body.get("description") or "",
                    bool(body.get("private", False)))
            if repo is None:
                err(422, "Repository already exists")
                return True
            send(201, repo_view(repo, handler._base_url()))
            return True
        err(404, "Not Found")
        return True

    m = re.match(r"^/api/v3/repos/([^/]+)/([^/]+?)(/.*)?$", path)
    if not m:
        return False
    owner, name, rest = m.group(1), m.group(2), m.group(3) or ""
    repo = store.get_repo(owner, name)
    if repo is None:
        err(404, "Not Found")
        return True
    base = handler._base_url()

    # -- the repository itself ---------------------------------------------
    if rest == "":
        if method == "GET":
            send(200, repo_view(repo, base))
            return True
        if method == "PATCH":
            body = handler._json_body()
            if body is None:
                handler._bad_json()
                return True
            with forge._write_lock:
                if isinstance(body.get("description"), str):
                    repo.description = body["description"]
                if isinstance(body.get("private"), bool):
                    repo.private = body["private"]
                try:
                    store._save_repos()
                except OSError:
                    pass
            send(200, repo_view(repo, base))
            return True
        if method == "DELETE":
            with forge._write_lock:
                store.delete_repo(name)
            handler._send_no_content(204)
            return True
        err(404, "Not Found")
        return True

    # -- git refs -----------------------------------------------------------
    if rest == "/git/refs" and method == "POST":
        body = handler._json_body()
        if body is None:
            handler._bad_json()
            return True
        ref = body.get("ref") or ""
        sha = body.get("sha") or ""
        if not ref.startswith("refs/") or not sha:
            err(422, "ref and sha are required")
            return True
        if not store.get_object(store.peel(sha)):
            err(422, "sha does not exist")
            return True
        with forge._write_lock:
            store.set_ref(name, ref, store.peel(sha))
        send(201, {"ref": ref, "object": {"sha": store.peel(sha),
                                           "type": "commit"}})
        return True
    mref = re.match(r"^/git/refs/(.+)$", rest)
    if mref and method == "GET":
        ref = mref.group(1)
        oid = store.get_ref(name, ref)
        if oid is None:
            err(404, "Not Found")
            return True
        send(200, {"ref": ref, "object": {"sha": oid, "type": "commit"}})
        return True
    if mref and method == "DELETE":
        with forge._write_lock:
            store.delete_ref(name, mref.group(1))
        handler._send_no_content(204)
        return True

    # -- branches -----------------------------------------------------------
    if rest == "/branches" and method == "GET":
        out = []
        for ref, oid in store.get_refs(name).items():
            if ref.startswith("refs/heads/"):
                out.append({"name": ref[len("refs/heads/"):],
                            "commit": {"sha": oid}, "protected": False})
        out.sort(key=lambda b: b["name"])
        send(200, out)
        return True
    mbr = re.match(r"^/branches/([^/]+)$", rest)
    if mbr and method == "GET":
        oid = store.get_ref(name, "refs/heads/" + mbr.group(1))
        if oid is None:
            err(404, "Not Found")
            return True
        send(200, {"name": mbr.group(1), "commit": {"sha": oid},
                   "protected": False})
        return True

    # -- commits ------------------------------------------------------------
    if rest == "/commits" and method == "GET":
        sha_param = (query.get("sha") or [None])[0]
        tip = _commit_at(store, name, sha_param)
        out = []
        if tip:
            for oid in _ancestors(store, tip):
                entry = store.get_object(oid)
                commit = parse_commit(entry[1])
                out.append({
                    "sha": oid,
                    "commit": {"message": commit["message"],
                               "author": {"name": "Forge",
                                          "email": "forge@example.invalid"},
                               "committer": {"name": "Forge",
                                             "email": "forge@example.invalid"}},
                    "author": {"login": repo.owner or ""},
                    "committer": {"login": repo.owner or ""},
                    "parents": [{"sha": p} for p in commit["parents"]],
                })
        chunk, extra = _paginate(handler, out, query)
        send(200, chunk, extra)
        return True

    # -- labels -------------------------------------------------------------
    if rest == "/labels" and method == "GET":
        send(200, store.list_labels(name))
        return True
    if rest == "/labels" and method == "POST":
        body = handler._json_body()
        if body is None:
            handler._bad_json()
            return True
        label = body.get("name")
        if not isinstance(label, str) or not label:
            err(422, "Label name is required")
            return True
        with forge._write_lock:
            record = store.create_label(name, label, body.get("color"))
        send(201, record)
        return True

    # -- contents -----------------------------------------------------------
    mcont = re.match(r"^/contents(?:/(.*))?$", rest)
    if mcont:
        cpath = mcont.group(1) or ""
        ref_param = (query.get("ref") or [None])[0]
        if method == "GET":
            tip = _commit_at(store, name, ref_param)
            if tip is None:
                err(404, "Not Found")
                return True
            entry = store.get_object(tip)
            tree = _tree_map(store, parse_commit(entry[1])["tree"])
            if cpath == "":
                send(200, _dir_listing(store, tree, ""))
                return True
            hit = tree.get(cpath)
            if hit is not None:
                blob = store.get_object(hit[1])
                data = blob[1] if blob else b""
                send(200, {
                    "type": "file", "encoding": "base64",
                    "content": base64.b64encode(data).decode("ascii"),
                    "sha": hit[1], "size": len(data),
                    "name": cpath.split("/")[-1], "path": cpath,
                    "url": "%s/api/v3/repos/%s/%s/contents/%s"
                           % (base, owner, name, cpath),
                    "html_url": "%s/%s/%s/blob/%s/%s"
                                % (base, owner, name,
                                   ref_param or DEFAULT_BRANCH, cpath),
                })
                return True
            # directory?
            prefix = cpath + "/"
            if any(p.startswith(prefix) for p in tree):
                send(200, _dir_listing(store, tree, cpath))
                return True
            err(404, "Not Found")
            return True
        if method == "PUT":
            body = handler._json_body()
            if body is None:
                handler._bad_json()
                return True
            content_b64 = body.get("content")
            if not isinstance(content_b64, str):
                err(422, "content is required")
                return True
            try:
                content = base64.b64decode(content_b64, validate=False)
            except Exception:
                err(422, "content is not valid base64")
                return True
            branch = body.get("branch") or DEFAULT_BRANCH
            existed = False
            tip = _commit_at(store, name, branch)
            if tip:
                entry = store.get_object(tip)
                existed = cpath in _tree_map(
                    store, parse_commit(entry[1])["tree"])
            with forge._write_lock:
                try:
                    commit_sha, blob_sha = _commit_file(
                        store, name, branch, cpath, content,
                        body.get("message") or "update %s" % cpath,
                        expected_sha=body.get("sha"))
                except ValueError as exc:
                    status, message = exc.args[0]
                    err(status, message)
                    return True
            send(201 if not existed else 200, {
                "content": {"sha": blob_sha, "encoding": "base64",
                            "name": cpath.split("/")[-1], "path": cpath},
                "commit": {"sha": commit_sha},
            })
            return True
        if method == "DELETE":
            body = handler._json_body()
            if body is None:
                handler._bad_json()
                return True
            with forge._write_lock:
                try:
                    commit_sha, _ = _commit_file(
                        store, name, body.get("branch") or DEFAULT_BRANCH,
                        cpath, b"", body.get("message") or "delete %s" % cpath,
                        expected_sha=body.get("sha"), delete=True)
                except ValueError as exc:
                    status, message = exc.args[0]
                    err(status, message)
                    return True
            send(200, {"commit": {"sha": commit_sha}})
            return True
        err(404, "Not Found")
        return True

    # -- issues -------------------------------------------------------------
    if rest == "/issues" and method == "GET":
        issues = store.list_issues(name)
        state = (query.get("state") or ["open"])[0]
        if state != "all":
            issues = [i for i in issues if i["state"] == state]
        if (query.get("sort") or ["created"])[0] == "created":
            reverse = (query.get("direction") or ["desc"])[0] == "desc"
            issues.sort(key=lambda i: i["number"], reverse=reverse)
        out = [issue_view(store, repo, i, base) for i in issues]
        chunk, extra = _paginate(handler, out, query)
        send(200, chunk, extra)
        return True
    if rest == "/issues" and method == "POST":
        body = handler._json_body()
        if body is None:
            handler._bad_json()
            return True
        title = body.get("title")
        if not isinstance(title, str) or not title:
            err(422, "title is required")
            return True
        labels = body.get("labels") or []
        if isinstance(labels, list):
            label_names = []
            for item in labels:
                label_names.append(item.get("name") if isinstance(item, dict)
                                   else item)
            for label_name in label_names:
                if not store.has_label(name, label_name):
                    err(422, "label %s does not exist" % label_name)
                    return True
        else:
            label_names = []
        with forge._write_lock:
            record = store.create_issue(
                name, title, body.get("body") or "", label_names,
                repo.owner or "")
        send(201, issue_view(store, repo, record, base))
        return True
    miss = re.match(r"^/issues/(\d+)(/.*)?$", rest)
    if miss:
        number = int(miss.group(1))
        tail = miss.group(2) or ""
        if tail == "/comments" and method == "POST":
            body = handler._json_body()
            if body is None:
                handler._bad_json()
                return True
            if store.get_issue(name, number) is None:
                err(404, "Not Found")
                return True
            with forge._write_lock:
                comment = store.create_comment(
                    name, number, body.get("body") or "", repo.owner or "")
            send(201, {"id": comment["id"], "body": comment["body"],
                       "user": {"login": repo.owner or ""}})
            return True
        if tail == "/comments" and method == "GET":
            if store.get_issue(name, number) is None:
                err(404, "Not Found")
                return True
            out = [{"id": c["id"], "body": c["body"],
                    "user": {"login": c.get("user") or repo.owner or ""}}
                   for c in store.list_comments(name, number)]
            send(200, out)
            return True
        if tail == "":
            if method == "GET":
                record = store.get_issue(name, number)
                if record is None:
                    err(404, "Not Found")
                    return True
                send(200, issue_view(store, repo, record, base))
                return True
            if method == "PATCH":
                body = handler._json_body()
                if body is None:
                    handler._bad_json()
                    return True
                if isinstance(body.get("labels"), list):
                    for item in body["labels"]:
                        label_name = item.get("name") if isinstance(item, dict) else item
                        if not store.has_label(name, label_name):
                            err(422, "label %s does not exist" % label_name)
                            return True
                with forge._write_lock:
                    record = store.patch_issue(name, number, body)
                if record is None:
                    err(404, "Not Found")
                    return True
                send(200, issue_view(store, repo, record, base))
                return True
        err(404, "Not Found")
        return True

    # -- pulls --------------------------------------------------------------
    if rest == "/pulls" and method == "GET":
        pulls = store.list_pulls(name)
        state = (query.get("state") or ["open"])[0]
        if state != "all":
            pulls = [p for p in pulls if p["state"] == state]
        out = [pull_view(store, repo, p, base) for p in pulls]
        chunk, extra = _paginate(handler, out, query)
        send(200, chunk, extra)
        return True
    if rest == "/pulls" and method == "POST":
        body = handler._json_body()
        if body is None:
            handler._bad_json()
            return True
        title = body.get("title")
        head = body.get("head")
        base_ref = body.get("base") or DEFAULT_BRANCH
        if not isinstance(title, str) or not title or \
                not isinstance(head, str) or not head:
            err(422, "title and head are required")
            return True
        if ":" in head:
            head = head.split(":", 1)[1]
        if store.get_ref(name, "refs/heads/" + head) is None:
            err(422, "head branch %s does not exist" % head)
            return True
        if store.get_ref(name, "refs/heads/" + base_ref) is None and \
                store.get_refs(name):
            err(422, "base branch %s does not exist" % base_ref)
            return True
        with forge._write_lock:
            for existing in store.list_pulls(name):
                if existing["head"] == head and existing["state"] == "open":
                    err(422, "a pull request from %s is already open" % head)
                    return True
            record = store.create_pull(name, title, body.get("body") or "",
                                       head, base_ref, repo.owner or "")
        send(201, pull_view(store, repo, record, base))
        return True
    mpull = re.match(r"^/pulls/(\d+)(/.*)?$", rest)
    if mpull:
        number = int(mpull.group(1))
        tail = mpull.group(2) or ""
        if tail == "/files" and method == "GET":
            record = store.get_pull(name, number)
            if record is None:
                err(404, "Not Found")
                return True
            send(200, pr_files(store, name, record))
            return True
        if tail == "/merge" and method == "PUT":
            body = handler._json_body()
            if body is None:
                body = {}
            record = store.get_pull(name, number)
            if record is None:
                err(404, "Not Found")
                return True
            if record["state"] != "open":
                err(405, "pull request is already closed")
                return True
            with forge._write_lock:
                commit_sha, conflict = squash_merge(store, name, record)
                if conflict is not None:
                    err(405, conflict)
                    return True
                store.patch_pull(name, number, {
                    "state": "closed", "merged": True,
                    "merged_at": EPOCH, "merge_commit_sha": commit_sha})
            send(200, {"sha": commit_sha, "merged": True,
                       "message": "Pull request successfully merged"})
            return True
        if tail == "" and method == "GET":
            record = store.get_pull(name, number)
            if record is None:
                err(404, "Not Found")
                return True
            send(200, pull_view(store, repo, record, base))
            return True
        err(404, "Not Found")
        return True

    err(404, "Not Found")
    return True


def _dir_listing(store, tree, dirpath):
    """Directory entries for GET /contents on a directory."""
    prefix = (dirpath + "/") if dirpath else ""
    seen = {}
    for path, (mode, oid) in tree.items():
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        first = rest.split("/")[0]
        if "/" in rest:
            if first not in seen:
                sub = {p[len(prefix) + len(first) + 1:]: v
                       for p, v in tree.items()
                       if p.startswith(prefix + first + "/")}
                seen[first] = {"type": "dir", "name": first,
                               "path": prefix + first,
                               "sha": _build_tree_from_map(store, sub)}
        else:
            entry = store.get_object(oid)
            seen[first] = {"type": "file", "name": first,
                           "path": path, "sha": oid,
                           "size": len(entry[1]) if entry else 0}
    return [seen[k] for k in sorted(seen)]
