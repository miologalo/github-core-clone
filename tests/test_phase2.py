"""Phase 2 test driver: drives REST v3, GraphQL v4 and MCP against a
running forge (in-process server on an ephemeral port) and reports
pass/fail per check. Run: python3 tests/test_phase2.py
"""

import base64
import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["MAKEFUN_SHOP_TOKEN"] = "test-token-123"
DATA_DIR = "/tmp/phase2-test-data"
shutil.rmtree(DATA_DIR, ignore_errors=True)

from server import serve  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, token="test-token-123",
             raw=None, headers=None):
        url = self.base + path
        data = raw
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = "Bearer " + token
        if headers:
            hdrs.update(headers)
        if data is None and body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_body = resp.read()
                return resp.status, dict(resp.headers), raw_body
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()
        except Exception as exc:  # noqa: BLE001
            return 0, {}, str(exc).encode()

    def json(self, method, path, body=None, **kw):
        status, headers, raw = self.call(method, path, body, **kw)
        try:
            parsed = json.loads(raw.decode()) if raw else None
        except ValueError:
            parsed = None
        return status, headers, parsed


def gql(client, query, variables=None):
    return client.json("POST", "/api/graphql",
                       {"query": query, "variables": variables or {}})


def mcp(client, method, params=None, req_id=1, token="test-token-123"):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params or {}}
    return client.json("POST", "/mcp", body, token=token)


def main():
    server, _forge = serve(DATA_DIR, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    c = Client("http://127.0.0.1:%d" % port)
    owner = "octo"

    # ---- REST root/meta/auth -------------------------------------------
    s, h, b = c.json("GET", "/api/v3/", token=None)
    check("rest root 401 without token", s == 401 and isinstance(b, dict) and b.get("message"))
    s, h, b = c.json("GET", "/api/v3/")
    scopes = {k.lower(): v for k, v in h.items()}.get("x-oauth-scopes", "")
    rid = {k.lower(): v for k, v in h.items()}.get("x-github-request-id", "")
    check("rest root 200 + scopes", s == 200 and "repo" in scopes and rid)
    s, h, b = c.json("GET", "/api/v3/meta")
    check("rest meta installed_version", s == 200 and b.get("installed_version"))
    s, h, b = c.json("GET", "/api/v3/nope")
    check("rest unknown route 404 json", s == 404 and b.get("message") and h.get("Content-Type", "").startswith("application/json"))
    s, h, b = c.call("POST", "/api/v3/user/repos", raw=b'{"name":')
    check("rest malformed json 400", s == 400)

    # ---- repos ----------------------------------------------------------
    s, h, b = c.json("POST", "/api/v3/user/repos", {"name": "demo", "private": True, "description": "d1"})
    check("rest create repo 201", s == 201 and b.get("name") == "demo" and b.get("id") == 1, str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo" % owner)
    check("rest get repo 200 + binds owner", s == 200 and b.get("full_name") == "octo/demo", str(b))
    s, h, b = c.json("GET", "/api/v3/repos/stranger/demo")
    check("rest foreign owner 404", s == 404)
    s, h, b = c.json("GET", "/api/v3/user")
    check("rest get user login", s == 200 and b.get("login") == "octo", str(b))
    s, h, b = c.json("PATCH", "/api/v3/repos/%s/demo" % owner, {"description": "d2"})
    check("rest patch repo description", s == 200 and b.get("description") == "d2", str(b))
    for i in range(11):
        c.json("POST", "/api/v3/user/repos", {"name": "page-%02d" % i})
    s, h, b = c.json("GET", "/api/v3/user/repos?per_page=10")
    check("rest pagination page1 10 + next link", s == 200 and len(b) == 10 and 'rel="next"' in h.get("Link", ""), str(h.get("Link")))
    nxt = h.get("Link", "").split(";")[0].strip("<> ")
    s, h, b = c.json("GET", nxt.replace(c.base, ""))
    check("rest pagination page2", s == 200 and len(b) >= 2, str(len(b) if isinstance(b, list) else b))

    # ---- contents --------------------------------------------------------
    def put(path, content, sha=None, branch=None, message="c"):
        body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
        if sha:
            body["sha"] = sha
        if branch:
            body["branch"] = branch
        return c.json("PUT", "/api/v3/repos/%s/demo/contents/%s" % (owner, path), body)

    s, h, b = put("README.md", "hello\n")
    check("rest contents create 201", s == 201 and b["commit"]["sha"] and b["content"]["sha"], str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/contents/README.md" % owner)
    blob_sha = b.get("sha") if isinstance(b, dict) else None
    check("rest contents get base64", s == 200 and base64.b64decode(b["content"]).decode() == "hello\n" and b["encoding"] == "base64", str(b)[:200])
    s, h, b = put("README.md", "hello2\n", sha=blob_sha)
    check("rest contents update 200", s in (200, 201) and b["content"]["sha"] != blob_sha, str(b)[:200])
    s, h, b = put("README.md", "stale\n", sha=blob_sha)
    check("rest contents stale sha 409", s == 409, str(s))
    s, h, b = put("docs/a.txt", "a\n")
    check("rest contents nested create", s == 201, str(b)[:200])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/contents/docs" % owner)
    check("rest contents directory listing", s == 200 and isinstance(b, list) and any(e["name"] == "a.txt" for e in b), str(b)[:200])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/contents/README.md" % owner)
    cur_sha = b["sha"]
    s, h, b = c.json("DELETE", "/api/v3/repos/%s/demo/contents/README.md" % owner, {"message": "rm", "sha": cur_sha})
    check("rest contents delete 200", s == 200 and b["commit"]["sha"], str(b)[:200])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/contents/README.md" % owner)
    check("rest contents get after delete 404", s == 404)

    # ---- labels / issues ---------------------------------------------------
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/labels" % owner, {"name": "bug", "color": "ff0000"})
    check("rest label create 201", s == 201 and b.get("name") == "bug", str(b))
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/issues" % owner, {"title": "t1", "body": "b1", "labels": ["nope"]})
    check("rest issue bad label 422", s == 422, str(s))
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/issues" % owner, {"title": "t1", "body": "b1", "labels": ["bug"]})
    check("rest issue create 201 open", s == 201 and b.get("state") == "open" and b.get("number") == 1 and b["labels"][0]["name"] == "bug", str(b))
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/issues" % owner, {"title": "t2", "body": "b2"})
    check("rest issue numbers increment", s == 201 and b.get("number") == 2, str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues?state=open&sort=created&direction=asc" % owner)
    check("rest issues list asc", s == 200 and [i["number"] for i in b] == [1, 2], str(b)[:200])
    s, h, b = c.json("PATCH", "/api/v3/repos/%s/demo/issues/2" % owner, {"state": "closed"})
    check("rest issue patch close", s == 200 and b.get("state") == "closed", str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues?state=open" % owner)
    check("rest issues list open excludes closed", s == 200 and [i["number"] for i in b] == [1], str(b)[:200])
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/issues/1/comments" % owner, {"body": "hello comment"})
    check("rest comment create 201", s == 201 and b.get("body") == "hello comment" and b.get("id"), str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues/1/comments" % owner)
    check("rest comments list", s == 200 and len(b) == 1 and b[0]["body"] == "hello comment", str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues/1" % owner)
    check("rest issue get one (read workload shape)", s == 200 and b.get("title") == "t1", str(b)[:200])

    # ---- branches / commits / pulls ---------------------------------------
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/commits" % owner)
    check("rest commits list", s == 200 and len(b) >= 1 and b[0].get("sha"), str(b)[:200])
    base_sha = b[0]["sha"]
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/git/refs" % owner, {"ref": "refs/heads/feature", "sha": base_sha})
    check("rest git refs create branch 201", s == 201 and b.get("ref") == "refs/heads/feature", str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/branches" % owner)
    check("rest branches list", s == 200 and {"main", "feature"} <= {x["name"] for x in b}, str(b))
    s, h, b = put("docs/a.txt", "a2\n", sha=None, branch="feature", message="feature change")
    # need current blob sha on feature for update
    s2, _, b2 = c.json("GET", "/api/v3/repos/%s/demo/contents/docs/a.txt?ref=feature" % owner)
    s, h, b = put("docs/a.txt", "a2\n", sha=b2["sha"], branch="feature", message="feature change")
    check("rest contents update on branch", s in (200, 201), str(b)[:200])
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/pulls" % owner, {"title": "pr1", "body": "pb", "head": "feature", "base": "main"})
    pr1 = b.get("number") if isinstance(b, dict) else None
    check("rest pull create 201 open", s == 201 and b.get("state") == "open" and b["head"]["ref"] == "feature" and b["base"]["ref"] == "main", str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/pulls/%s/files" % (owner, pr1))
    check("rest pull files", s == 200 and any(f["filename"] == "docs/a.txt" and f["additions"] >= 1 for f in b), str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/pulls/999999" % owner)
    check("rest pull get unknown 404", s == 404)
    s, h, b = c.json("PUT", "/api/v3/repos/%s/demo/pulls/999999/merge" % owner, {"merge_method": "squash"})
    check("rest pull merge unknown 404", s == 404)
    s, h, b = c.json("PUT", "/api/v3/repos/%s/demo/pulls/%s/merge" % (owner, pr1), {"merge_method": "squash"})
    merge_sha = b.get("sha") if isinstance(b, dict) else None
    check("rest pull merge 200", s == 200 and b.get("merged") is True and merge_sha, str(b))
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/pulls/%s" % (owner, pr1))
    check("rest pull merged closed + merge_commit_sha", s == 200 and b.get("merged") is True and b.get("state") == "closed" and b.get("merge_commit_sha") == merge_sha and b.get("merged_at"), str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/contents/docs/a.txt?ref=main" % owner)
    check("rest squash content on default branch", s == 200 and base64.b64decode(b["content"]).decode() == "a2\n", str(b)[:200])

    # conflict: two branches change same file differently
    for br, content in (("conf-a", "aaa\n"), ("conf-b", "bbb\n")):
        c.json("POST", "/api/v3/repos/%s/demo/git/refs" % owner, {"ref": "refs/heads/" + br, "sha": merge_sha})
        s2, _, b2 = c.json("GET", "/api/v3/repos/%s/demo/contents/docs/a.txt?ref=%s" % (owner, br))
        c.json("PUT", "/api/v3/repos/%s/demo/contents/docs/a.txt" % owner,
               {"message": "m", "content": base64.b64encode(content.encode()).decode(), "sha": b2["sha"], "branch": br})
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/pulls" % owner, {"title": "pa", "body": "", "head": "conf-a", "base": "main"})
    pra = b["number"]
    s, h, b = c.json("POST", "/api/v3/repos/%s/demo/pulls" % owner, {"title": "pb", "body": "", "head": "conf-b", "base": "main"})
    prb = b["number"]
    s, h, b = c.json("PUT", "/api/v3/repos/%s/demo/pulls/%s/merge" % (owner, pra), {"merge_method": "squash"})
    check("rest merge first of conflicting pair 200", s == 200, str(b))
    s, h, b = c.json("PUT", "/api/v3/repos/%s/demo/pulls/%s/merge" % (owner, prb), {"merge_method": "squash"})
    check("rest merge conflicted 405", s == 405, str(s))

    # delete repo
    s, h, b = c.json("POST", "/api/v3/user/repos", {"name": "todelete"})
    s, h, b = c.json("DELETE", "/api/v3/repos/%s/todelete" % owner)
    check("rest delete repo 204", s == 204, str(s))
    s, h, b = c.json("GET", "/api/v3/repos/%s/todelete" % owner)
    check("rest get after delete 404", s == 404)

    # ---- GraphQL ------------------------------------------------------------
    s, h, b = gql(c, "query ViewerLogin { viewer { login } }")
    check("gql viewer login", s == 200 and b["data"]["viewer"]["login"] == "octo", str(b)[:200])
    s, h, b = c.json("POST", "/api/graphql", {"query": "query { viewer { login } }"}, token=None)
    check("gql unauthenticated 401", s == 401, str(s))
    s, h, b = gql(c, 'query Issue_fields { Issue: __type(name: "Issue") { fields(includeDeprecated: true) { name } } }')
    names = [f["name"] for f in b["data"]["Issue"]["fields"]]
    check("gql introspect Issue", s == 200 and {"id", "number", "title", "state", "body"} <= set(names), str(names))
    s, h, b = gql(c, 'query T { PullRequest: __type(name: "PullRequest") { fields(includeDeprecated: true) { name } } Repository: __type(name: "Repository") { fields(includeDeprecated: true) { name } } }')
    prn = [f["name"] for f in b["data"]["PullRequest"]["fields"]]
    rpn = [f["name"] for f in b["data"]["Repository"]["fields"]]
    check("gql introspect PR+Repo", {"id", "number", "headRefName", "baseRefName", "mergeable", "state", "merged", "files"} <= set(prn) and {"id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef", "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequests", "pullRequest"} <= set(rpn), str((prn, rpn)))
    s, h, b = gql(c, 'query S { SearchType: __type(name: "SearchType") { enumValues(includeDeprecated: true) { name } } }')
    check("gql introspect SearchType ISSUE", "ISSUE" in [e["name"] for e in b["data"]["SearchType"]["enumValues"]], str(b)[:200])
    s, h, b = gql(c, """query OpenIssues($owner: String!, $name: String!, $first: Int!) {
      repository(owner: $owner, name: $name) {
        issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { number title state id } } } }""",
      {"owner": owner, "name": "demo", "first": 10})
    nums = [n["number"] for n in b["data"]["repository"]["issues"]["nodes"]]
    check("gql open issues DESC, closed omitted", nums == [1] and b["data"]["repository"]["issues"]["nodes"][0]["state"] == "OPEN", str(nums))
    s, h, b = gql(c, """query RepositoryFields($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) { id databaseId name owner { login } defaultBranchRef { name } viewerPermission hasIssuesEnabled } }""",
      {"owner": owner, "name": "demo"})
    r = b["data"]["repository"]
    check("gql repository fields", r["databaseId"] == 1 and r["name"] == "demo" and r["owner"]["login"] == "octo" and r["defaultBranchRef"]["name"] == "main" and r["viewerPermission"] == "ADMIN" and r["hasIssuesEnabled"] is True and r["id"] != "1", str(r))
    repo_id = r["id"]
    s, h, b = gql(c, """query IssueByNumber($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) { issue(number: $number) { number title state id body } } }""",
      {"owner": owner, "name": "demo", "number": 1})
    check("gql issue by number", b["data"]["repository"]["issue"]["title"] == "t1" and b["data"]["repository"]["issue"]["body"] == "b1", str(b)[:200])
    issue_id = b["data"]["repository"]["issue"]["id"]
    s, h, b = gql(c, """mutation IssueCreate($input: CreateIssueInput!) {
      createIssue(input: $input) { issue { number id } } }""",
      {"input": {"repositoryId": repo_id, "title": "gql-issue", "body": "gb"}})
    check("gql createIssue", s == 200 and b["data"]["createIssue"]["issue"]["number"] >= 3, str(b)[:200])
    gql_num = b["data"]["createIssue"]["issue"]["number"]
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues/%d" % (owner, gql_num))
    check("gql createIssue visible over REST", s == 200 and b["title"] == "gql-issue", str(b)[:200])
    s, h, b = gql(c, """mutation IssueClose($input: CloseIssueInput!) {
      closeIssue(input: $input) { issue { number id state } } }""",
      {"input": {"issueId": issue_id}})
    check("gql closeIssue", b["data"]["closeIssue"]["issue"]["state"] == "CLOSED", str(b)[:200])
    s, h, b = gql(c, """query PullRequests($owner: String!, $name: String!, $first: Int!) {
      repository(owner: $owner, name: $name) { pullRequests(first: $first) { nodes { number headRefName baseRefName mergeable state } } } }""",
      {"owner": owner, "name": "demo", "first": 5})
    check("gql pullRequests list", s == 200 and isinstance(b["data"]["repository"]["pullRequests"]["nodes"], list), str(b)[:200])
    s, h, b = gql(c, """query PF($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) { pullRequest(number: $number) { number files(first: 10) { nodes { path } } } } }""",
      {"owner": owner, "name": "demo", "number": pr1})
    paths = [n["path"] for n in b["data"]["repository"]["pullRequest"]["files"]["nodes"]]
    check("gql pullRequest files", "docs/a.txt" in paths, str(paths))
    # createPullRequest over GraphQL from a fresh git-pushed branch: use REST branch
    s2, _, b2 = c.json("GET", "/api/v3/repos/%s/demo/commits" % owner)
    c.json("POST", "/api/v3/repos/%s/demo/git/refs" % owner, {"ref": "refs/heads/gql-head", "sha": b2[0]["sha"]})
    s2, _, b2 = c.json("GET", "/api/v3/repos/%s/demo/contents/docs/a.txt?ref=gql-head" % owner)
    c.json("PUT", "/api/v3/repos/%s/demo/contents/docs/a.txt" % owner,
           {"message": "g", "content": base64.b64encode(b"gql\n").decode(), "sha": b2["sha"], "branch": "gql-head"})
    s, h, b = gql(c, """mutation PullRequestCreate($input: CreatePullRequestInput!) {
      createPullRequest(input: $input) { pullRequest { number id state headRefName baseRefName } } }""",
      {"input": {"repositoryId": repo_id, "baseRefName": "main", "headRefName": "gql-head", "title": "gql pr"}})
    check("gql createPullRequest", s == 200 and b["data"]["createPullRequest"]["pullRequest"]["headRefName"] == "gql-head" and b["data"]["createPullRequest"]["pullRequest"]["state"] == "OPEN", str(b)[:300])
    gql_pr = b["data"]["createPullRequest"]["pullRequest"]["number"]
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/pulls/%d" % (owner, gql_pr))
    check("gql createPullRequest visible over REST", s == 200 and b["head"]["ref"] == "gql-head" and b["base"]["ref"] == "main", str(b)[:200])
    # mergePullRequest over GraphQL
    s, h, b = gql(c, """query PRId($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) { pullRequest(number: $number) { id number } } }""",
      {"owner": owner, "name": "demo", "number": gql_pr})
    pr_gid = b["data"]["repository"]["pullRequest"]["id"]
    s, h, b = gql(c, """mutation MergePullRequest($input: MergePullRequestInput!) {
      mergePullRequest(input: $input) { pullRequest { number merged state } } }""",
      {"input": {"pullRequestId": pr_gid, "mergeMethod": "SQUASH"}})
    check("gql mergePullRequest", s == 200 and b["data"]["mergePullRequest"]["pullRequest"]["merged"] is True, str(b)[:300])
    s, h, b = gql(c, """mutation AddComment($input: AddCommentInput!) {
      addComment(input: $input) { commentEdge { node { id body } } } }""",
      {"input": {"subjectId": issue_id, "body": "gql comment"}})
    check("gql addComment", s == 200 and b["data"]["addComment"]["commentEdge"]["node"]["body"] == "gql comment", str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/demo/issues/1/comments" % owner)
    check("gql addComment visible over REST", any(cm["body"] == "gql comment" for cm in b), str(b))
    s, h, b = gql(c, "query ViewerInvalidField { viewer { login noSuchFieldOnViewer } }")
    check("gql invalid field 200 + errors naming it", s == 200 and b.get("errors") and "noSuchFieldOnViewer" in str(b["errors"]), str(b)[:300])
    s, h, b = gql(c, """query NodeRepository($id: ID!) { node(id: $id) { id ... on Repository { name nameWithOwner } } }""",
                  {"id": repo_id})
    check("gql node(id:) repository", b["data"]["node"]["name"] == "demo" and b["data"]["node"]["nameWithOwner"] == "octo/demo", str(b)[:200])
    s, h, b = gql(c, """query RepositoryFields($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) { id name } }""",
      {"owner": owner, "name": "missing-repo"})
    check("gql unknown repo null no errors", b["data"]["repository"] is None and not b.get("errors"), str(b)[:200])
    s, h, b = c.json("POST", "/api/graphql", [{"query": "query { viewer { login } }"}, {"query": 'query { Issue: __type(name: "Issue") { fields { name } } }'}])
    check("gql batch two ops in order", s == 200 and isinstance(b, list) and len(b) == 2 and b[0]["data"]["viewer"]["login"] == "octo" and b[1]["data"]["Issue"]["fields"], str(b)[:200])
    # pagination: create 7 issues in a fresh repo
    c.json("POST", "/api/v3/user/repos", {"name": "pages"})
    for i in range(7):
        c.json("POST", "/api/v3/repos/%s/pages/issues" % owner, {"title": "p%d" % i, "body": ""})
    s, h, b = gql(c, """query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) {
      repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }""",
      {"owner": owner, "name": "pages", "first": 5, "after": None})
    p1 = b["data"]["repository"]["issues"]
    s, h, b = gql(c, """query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) {
      repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }""",
      {"owner": owner, "name": "pages", "first": 5, "after": p1["pageInfo"]["endCursor"]})
    p2 = b["data"]["repository"]["issues"]
    check("gql issues cursor pagination 5+2", len(p1["nodes"]) == 5 and p1["pageInfo"]["hasNextPage"] is True and len(p2["nodes"]) == 2 and p2["pageInfo"]["hasNextPage"] is False, str((p1, p2))[:300])

    # ---- MCP ---------------------------------------------------------------
    s, h, b = mcp(c, "server/discover", {}, req_id="discover-1")
    check("mcp server/discover supportedVersions", s == 200 and b.get("result", {}).get("supportedVersions") and "2026-07-28" in b["result"]["supportedVersions"], str(b)[:300])
    s, h, b = mcp(c, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}, req_id="initialize-1")
    check("mcp initialize legacy", s == 200 and b.get("result", {}).get("serverInfo", {}).get("name") and b["result"]["capabilities"].get("tools") is not None, str(b)[:300])
    s, h, b = mcp(c, "tools/list", {}, req_id=2)
    tools = b.get("result", {}).get("tools", [])
    names = [t["name"] for t in tools]
    check("mcp tools/list six required", len(tools) >= 6 and {"create_repo", "get_repo", "create_issue", "list_issues", "create_pull_request", "merge_pull_request"} <= set(names), str(names))
    check("mcp tool schemas", all(t.get("description") and t.get("inputSchema", {}).get("type") == "object" for t in tools), str(tools)[:200])
    s2, h2, b2 = mcp(c, "tools/list", {}, req_id=3)
    check("mcp tools/list idempotent bytes", json.dumps(b["result"]["tools"], sort_keys=True) == json.dumps(b2["result"]["tools"], sort_keys=True))
    s, h, b = mcp(c, "tools/call", {"name": "create_repo", "arguments": {"name": "mcp-repo", "owner": owner}}, req_id=4)
    check("mcp create_repo", s == 200 and not b["result"].get("isError") and "mcp-repo" in b["result"]["content"][0]["text"], str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/mcp-repo" % owner)
    check("mcp create_repo visible over REST same id", s == 200 and b.get("name") == "mcp-repo", str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "get_repo", "arguments": {"owner": owner, "repo": "mcp-repo"}}, req_id=5)
    check("mcp get_repo", s == 200 and "mcp-repo" in b["result"]["content"][0]["text"], str(b)[:300])
    s, h, b = mcp(c, "tools/call", {"name": "create_issue", "arguments": {"owner": owner, "repo": "mcp-repo", "title": "mcp issue", "body": "mb"}}, req_id=6)
    check("mcp create_issue", s == 200 and not b["result"].get("isError") and "mcp issue" in b["result"]["content"][0]["text"], str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/mcp-repo/issues" % owner)
    check("mcp create_issue visible over REST", s == 200 and any(i["title"] == "mcp issue" for i in b), str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "list_issues", "arguments": {"owner": owner, "repo": "mcp-repo", "state": "open"}}, req_id=7)
    check("mcp list_issues returns the issue", s == 200 and "mcp issue" in b["result"]["content"][0]["text"], str(b)[:300])
    # PR over MCP: base commit + branch via REST
    c.json("PUT", "/api/v3/repos/%s/mcp-repo/contents/f.txt" % owner,
           {"message": "base", "content": base64.b64encode(b"base\n").decode()})
    s, h, b = c.json("GET", "/api/v3/repos/%s/mcp-repo/commits" % owner)
    tip = b[0]["sha"]
    c.json("POST", "/api/v3/repos/%s/mcp-repo/git/refs" % owner, {"ref": "refs/heads/mcp-head", "sha": tip})
    s2, _, b2 = c.json("GET", "/api/v3/repos/%s/mcp-repo/contents/f.txt?ref=mcp-head" % owner)
    c.json("PUT", "/api/v3/repos/%s/mcp-repo/contents/f.txt" % owner,
           {"message": "h", "content": base64.b64encode(b"head\n").decode(), "sha": b2["sha"], "branch": "mcp-head"})
    s, h, b = mcp(c, "tools/call", {"name": "create_pull_request", "arguments": {"owner": owner, "repo": "mcp-repo", "title": "mcp pr", "head": "mcp-head", "base": "main"}}, req_id=8)
    check("mcp create_pull_request", s == 200 and not b["result"].get("isError") and "mcp-head" in b["result"]["content"][0]["text"], str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/mcp-repo/pulls" % owner)
    pr_num = b[0]["number"] if b else None
    check("mcp create_pull_request visible over REST", pr_num is not None and b[0]["head"]["ref"] == "mcp-head" and b[0]["base"]["ref"] == "main", str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "merge_pull_request", "arguments": {"owner": owner, "repo": "mcp-repo", "pull_number": pr_num}}, req_id=9)
    check("mcp merge_pull_request", s == 200 and not b["result"].get("isError"), str(b)[:300])
    s, h, b = c.json("GET", "/api/v3/repos/%s/mcp-repo/pulls/%d" % (owner, pr_num))
    check("mcp merge visible over REST merged:true", s == 200 and b.get("merged") is True, str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "no_such_tool_x", "arguments": {}}, req_id=10)
    check("mcp unknown tool -32602/-32601", s == 200 and b.get("error", {}).get("code") in (-32602, -32601), str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "create_issue", "arguments": {}}, req_id=11)
    check("mcp missing args isError or -32602", s == 200 and (b["result"].get("isError") is True or b.get("error", {}).get("code") == -32602), str(b)[:200])
    s, h, b = c.call("POST", "/mcp", raw=b'{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"')
    check("mcp parse error 400 -32700", s == 400 and json.loads(b).get("error", {}).get("code") == -32700, str(s) + str(b)[:200])
    s, h, b = mcp(c, "tools/call", {"name": "create_issue", "arguments": {"owner": "x", "repo": "y", "title": "t"}}, req_id=12, token=None)
    check("mcp no auth 401", s == 401, str(s))
    # concurrent calls keep their ids
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(mcp, c, "tools/call", {"name": "get_repo", "arguments": {"owner": owner, "repo": "demo"}}, "cc-left")
        f2 = pool.submit(mcp, c, "tools/call", {"name": "get_repo", "arguments": {"owner": owner, "repo": "mcp-repo"}}, "cc-right")
        r1 = f1.result()
        r2 = f2.result()
    check("mcp concurrent calls own ids/objects", r1[2].get("id") == "cc-left" and r2[2].get("id") == "cc-right" and "demo" in r1[2]["result"]["content"][0]["text"] and "mcp-repo" in r2[2]["result"]["content"][0]["text"], str((r1[2], r2[2]))[:300])

    server.shutdown()
    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d passed, %d failed" % (len(RESULTS) - len(failed), len(RESULTS), len(failed)))
    for name, _ok, detail in failed:
        print("FAILED: " + name + " — " + detail)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
