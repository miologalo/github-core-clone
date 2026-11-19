# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/miologalo/github-core-clone/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/miologalo/github-core-clone/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge, written in Python 3.13 with the standard
library only — no git binary, no packages, no outside service at runtime.
Built for [github-core-clone-01](https://make.fun/bounty/github-core-clone-01)
on make.fun.

## What it does (Phase 1)

- **Git core**: loose objects (blob/tree/commit/tag, SHA-1, zlib), packfile
  read/write (pack idx v2 semantics, OFS_DELTA + REF_DELTA with delta
  apply, thin-pack bases from the store), ref storage, fast-forward-only
  updates (`ng <ref> non-fast-forward` per refused ref).
- **Smart HTTP** at `/{owner}/{repo}.git`: `info/refs` for both services,
  `git-upload-pack` protocol v2 (capability advertisement, `ls-refs` with
  peel/symrefs/unborn/ref-prefix, shallow fetch and deepen) with a v0
  fallback, and `git-receive-pack` v0 with `report-status`,
  `side-band-64k` and `delete-refs`. A real git client can clone from and
  push to it.
- **Lifecycle**: binds an ephemeral port, `POST`s `{"port": N}` to
  `$MAKEFUN_ENV_URL/register`, long-polls `GET $MAKEFUN_ENV_URL/done`,
  exits 0 when done. Without `MAKEFUN_ENV_URL` it serves locally.
- **Store**: one data directory (`./data`, or `FORGE_DATA_DIR`) of sorted
  JSON files plus loose objects; no timestamps or random ids in exports.
  Threaded request handling, serialized writes.
- **Auth**: `Authorization: Bearer $MAKEFUN_SHOP_TOKEN` on every route
  except `/healthz` (missing/wrong token is 401); the same repo name under
  another owner is 404, never 403.
- **Minimal repo REST** (Phase 1 only, so git has somewhere to push):
  `POST /api/v3/user/repos`, `GET /api/v3/user/repos`,
  `GET /api/v3/repos/{owner}/{repo}`, `GET /api/v3/`.

Phase 2 adds the full REST v3 surface, GraphQL v4 and MCP.

## Run it

```sh
python3 main.py                 # local dev: serves on an ephemeral port
python3 main.py --port 9000     # local dev on a fixed port
FORGE_DATA_DIR=/tmp/forge-data python3 main.py
```

In a trial the suite sets `MAKEFUN_ENV_URL` and `MAKEFUN_SHOP_TOKEN` and
the program registers itself and exits when the suite says done.

## Test it

```sh
python3 tests/test_forge.py
```

Starts the forge on a scratch data dir, creates a repo over REST, then
drives it with the system git client: empty clone, push, clone back,
shallow clone (depth 1), and a non-fast-forward refusal.

## Layout

| File | Job |
| --- | --- |
| `main.py` | lifecycle: bind, register, poll done, exit 0 |
| `server.py` | HTTP routing, auth, `/healthz`, minimal repo REST |
| `smart_http.py` | git smart HTTP: advertisements, ls-refs, fetch, receive-pack |
| `store.py` | objects, refs, repos, persistence, reachability/shallow walks |
| `packfile.py` | packfile read/write, pkt-line framing |
| `git_objects.py` | object ids, commit/tree/tag parsing, delta apply |

## License

MIT — see `LICENSE`.
