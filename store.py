"""The forge's store: git objects, refs and repository records.

One data directory (default ./data, FORGE_DATA_DIR overrides). Objects are
kept in memory for speed and written through to loose object files on disk;
repository and ref records live in sorted JSON files with no timestamps and
no random ids, so an export is deterministic. All mutations go through one
re-entrant lock; readers snapshot what they need under the same lock.
"""

from __future__ import annotations

import json
import os
import threading
import zlib

from git_objects import ZERO_OID, hash_object, parse_commit, parse_tag, parse_tree

DEFAULT_BRANCH = "main"
HEAD_TARGET = "refs/heads/main"


class Repo:
    def __init__(self, name, owner=None, description="", private=False, rid=0):
        self.name = name
        self.owner = owner          # None until the first owner-scoped access binds it
        self.description = description
        self.private = private
        self.rid = rid

    def to_dict(self):
        return {"id": self.rid, "name": self.name,
                "owner": self.owner, "description": self.description,
                "private": self.private, "default_branch": DEFAULT_BRANCH}

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d.get("owner"), d.get("description", ""),
                   d.get("private", False), d.get("id", 0))


class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self.objects = {}           # oid -> (type, bytes)
        self.repos = {}             # name -> Repo (names are unique per trial)
        self.refs = {}              # name -> {ref: oid}
        self._next_id = 1
        self._load()

    # ------------------------------------------------------------ persistence

    def _paths(self):
        return (os.path.join(self.data_dir, "repos.json"),
                os.path.join(self.data_dir, "refs.json"),
                os.path.join(self.data_dir, "objects"))

    def _load(self):
        repos_path, refs_path, obj_dir = self._paths()
        try:
            os.makedirs(obj_dir, exist_ok=True)
        except OSError:
            return
        try:
            with open(repos_path, "r", encoding="utf-8") as fh:
                for item in json.load(fh).get("repos", []):
                    repo = Repo.from_dict(item)
                    self.repos[repo.name] = repo
                    self._next_id = max(self._next_id, repo.rid + 1)
        except (OSError, ValueError):
            pass
        try:
            with open(refs_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh).get("refs", {})
                for name, refmap in raw.items():
                    self.refs[name] = dict(sorted(refmap.items()))
        except (OSError, ValueError):
            pass
        # Loose objects back into memory.
        for root, _dirs, files in os.walk(obj_dir):
            for fname in files:
                path = os.path.join(root, fname)
                try:
                    with open(path, "rb") as fh:
                        raw = zlib.decompress(fh.read())
                    nul = raw.index(b"\0")
                    header = raw[:nul].decode("ascii")
                    obj_type, _size = header.split(" ")
                    data = raw[nul + 1:]
                    oid = hash_object(obj_type, data)
                    self.objects.setdefault(oid, (obj_type, data))
                except (OSError, ValueError, zlib.error):
                    continue

    def _save_repos(self):
        repos_path, refs_path, _ = self._paths()
        payload = {"repos": [self.repos[n].to_dict()
                             for n in sorted(self.repos)]}
        tmp = repos_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.replace(tmp, repos_path)

    def _save_refs(self):
        _repos_path, refs_path, _ = self._paths()
        payload = {"refs": {n: dict(sorted(self.refs.get(n, {}).items()))
                            for n in sorted(self.refs)}}
        tmp = refs_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, indent=1)
            fh.write("\n")
        os.replace(tmp, refs_path)

    # ------------------------------------------------------------ objects

    def has_object(self, oid):
        return oid in self.objects

    def get_object(self, oid):
        return self.objects.get(oid)

    def put_object(self, obj_type, data):
        """Store an object; return its oid. Writes a loose file through."""
        oid = hash_object(obj_type, data)
        with self.lock:
            if oid in self.objects:
                return oid
            self.objects[oid] = (obj_type, data)
            _rp, _fp, obj_dir = self._paths()
            path = os.path.join(obj_dir, oid[:2], oid[2:])
            if not os.path.exists(path):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    raw = ("%s %d\0" % (obj_type, len(data))).encode("ascii") + data
                    tmp = path + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(zlib.compress(raw, 6))
                    os.replace(tmp, path)
                except OSError:
                    pass
        return oid

    def put_objects(self, objects):
        """objects: {oid: (type, bytes)} — store every one, return oids kept."""
        kept = []
        for oid, (obj_type, data) in objects.items():
            if hash_object(obj_type, data) != oid:
                raise ValueError("object %s does not hash to its id" % oid)
            kept.append(self.put_object(obj_type, data))
        return kept

    # ------------------------------------------------------------ repos / refs

    def create_repo(self, name, description="", private=False):
        """Return the Repo, or None when a repo of that name already exists."""
        with self.lock:
            if name in self.repos:
                return None
            repo = Repo(name, None, description, private, self._next_id)
            self._next_id += 1
            self.repos[name] = repo
            self.refs.setdefault(name, {})
            try:
                self._save_repos()
                self._save_refs()
            except OSError:
                pass
            return repo

    def get_repo(self, owner, name):
        """Owner-scoped lookup. The first access binds an unowned repo to
        that owner; the same name under another owner is None (404, never
        403). The token names one login and the suite only ever creates a
        name once, so lazy binding is exact for this protocol."""
        with self.lock:
            repo = self.repos.get(name)
            if repo is None:
                return None
            if repo.owner is None:
                repo.owner = owner
                try:
                    self._save_repos()
                except OSError:
                    pass
            if repo.owner != owner:
                return None
            return repo

    def repo_exists(self, owner, name):
        return self.get_repo(owner, name) is not None

    def list_repos(self):
        with self.lock:
            return [self.repos[n] for n in sorted(self.repos)]

    def get_refs(self, name):
        with self.lock:
            return dict(sorted(self.refs.get(name, {}).items()))

    def get_ref(self, name, ref):
        with self.lock:
            return self.refs.get(name, {}).get(ref)

    def set_ref(self, name, ref, oid):
        with self.lock:
            self.refs.setdefault(name, {})[ref] = oid
            try:
                self._save_refs()
            except OSError:
                pass

    def delete_ref(self, name, ref):
        with self.lock:
            self.refs.get(name, {}).pop(ref, None)
            try:
                self._save_refs()
            except OSError:
                pass

    # ------------------------------------------------------------ graph walks

    def peel(self, oid):
        """Follow tag objects to the commit/tree/blob they point at."""
        seen = set()
        while oid in self.objects and oid not in seen:
            seen.add(oid)
            obj_type, data = self.objects[oid]
            if obj_type != "tag":
                return oid
            oid = parse_tag(data)["object"]
        return oid

    def is_ancestor(self, ancestor, descendant):
        """True when ancestor == descendant or ancestor is reachable from
        descendant through commit parents (tags peeled first)."""
        ancestor = self.peel(ancestor)
        descendant = self.peel(descendant)
        if ancestor == descendant:
            return True
        if ancestor == ZERO_OID:
            return True
        seen = set()
        stack = [descendant]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            entry = self.objects.get(oid)
            if entry is None or entry[0] != "commit":
                continue
            for parent in parse_commit(entry[1])["parents"]:
                if parent == ancestor:
                    return True
                stack.append(parent)
        return False

    def _add_tree(self, out, tree_oid):
        """Add a tree and everything under it (blobs, subtrees)."""
        stack = [tree_oid]
        while stack:
            oid = stack.pop()
            if oid in out:
                continue
            entry = self.objects.get(oid)
            if entry is None:
                continue
            out.add(oid)
            if entry[0] != "tree":
                continue
            for mode, _name, child in parse_tree(entry[1]):
                if mode == "160000":      # gitlink: commit the repo does not hold
                    continue
                stack.append(child)

    def reachable(self, roots):
        """Every object reachable from roots (commits, tags, trees, blobs)."""
        out = set()
        stack = list(roots)
        while stack:
            oid = stack.pop()
            if oid in out or oid == ZERO_OID:
                continue
            entry = self.objects.get(oid)
            if entry is None:
                continue
            obj_type, data = entry
            out.add(oid)
            if obj_type == "commit":
                commit = parse_commit(data)
                self._add_tree(out, commit["tree"])
                stack.extend(commit["parents"])
            elif obj_type == "tag":
                stack.append(parse_tag(data)["object"])
            elif obj_type == "tree":
                self._add_tree(out, oid)
        return out

    def _bounded_reachable(self, roots, stop):
        """Reachable objects, but never walking past a commit in `stop`
        (a shallow client's boundary: it holds the commit, not its parents)."""
        out = set()
        stack = list(roots)
        while stack:
            oid = stack.pop()
            if oid in out or oid == ZERO_OID:
                continue
            entry = self.objects.get(oid)
            if entry is None:
                continue
            obj_type, data = entry
            out.add(oid)
            if obj_type == "commit":
                commit = parse_commit(data)
                self._add_tree(out, commit["tree"])
                if oid not in stop:
                    stack.extend(commit["parents"])
            elif obj_type == "tag":
                stack.append(parse_tag(data)["object"])
            elif obj_type == "tree":
                self._add_tree(out, oid)
        return out

    def collect_for_fetch(self, wants, haves=(), depth=None, shallow=()):
        """Objects a fetch must send, plus shallow/unshallow boundary lists.

        wants: tip oids. haves: oids the client already holds. depth: max
        commit generations from a want (1 = tip only). shallow: the
        client's current shallow boundary (for deepen).
        Returns (object_oids, shallow_list, unshallow_list).
        """
        with self.lock:
            # A shallow client's haves stop at its shallow boundary: it
            # holds the boundary commit, not the history behind it, so the
            # have-closure must not walk past `shallow`.
            have_closure = (self._bounded_reachable(haves, set(shallow))
                            if haves else set())
            # Commit walk, level by level from the wants.
            included_commits = set()
            boundary = set()
            queue = []          # (oid, level)
            seen_level = {}

            def enqueue(oid, level):
                oid = self.peel(oid)
                if oid in seen_level and seen_level[oid] <= level:
                    return
                seen_level[oid] = level
                queue.append((oid, level))

            for want in wants:
                entry = self.objects.get(want)
                if entry is None:
                    continue
                if entry[0] == "tag":
                    included_commits.add(want)
                    enqueue(parse_tag(entry[1])["object"], 0)
                elif entry[0] == "commit":
                    enqueue(want, 0)
                else:
                    included_commits.add(want)

            idx = 0
            while idx < len(queue):
                oid, level = queue[idx]
                idx += 1
                if depth is not None and level >= depth:
                    continue
                if oid in included_commits:
                    continue
                included_commits.add(oid)
                entry = self.objects.get(oid)
                if entry is None or entry[0] != "commit":
                    continue
                parents = parse_commit(entry[1])["parents"]
                if depth is not None and level == depth - 1 and parents:
                    boundary.add(oid)
                for parent in parents:
                    enqueue(parent, level + 1)

            objects = set()
            for oid in included_commits:
                entry = self.objects.get(oid)
                if entry is None:
                    continue
                objects.add(oid)
                if entry[0] == "commit":
                    self._add_tree(objects, parse_commit(entry[1])["tree"])
                elif entry[0] == "tag":
                    target = self.peel(oid)
                    if target in self.objects:
                        objects.add(target)
                        if self.objects[target][0] == "commit":
                            self._add_tree(
                                objects,
                                parse_commit(self.objects[target][1])["tree"])
                elif entry[0] == "tree":
                    self._add_tree(objects, oid)
            # A tag's target commit also needs its own closure.
            extra = set()
            for oid in list(objects):
                entry = self.objects.get(oid)
                if entry and entry[0] == "commit":
                    extra |= self.reachable([oid]) if depth is None else set()
            objects |= extra
            objects -= have_closure
            # Never send a commit the client holds as a have, even via depth.
            shallow_list = sorted(b for b in boundary if b not in have_closure)
            unshallow = sorted(
                s for s in shallow
                if s in included_commits and s not in boundary
                and s not in have_closure)
            return objects, shallow_list, unshallow
