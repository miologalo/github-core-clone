"""GraphQL v4 (/api/graphql): a small hand-rolled executor for exactly the
documents gh and github-mcp-server send — viewer/repository reads,
introspection __type probes, issue/pull-request connections, and the
create/close/merge/addComment mutations. Not a full GraphQL engine.

Standard library only. Unknown fields are HTTP 200 with an errors array
naming the field; an unknown repository is data.repository = null with no
errors; unauthenticated requests are exactly 401 (handled by the caller).
"""

from __future__ import annotations

import base64
import json
import re

from rest_v3 import mergeable_state, pr_files, squash_merge

# --------------------------------------------------------------------------
# global ids: opaque, stable, never the plain number

def gid(kind, *parts):
    raw = ":".join([kind] + [str(p) for p in parts])
    return base64.b64encode(raw.encode("ascii")).decode("ascii")


def ungid(value):
    try:
        raw = base64.b64decode(value.encode("ascii")).decode("ascii")
    except Exception:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


# --------------------------------------------------------------------------
# tokenizer + parser

_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comma>,)
  | (?P<ellipsis>\.\.\.)
  | (?P<punct>[{}()\[\]:\$!@=])
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


def _tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError("unexpected character %r at %d" % (text[pos], pos))
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comma"):
            continue
        tokens.append((kind, m.group()))
    tokens.append(("eof", ""))
    return tokens


class _Parser:
    def __init__(self, text):
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect_punct(self, value):
        kind, text = self.next()
        if kind != "punct" or text != value:
            raise ValueError("expected %s, got %s" % (value, text))
        return text

    def accept_punct(self, value):
        kind, text = self.peek()
        if kind == "punct" and text == value:
            self.i += 1
            return True
        return False

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            kind, text = self.peek()
            if kind == "name" and text == "fragment":
                self.next()
                name = self.next()[1]
                self.next()  # 'on'
                type_name = self.next()[1]
                fragments[name] = (type_name, self.parse_selection_set())
            else:
                operations.append(self.parse_operation())
        return operations, fragments

    def parse_operation(self):
        kind, text = self.peek()
        op_kind = "query"
        name = None
        if kind == "name" and text in ("query", "mutation", "subscription"):
            self.next()
            op_kind = text
            if self.peek()[0] == "name":
                name = self.next()[1]
        elif kind == "punct" and text == "{":
            pass
        else:
            raise ValueError("expected an operation, got %s" % text)
        var_defs = {}
        if self.accept_punct("("):
            while not self.accept_punct(")"):
                self.expect_punct("$")
                var_name = self.next()[1]
                self.expect_punct(":")
                var_type = self.parse_type()
                default = None
                if self.accept_punct("="):
                    default = self.parse_value()
                var_defs[var_name] = (var_type, default)
        selection = self.parse_selection_set()
        return {"kind": op_kind, "name": name, "vars": var_defs,
                "selection": selection}

    def parse_type(self):
        if self.accept_punct("["):
            inner = self.parse_type()
            self.expect_punct("]")
            type_name = "[%s]" % inner
        else:
            type_name = self.next()[1]
        if self.accept_punct("!"):
            type_name += "!"
        return type_name

    def parse_selection_set(self):
        self.expect_punct("{")
        fields = []
        while not self.accept_punct("}"):
            fields.append(self.parse_field())
        return fields

    def parse_field(self):
        kind, text = self.peek()
        if kind == "ellipsis":
            # '...' fragment spread or inline fragment
            self.next()  # consumes the ellipsis token as one token
            kind2, text2 = self.peek()
            if kind2 == "name" and text2 == "on":
                self.next()
                type_name = self.next()[1]
                return {"fragment": True, "type": type_name,
                        "selection": self.parse_selection_set()}
            name = self.next()[1]
            directives = self._skip_directives()
            return {"fragment": True, "spread": name}
        alias = None
        name = self.next()[1]
        if self.accept_punct(":"):
            alias = name
            name = self.next()[1]
        args = {}
        if self.accept_punct("("):
            while not self.accept_punct(")"):
                arg_name = self.next()[1]
                self.expect_punct(":")
                args[arg_name] = self.parse_value()
        self._skip_directives()
        selection = None
        if self.peek() == ("punct", "{"):
            selection = self.parse_selection_set()
        return {"alias": alias or name, "name": name, "args": args,
                "selection": selection}

    def _skip_directives(self):
        while self.peek() == ("punct", "@"):
            self.next()
            self.next()  # directive name
            if self.accept_punct("("):
                depth = 1
                while depth:
                    kind, text = self.next()
                    if kind == "punct" and text == "(":
                        depth += 1
                    elif kind == "punct" and text == ")":
                        depth -= 1

    def parse_value(self):
        kind, text = self.next()
        if kind == "punct" and text == "$":
            return ("var", self.next()[1])
        if kind == "punct" and text == "[":
            values = []
            while not self.accept_punct("]"):
                values.append(self.parse_value())
            return ("list", values)
        if kind == "punct" and text == "{":
            obj = {}
            while not self.accept_punct("}"):
                key = self.next()[1]
                self.expect_punct(":")
                obj[key] = self.parse_value()
            return ("object", obj)
        if kind == "string":
            return ("string", json.loads(text))
        if kind == "number":
            if "." in text or "e" in text or "E" in text:
                return ("float", float(text))
            return ("int", int(text))
        if kind == "name":
            if text == "true":
                return ("bool", True)
            if text == "false":
                return ("bool", False)
            if text == "null":
                return ("null", None)
            return ("enum", text)
        raise ValueError("unexpected value token %s" % text)


def _resolve_value(value, variables):
    kind = value[0]
    if kind == "var":
        return variables.get(value[1])
    if kind == "list":
        return [_resolve_value(v, variables) for v in value[1]]
    if kind == "object":
        return {k: _resolve_value(v, variables) for k, v in value[1].items()}
    return value[1]


def _resolve_args(args, variables):
    return {k: _resolve_value(v, variables) for k, v in args.items()}


# --------------------------------------------------------------------------
# introspection data

_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "url", "createdAt",
              "updatedAt", "author", "labels", "comments", "repository"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged",
                    "headRefName", "baseRefName", "mergeable", "files",
                    "headRef", "baseRef", "author", "createdAt", "mergedAt",
                    "mergeCommit", "url"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest",
                   "description", "private", "fork", "url", "createdAt",
                   "updatedAt", "labels"],
    "User": ["id", "login", "name"],
    "Query": ["viewer", "repository", "node", "__type"],
    "Mutation": ["createIssue", "closeIssue", "createPullRequest",
                 "mergePullRequest", "addComment"],
}

_ENUM_VALUES = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "COMMIT",
                   "CODE", "WIKI", "PACKAGE"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "OrderDirection": ["ASC", "DESC"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
}


def _cursor(number):
    return base64.b64encode(("cursor:%d" % number).encode("ascii")).decode("ascii")


def _uncursor(value):
    try:
        raw = base64.b64decode(value.encode("ascii")).decode("ascii")
        if raw.startswith("cursor:"):
            return int(raw[len("cursor:"):])
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# executor

class Executor:
    def __init__(self, forge):
        self.store = forge.store
        self.forge = forge
        self.errors = []

    # -- entry points ------------------------------------------------------
    def execute(self, operation, variables, fragments):
        self.errors = []
        variables = dict(variables or {})
        for name, (_type, default) in operation["vars"].items():
            if name not in variables and default is not None:
                variables[name] = _resolve_value(default, variables)
        root_type = "Mutation" if operation["kind"] == "mutation" else "Query"
        data, _ = self._select(operation["selection"], root_type, None,
                               variables, fragments, path=[])
        result = {"data": data}
        if self.errors:
            result["errors"] = self.errors
        return result

    # -- selection machinery ------------------------------------------------
    def _select(self, selection, type_name, obj, variables, fragments, path):
        out = {}
        for field in selection:
            if field.get("fragment"):
                if "spread" in field:
                    frag = fragments.get(field["spread"])
                    if frag is None:
                        continue
                    frag_type, frag_sel = frag
                    if frag_type != type_name and not self._type_matches(
                            frag_type, type_name, obj):
                        continue
                    sub, _ = self._select(frag_sel, type_name, obj,
                                          variables, fragments, path)
                    out.update(sub)
                    continue
                frag_type = field["type"]
                if frag_type != type_name and not self._type_matches(
                        frag_type, type_name, obj):
                    continue
                sub, _ = self._select(field["selection"], type_name, obj,
                                      variables, fragments, path)
                out.update(sub)
                continue
            key = field["alias"]
            args = _resolve_args(field["args"], variables)
            value, child_type = self._resolve_field(
                type_name, obj, field["name"], args, variables, fragments,
                path + [key])
            if child_type is None and value is None and field["selection"]:
                # unknown field: error already recorded; emit null
                out[key] = None
                continue
            if field["selection"] is not None and value is not None:
                if isinstance(value, list):
                    out[key] = [self._select(field["selection"], child_type,
                                             item, variables, fragments,
                                             path + [key])[0]
                                if item is not None else None
                                for item in value]
                else:
                    sub, _ = self._select(field["selection"], child_type,
                                          value, variables, fragments,
                                          path + [key])
                    out[key] = sub
            else:
                out[key] = value
        return out, type_name

    def _type_matches(self, frag_type, type_name, obj):
        # Inline fragments like `... on Repository` on a node() result: the
        # object carries its concrete type in _gql_type.
        if isinstance(obj, dict):
            return obj.get("_gql_type") == frag_type
        return frag_type == type_name

    # -- field resolution ----------------------------------------------------
    def _unknown(self, type_name, field, path):
        self.errors.append({
            "message": 'Cannot query field "%s" on type "%s".' % (field, type_name),
            "path": list(path)})

    def _resolve_field(self, type_name, obj, field, args, variables,
                       fragments, path):
        if field == "__typename":
            return type_name, None
        handler = getattr(self, "_type_" + type_name.lower(), None)
        if handler is None:
            self._unknown(type_name, field, path)
            return None, None
        return handler(obj, field, args, variables, fragments, path)

    # -- Query ---------------------------------------------------------------
    def _type_query(self, obj, field, args, variables, fragments, path):
        store = self.store
        if field == "viewer":
            login = store.learned_login or ""
            return {"_gql_type": "User", "login": login}, "User"
        if field == "repository":
            owner = args.get("owner")
            name = args.get("name")
            if store.learned_login and owner != store.learned_login:
                return None, "Repository"
            repo = store.get_repo(owner, name) if owner and name else None
            if repo is None:
                return None, "Repository"
            return {"_gql_type": "Repository", "repo": repo}, "Repository"
        if field == "node":
            decoded = ungid(args.get("id") or "")
            if not decoded:
                return None, None
            kind, parts = decoded
            if kind == "Repository":
                repo = None
                for candidate in store.list_repos():
                    if str(candidate.rid) == parts[0]:
                        repo = candidate
                        break
                if repo is None:
                    return None, "Repository"
                return {"_gql_type": "Repository", "repo": repo}, "Repository"
            if kind == "Issue":
                repo = next((r for r in store.list_repos()
                             if str(r.rid) == parts[0]), None)
                record = store.get_issue(
                    repo.name, int(parts[1])) if repo else None
                if record is None:
                    return None, "Issue"
                return self._issue_obj(repo, record), "Issue"
            if kind == "PullRequest":
                repo = next((r for r in store.list_repos()
                             if str(r.rid) == parts[0]), None)
                record = store.get_pull(
                    repo.name, int(parts[1])) if repo else None
                if record is None:
                    return None, "PullRequest"
                return self._pull_obj(repo, record), "PullRequest"
            return None, None
        if field == "__type":
            name = args.get("name")
            fields = _TYPE_FIELDS.get(name, [])
            enum_values = _ENUM_VALUES.get(name, [])
            kind = "ENUM" if name in _ENUM_VALUES else "OBJECT"
            return {"_gql_type": "__Type", "name": name, "kind": kind,
                    "fields": [{"name": f} for f in fields],
                    "enumValues": [{"name": v} for v in enum_values]}, "__Type"
        self._unknown("Query", field, path)
        return None, None

    # -- Mutation --------------------------------------------------------------
    def _type_mutation(self, obj, field, args, variables, fragments, path):
        store = self.store
        data = args.get("input") or {}
        if field == "createIssue":
            decoded = ungid(data.get("repositoryId") or "")
            if not decoded or decoded[0] != "Repository":
                self.errors.append({"message": "repositoryId is invalid",
                                    "path": list(path)})
                return None, None
            repo = next((r for r in store.list_repos()
                         if str(r.rid) == decoded[1][0]), None)
            if repo is None:
                return None, None
            with self.forge._write_lock:
                record = store.create_issue(
                    repo.name, data.get("title") or "",
                    data.get("body") or "", [], repo.owner or "")
            return {"_gql_type": "CreateIssuePayload",
                    "issue": self._issue_obj(repo, record)}, "CreateIssuePayload"
        if field == "closeIssue":
            decoded = ungid(data.get("issueId") or "")
            if not decoded or decoded[0] != "Issue":
                self.errors.append({"message": "issueId is invalid",
                                    "path": list(path)})
                return None, None
            repo = next((r for r in store.list_repos()
                         if str(r.rid) == decoded[1][0]), None)
            record = store.get_issue(repo.name, int(decoded[1][1])) \
                if repo else None
            if record is None:
                return None, None
            with self.forge._write_lock:
                record = store.patch_issue(repo.name, record["number"],
                                           {"state": "closed"})
            return {"_gql_type": "CloseIssuePayload",
                    "issue": self._issue_obj(repo, record)}, "CloseIssuePayload"
        if field == "createPullRequest":
            decoded = ungid(data.get("repositoryId") or "")
            if not decoded or decoded[0] != "Repository":
                self.errors.append({"message": "repositoryId is invalid",
                                    "path": list(path)})
                return None, None
            repo = next((r for r in store.list_repos()
                         if str(r.rid) == decoded[1][0]), None)
            if repo is None:
                return None, None
            head = data.get("headRefName") or ""
            base = data.get("baseRefName") or "main"
            if store.get_ref(repo.name, "refs/heads/" + head) is None:
                self.errors.append(
                    {"message": "head branch %s does not exist" % head,
                     "path": list(path)})
                return None, None
            with self.forge._write_lock:
                existing = [p for p in store.list_pulls(repo.name)
                            if p["head"] == head and p["state"] == "open"]
                if existing:
                    record = existing[0]
                else:
                    record = store.create_pull(
                        repo.name, data.get("title") or "",
                        data.get("body") or "", head, base, repo.owner or "")
            return {"_gql_type": "CreatePullRequestPayload",
                    "pullRequest": self._pull_obj(repo, record)}, \
                "CreatePullRequestPayload"
        if field == "mergePullRequest":
            decoded = ungid(data.get("pullRequestId") or "")
            if not decoded or decoded[0] != "PullRequest":
                self.errors.append({"message": "pullRequestId is invalid",
                                    "path": list(path)})
                return None, None
            repo = next((r for r in store.list_repos()
                         if str(r.rid) == decoded[1][0]), None)
            record = store.get_pull(repo.name, int(decoded[1][1])) \
                if repo else None
            if record is None:
                return None, None
            if record["state"] != "open":
                self.errors.append({"message": "pull request is not open",
                                    "path": list(path)})
                return None, None
            with self.forge._write_lock:
                commit_sha, conflict = squash_merge(store, repo.name, record)
                if conflict is not None:
                    self.errors.append({"message": conflict,
                                        "path": list(path)})
                    return None, None
                from rest_v3 import EPOCH
                record = store.patch_pull(repo.name, record["number"], {
                    "state": "closed", "merged": True,
                    "merged_at": EPOCH, "merge_commit_sha": commit_sha})
            return {"_gql_type": "MergePullRequestPayload",
                    "pullRequest": self._pull_obj(repo, record)}, \
                "MergePullRequestPayload"
        if field == "addComment":
            decoded = ungid(data.get("subjectId") or "")
            if not decoded or decoded[0] != "Issue":
                self.errors.append({"message": "subjectId is invalid",
                                    "path": list(path)})
                return None, None
            repo = next((r for r in store.list_repos()
                         if str(r.rid) == decoded[1][0]), None)
            number = int(decoded[1][1])
            if repo is None or store.get_issue(repo.name, number) is None:
                return None, None
            with self.forge._write_lock:
                comment = store.create_comment(
                    repo.name, number, data.get("body") or "", repo.owner or "")
            node = {"_gql_type": "IssueComment",
                    "id": gid("IssueComment", repo.rid, number, comment["id"]),
                    "body": comment["body"]}
            return {"_gql_type": "AddCommentPayload",
                    "commentEdge": {"node": node}}, "AddCommentPayload"
        self._unknown("Mutation", field, path)
        return None, None

    # -- payload passthrough types ---------------------------------------------
    def _type_createissuepayload(self, obj, field, args, variables,
                                 fragments, path):
        return self._payload_field(obj, field, "Issue", path)

    def _type_closeissuepayload(self, obj, field, args, variables,
                                fragments, path):
        return self._payload_field(obj, field, "Issue", path)

    def _type_createpullrequestpayload(self, obj, field, args, variables,
                                       fragments, path):
        return self._payload_field(obj, field, "PullRequest", path)

    def _type_mergepullrequestpayload(self, obj, field, args, variables,
                                      fragments, path):
        return self._payload_field(obj, field, "PullRequest", path)

    def _type_addcommentpayload(self, obj, field, args, variables,
                                fragments, path):
        if field == "commentEdge":
            return obj.get("commentEdge"), "IssueCommentEdge"
        self._unknown("AddCommentPayload", field, path)
        return None, None

    def _type_issuecommentedge(self, obj, field, args, variables,
                               fragments, path):
        if field == "node":
            return obj.get("node"), "IssueComment"
        self._unknown("IssueCommentEdge", field, path)
        return None, None

    def _type_issuecomment(self, obj, field, args, variables, fragments, path):
        if field in ("id", "body"):
            return obj.get(field), None
        self._unknown("IssueComment", field, path)
        return None, None

    def _payload_field(self, obj, field, child_type, path):
        key = {"issue": "issue", "pullRequest": "pullRequest"}.get(field)
        if key and key in obj:
            return obj[key], child_type
        self._unknown("Payload", field, path)
        return None, None

    # -- object builders ---------------------------------------------------------
    def _issue_obj(self, repo, record):
        return {"_gql_type": "Issue",
                "id": gid("Issue", repo.rid, record["number"]),
                "number": record["number"], "title": record["title"],
                "body": record["body"],
                "state": "OPEN" if record["state"] == "open" else "CLOSED",
                "repo": repo, "record": record}

    def _pull_obj(self, repo, record):
        state = {"open": "OPEN", "closed": "MERGED" if record["merged"]
                 else "CLOSED"}[record["state"]]
        return {"_gql_type": "PullRequest",
                "id": gid("PullRequest", repo.rid, record["number"]),
                "number": record["number"], "title": record["title"],
                "body": record["body"], "state": state,
                "merged": record["merged"],
                "headRefName": record["head"], "baseRefName": record["base"],
                "repo": repo, "record": record}

    # -- Repository ----------------------------------------------------------------
    def _type_repository(self, obj, field, args, variables, fragments, path):
        repo = obj["repo"]
        store = self.store
        if field == "id":
            return gid("Repository", repo.rid), None
        if field == "databaseId":
            return repo.rid, None
        if field == "name":
            return repo.name, None
        if field == "nameWithOwner":
            return "%s/%s" % (repo.owner or "", repo.name), None
        if field == "owner":
            return {"_gql_type": "User", "login": repo.owner or ""}, "User"
        if field == "defaultBranchRef":
            return {"_gql_type": "Ref", "name": "main"}, "Ref"
        if field == "viewerPermission":
            return "ADMIN", None
        if field == "hasIssuesEnabled":
            return True, None
        if field == "description":
            return repo.description, None
        if field == "private":
            return repo.private, None
        if field == "issue":
            record = store.get_issue(repo.name, args.get("number"))
            if record is None:
                return None, "Issue"
            return self._issue_obj(repo, record), "Issue"
        if field == "issues":
            issues = store.list_issues(repo.name)
            states = args.get("states")
            if states:
                wanted = {"OPEN": "open", "CLOSED": "closed"}
                keep = {wanted[s] for s in states if s in wanted}
                issues = [i for i in issues if i["state"] in keep]
            order_by = args.get("orderBy") or {}
            direction = order_by.get("direction", "ASC")
            issues.sort(key=lambda i: i["number"],
                        reverse=(direction == "DESC"))
            after = args.get("after")
            if after:
                cursor = _uncursor(after)
                if cursor is not None:
                    if direction == "DESC":
                        issues = [i for i in issues if i["number"] < cursor]
                    else:
                        issues = [i for i in issues if i["number"] > cursor]
            first = args.get("first")
            total = len(issues)
            nodes = issues[:first] if isinstance(first, int) else issues
            has_next = len(nodes) < total
            end_cursor = _cursor(nodes[-1]["number"]) if nodes else None
            return {"_gql_type": "IssueConnection",
                    "nodes": [self._issue_obj(repo, i) for i in nodes],
                    "pageInfo": {"hasNextPage": has_next,
                                 "endCursor": end_cursor,
                                 "hasPreviousPage": False,
                                 "startCursor": _cursor(nodes[0]["number"])
                                 if nodes else None},
                    "totalCount": total}, "IssueConnection"
        if field == "pullRequests":
            pulls = store.list_pulls(repo.name)
            first = args.get("first")
            if isinstance(first, int):
                pulls = pulls[:first]
            return {"_gql_type": "PullRequestConnection",
                    "nodes": [self._pull_obj(repo, p) for p in pulls],
                    "totalCount": len(pulls)}, "PullRequestConnection"
        if field == "pullRequest":
            record = store.get_pull(repo.name, args.get("number"))
            if record is None:
                return None, "PullRequest"
            return self._pull_obj(repo, record), "PullRequest"
        self._unknown("Repository", field, path)
        return None, None

    # -- Issue ---------------------------------------------------------------------
    def _type_issue(self, obj, field, args, variables, fragments, path):
        if field in ("id", "number", "title", "state", "body"):
            return obj.get(field), None
        if field == "url":
            return "", None
        self._unknown("Issue", field, path)
        return None, None

    # -- PullRequest -----------------------------------------------------------------
    def _type_pullrequest(self, obj, field, args, variables, fragments, path):
        if field in ("id", "number", "title", "body", "state", "merged",
                     "headRefName", "baseRefName"):
            return obj.get(field), None
        if field == "mergeable":
            return mergeable_state(self.store, obj["repo"].name,
                                   obj["record"]), None
        if field == "files":
            files = pr_files(self.store, obj["repo"].name, obj["record"])
            first = args.get("first")
            if isinstance(first, int):
                files = files[:first]
            return {"_gql_type": "PullRequestChangedFileConnection",
                    "nodes": [{"path": f["filename"]} for f in files],
                    "totalCount": len(files)}, \
                "PullRequestChangedFileConnection"
        self._unknown("PullRequest", field, path)
        return None, None

    # -- User / Ref / connections / __Type --------------------------------------------
    def _type_user(self, obj, field, args, variables, fragments, path):
        if field == "login":
            return obj.get("login", ""), None
        if field == "id":
            return gid("User", obj.get("login", "")), None
        self._unknown("User", field, path)
        return None, None

    def _type_ref(self, obj, field, args, variables, fragments, path):
        if field == "name":
            return obj.get("name"), None
        self._unknown("Ref", field, path)
        return None, None

    def _type_issueconnection(self, obj, field, args, variables,
                              fragments, path):
        if field == "nodes":
            return obj["nodes"], "Issue"
        if field == "pageInfo":
            return obj["pageInfo"], "PageInfo"
        if field == "totalCount":
            return obj["totalCount"], None
        self._unknown("IssueConnection", field, path)
        return None, None

    def _type_pullrequestconnection(self, obj, field, args, variables,
                                    fragments, path):
        if field == "nodes":
            return obj["nodes"], "PullRequest"
        if field == "pageInfo":
            return {"hasNextPage": False, "endCursor": None,
                    "hasPreviousPage": False, "startCursor": None}, "PageInfo"
        if field == "totalCount":
            return obj["totalCount"], None
        self._unknown("PullRequestConnection", field, path)
        return None, None

    def _type_pullrequestchangedfileconnection(self, obj, field, args,
                                               variables, fragments, path):
        if field == "nodes":
            return obj["nodes"], "PullRequestChangedFile"
        if field == "totalCount":
            return obj["totalCount"], None
        self._unknown("PullRequestChangedFileConnection", field, path)
        return None, None

    def _type_pullrequestchangedfile(self, obj, field, args, variables,
                                     fragments, path):
        if field == "path":
            return obj.get("path"), None
        self._unknown("PullRequestChangedFile", field, path)
        return None, None

    def _type_pageinfo(self, obj, field, args, variables, fragments, path):
        if field in ("hasNextPage", "endCursor", "hasPreviousPage",
                     "startCursor"):
            return obj.get(field), None
        self._unknown("PageInfo", field, path)
        return None, None

    def _type___type(self, obj, field, args, variables, fragments, path):
        if field == "name":
            return obj.get("name"), None
        if field == "kind":
            return obj.get("kind"), None
        if field == "fields":
            return obj.get("fields"), "__Field"
        if field == "enumValues":
            return obj.get("enumValues"), "__EnumValue"
        self._unknown("__Type", field, path)
        return None, None

    def _type___field(self, obj, field, args, variables, fragments, path):
        if field == "name":
            return obj.get("name"), None
        self._unknown("__Field", field, path)
        return None, None

    def _type___enumvalue(self, obj, field, args, variables, fragments, path):
        if field == "name":
            return obj.get("name"), None
        self._unknown("__EnumValue", field, path)
        return None, None


# --------------------------------------------------------------------------
# HTTP entry point

def handle_graphql(handler):
    """POST /api/graphql. Auth is checked by the caller before this runs."""
    raw = handler._read_body()
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        handler._send_json(400, {"message": "Malformed JSON"})
        return True
    executor = Executor(handler.forge)
    operations = body if isinstance(body, list) else [body]
    results = []
    for item in operations:
        if not isinstance(item, dict) or not isinstance(
                item.get("query"), str):
            results.append({"data": None, "errors": [
                {"message": "A query document is required"}]})
            continue
        try:
            parsed_ops, fragments = _Parser(item["query"]).parse_document()
        except ValueError as exc:
            results.append({"data": None, "errors": [
                {"message": "GraphQL syntax error: %s" % exc}]})
            continue
        operation = parsed_ops[0]
        name = item.get("operationName")
        if name:
            for candidate in parsed_ops:
                if candidate["name"] == name:
                    operation = candidate
                    break
        variables = item.get("variables") or {}
        if not isinstance(variables, dict):
            variables = {}
        results.append(executor.execute(operation, variables, fragments))
    if isinstance(body, list):
        handler._send_json(200, results)
    else:
        handler._send_json(200, results[0])
    return True
