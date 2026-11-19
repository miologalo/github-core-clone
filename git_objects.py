"""Git object model: SHA-1 ids, loose-object headers, commit/tree/tag parsing.

Standard library only. No git binary, no outside service.
"""

from __future__ import annotations

import hashlib

ZERO_OID = "0" * 40

_TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}


def hash_object(obj_type: str, data: bytes) -> str:
    """git's object id: SHA-1 over '<type> <size>\\0' + bytes."""
    h = hashlib.sha1()
    h.update(("%s %d\0" % (obj_type, len(data))).encode("ascii"))
    h.update(data)
    return h.hexdigest()


def type_code(obj_type: str) -> int:
    return _TYPE_CODE[obj_type]


def code_type(code: int) -> str:
    return _CODE_TYPE[code]


def parse_tree(data: bytes):
    """Return [(mode, name, oid)] in the object's stored order."""
    entries = []
    off = 0
    while off < len(data):
        sp = data.index(b" ", off)
        nul = data.index(b"\0", sp + 1)
        mode = data[off:sp].decode("ascii")
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        off = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    key = (name + "/") if mode == "40000" else name
    return key.encode("utf-8")


def build_tree(entries):
    """Build tree bytes from [(mode, name, oid)], sorted the way git sorts."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += ("%s %s\0" % (mode, name)).encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data: bytes):
    """Return {'tree': oid, 'parents': [...], 'author': str, 'committer': str,
    'message': str, 'extra': [(key, value)]}."""
    head, sep, message = data.partition(b"\n\n")
    result = {"tree": "", "parents": [], "author": "", "committer": "",
              "message": message.decode("utf-8", "replace") if sep else "",
              "extra": []}
    if not sep:
        head = data
        result["message"] = ""
    for line in head.decode("utf-8", "replace").split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key == "tree":
            result["tree"] = value
        elif key == "parent":
            result["parents"].append(value)
        elif key == "author":
            result["author"] = value
        elif key == "committer":
            result["committer"] = value
        else:
            result["extra"].append((key, value))
    return result


def build_commit(tree, parents, author, committer, message):
    lines = ["tree %s" % tree]
    for parent in parents:
        lines.append("parent %s" % parent)
    lines.append("author %s" % author)
    lines.append("committer %s" % committer)
    lines.append("")
    return ("\n".join(lines) + "\n" + message).encode("utf-8")


def parse_tag(data: bytes):
    head, sep, message = data.partition(b"\n\n")
    fields = {}
    for line in head.decode("utf-8", "replace").split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        fields[key] = value
    return {"object": fields.get("object", ""), "type": fields.get("type", ""),
            "tag": fields.get("tag", ""), "tagger": fields.get("tagger", ""),
            "message": message.decode("utf-8", "replace") if sep else ""}


def build_tag(obj, obj_type, tag, tagger, message):
    text = ("object %s\ntype %s\ntag %s\ntagger %s\n\n%s"
            % (obj, obj_type, tag, tagger, message))
    return text.encode("utf-8")


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git delta instruction stream to a base object's bytes."""
    off = 0

    def read_varint():
        nonlocal off
        value = 0
        shift = 0
        while True:
            if off >= len(delta):
                raise ValueError("delta size varint truncated")
            byte = delta[off]
            off += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    source_size = read_varint()
    target_size = read_varint()
    if source_size != len(base):
        raise ValueError("delta expects %d-byte base, got %d"
                         % (source_size, len(base)))
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    copy_off |= delta[off] << (bit * 8)
                    off += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    copy_size |= delta[off] << (bit * 8)
                    off += 1
            if copy_size == 0:
                copy_size = 0x10000
            if copy_off + copy_size > len(base):
                raise ValueError("delta copy out of base")
            out += base[copy_off:copy_off + copy_size]
        elif op:
            size = op & 0x7F
            if off + size > len(delta):
                raise ValueError("delta insert truncated")
            out += delta[off:off + size]
            off += size
        else:
            raise ValueError("delta opcode 0 invalid")
    if len(out) != target_size:
        raise ValueError("delta produced %d of %d bytes"
                         % (len(out), target_size))
    return bytes(out)
