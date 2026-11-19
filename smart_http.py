"""Git smart HTTP: info/refs advertisements, upload-pack (v2 + v0),
ls-refs, and receive-pack (v0) with report-status and fast-forward rules.

Wire details follow the suite's client (kit program/env/src/git-client.ts):
pkt-line framing, the "# service=..." preamble on v0 advertisements,
"version 2" on v2, side-band-64k framing of packs and push reports, and
the exact per-ref report lines "ok <ref>" / "ng <ref> non-fast-forward".
"""

from __future__ import annotations

from git_objects import ZERO_OID, parse_tag
from packfile import (decode_all, decode_until_flush, encode_pkt_line,
                      encode_pkt_lines, read_pack, text_lines, write_pack)

AGENT = "github-core-clone/1.0"

UPLOAD_V0_CAPS = ("side-band-64k thin-pack ofs-delta shallow "
                  "multi_ack_detailed agent=%s" % AGENT)
RECEIVE_CAPS = ("report-status side-band-64k delete-refs ofs-delta "
                "thin-pack agent=%s" % AGENT)

_SIDEBAND_CHUNK = 32768


class GitError(Exception):
    """A protocol-level failure; carries the HTTP status to answer with."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- helpers

def _sorted_refs(store, repo_name):
    return sorted(store.get_refs(repo_name).items())


def _peeled(store, oid):
    """The commit a tag object peels to, or None when oid is not a tag."""
    entry = store.get_object(oid)
    if entry is None or entry[0] != "tag":
        return None
    target = parse_tag(entry[1])["object"]
    while True:
        inner = store.get_object(target)
        if inner is None or inner[0] != "tag":
            return target
        target = parse_tag(inner[1])["object"]


def _head_oid(store, repo_name):
    return store.get_ref(repo_name, "refs/heads/main")


def _sideband_pack(pack: bytes) -> bytes:
    """Pack bytes as side-band-64k band-1 pkt-lines."""
    out = bytearray()
    for off in range(0, len(pack), _SIDEBAND_CHUNK):
        chunk = b"\x01" + pack[off:off + _SIDEBAND_CHUNK]
        out += encode_pkt_line(chunk)
    return bytes(out)


def _ref_lines_v0(store, repo_name, caps):
    """v0 ref advertisement lines (without preamble/flush)."""
    refs = _sorted_refs(store, repo_name)
    head = _head_oid(store, repo_name)
    lines = []
    if not refs:
        lines.append(b"%s capabilities^{}\0%s\n" % (ZERO_OID.encode(), caps.encode()))
        return lines
    first = True
    ordered = []
    if head:
        ordered.append(("HEAD", head))
    ordered.extend(refs)
    for name, oid in ordered:
        line = ("%s %s" % (oid, name)).encode()
        if first:
            line += b"\0" + caps.encode()
            first = False
        lines.append(line + b"\n")
        peeled = _peeled(store, oid)
        if peeled:
            lines.append(("%s %s^{}\n" % (peeled, name)).encode())
    return lines


def advertisement_v0(store, repo_name, service):
    if service == "git-upload-pack":
        caps = UPLOAD_V0_CAPS + " symref=HEAD:refs/heads/main"
    else:
        caps = RECEIVE_CAPS + " symref=HEAD:refs/heads/main"
    preamble = ("# service=%s\n" % service).encode()
    body = encode_pkt_lines([preamble, "flush"])
    body += encode_pkt_lines(_ref_lines_v0(store, repo_name, caps))
    body += encode_pkt_lines(["flush"])
    return body


def advertisement_v2():
    lines = [b"version 2\n",
             b"fetch=shallow wait-for-done\n",
             b"ls-refs=unborn\n",
             b"object-format=sha1\n",
             ("agent=%s\n" % AGENT).encode()]
    return encode_pkt_lines(lines + ["flush"])


# ---------------------------------------------------------------- ls-refs (v2)

def handle_ls_refs(store, repo_name, body: bytes) -> bytes:
    packets, off = decode_until_flush(body)
    lines = text_lines(packets)
    if not lines or not lines[0].startswith("command=ls-refs"):
        raise GitError(400, "expected command=ls-refs")
    # Arguments run between the delim and the flush.
    args = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "delim":
            seen_delim = True
        elif seen_delim and kind == "data":
            args.append(payload.decode("utf-8", "replace").rstrip("\n"))
    peel = "peel" in args
    symrefs = "symrefs" in args
    prefixes = [a[len("ref-prefix "):] for a in args
                if a.startswith("ref-prefix ")]
    refs = _sorted_refs(store, repo_name)
    head = _head_oid(store, repo_name)

    def keep(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    out = []
    head_line = None
    if head is None:
        if keep("HEAD"):
            head_line = "unborn HEAD"
            if symrefs:
                head_line += " symref-target:refs/heads/main"
            out.append(head_line + "\n")
    else:
        if keep("HEAD"):
            head_line = "%s HEAD" % head
            if symrefs:
                head_line += " symref-target:refs/heads/main"
            out.append(head_line + "\n")
    for name, oid in refs:
        if not keep(name):
            continue
        line = "%s %s" % (oid, name)
        if peel:
            peeled = _peeled(store, oid)
            if peeled:
                line += " peeled:%s" % peeled
        out.append(line + "\n")
    return encode_pkt_lines([s.encode() for s in out] + ["flush"])


# ---------------------------------------------------------------- fetch

def _parse_v2_request(body: bytes):
    packets = decode_all(body)
    lines = text_lines(packets)
    if not lines or not lines[0].startswith("command=fetch"):
        raise GitError(400, "expected command=fetch")
    args = []
    seen_delim = False
    for kind, payload in packets:
        if kind == "delim":
            seen_delim = True
        elif seen_delim and kind == "data":
            args.append(payload.decode("utf-8", "replace").rstrip("\n"))
    wants, haves, shallow = [], [], []
    depth = None
    for arg in args:
        if arg.startswith("want "):
            wants.append(arg[5:].strip())
        elif arg.startswith("have "):
            haves.append(arg[5:].strip())
        elif arg.startswith("shallow "):
            shallow.append(arg[8:].strip())
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                raise GitError(400, "bad deepen value")
        elif arg.startswith(("thin-pack", "no-progress", "ofs-delta",
                             "done", "filter ", "deepen-since ",
                             "deepen-not ", "agent=", "object-format=",
                             "sideband-all")):
            continue
        elif arg == "":
            continue
        else:
            # Unknown fetch arguments are ignored; the client only sends
            # the ones above, and ignoring keeps us robust to new ones.
            continue
    return wants, haves, shallow, depth


def _parse_v0_request(body: bytes):
    packets, off = decode_until_flush(body)
    first = text_lines(packets)
    rest_packets, _off2 = [], off
    # Second section: have/done lines up to the next flush (may be absent
    # when the client sent a single section).
    haves = []
    if off < len(body):
        try:
            packets2, off = decode_until_flush(body, off)
            rest_packets = text_lines(packets2)
        except ValueError:
            rest_packets = []
    wants, shallow = [], []
    depth = None
    for line in first:
        line = line.rstrip("\n")
        if line.startswith("want "):
            wants.append(line[5:].split(" ")[0])
        elif line.startswith("shallow "):
            shallow.append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                raise GitError(400, "bad deepen value")
    for line in rest_packets:
        line = line.rstrip("\n")
        if line.startswith("have "):
            haves.append(line[5:].strip())
    return wants, haves, shallow, depth


def _build_pack(store, oids):
    objects = []
    with store.lock:
        for oid in sorted(oids):
            entry = store.get_object(oid)
            if entry is not None:
                objects.append(entry)
    return write_pack(objects)


def _fetch_response_v2(store, repo_name, wants, haves, shallow, depth):
    objects, shallow_list, unshallow = store.collect_for_fetch(
        wants, haves, depth, shallow)
    out = bytearray()
    # acknowledgments — only when the client sent haves. A real git client
    # with no haves expects the packfile section next and dies on an
    # acknowledgments section ("expected 'packfile'"); the suite client
    # accepts either order, so omitting it here satisfies both.
    # Sections are separated by delim (0001), not flush: a real git client
    # dies ("expected packfile to be sent after 'ready'") on a flush after
    # the acknowledgments section. Only the final section ends with flush.
    if haves:
        ack_lines = [b"acknowledgments\n"]
        for have in haves:
            if store.has_object(have):
                ack_lines.append(("ACK %s\n" % have).encode())
        ack_lines.append(b"ready\n")
        out += encode_pkt_lines(ack_lines + ["delim"])
    # shallow-info
    if shallow_list or unshallow:
        info = [b"shallow-info\n"]
        info += [("shallow %s\n" % o).encode() for o in shallow_list]
        info += [("unshallow %s\n" % o).encode() for o in unshallow]
        out += encode_pkt_lines(info + ["delim"])
    # packfile — always sent, even when nothing is new: an empty pack
    # keeps both real git and the suite client from stalling after 'ready'.
    pack = _build_pack(store, objects)
    out += encode_pkt_lines([b"packfile\n"])
    out += _sideband_pack(pack)
    out += encode_pkt_lines(["flush"])
    return bytes(out)


def _fetch_response_v0(store, repo_name, wants, haves, shallow, depth):
    objects, shallow_list, unshallow = store.collect_for_fetch(
        wants, haves, depth, shallow)
    out = bytearray()
    if haves and all(store.has_object(h) for h in haves):
        for have in haves:
            out += encode_pkt_line(("ACK %s\n" % have).encode())
    else:
        out += encode_pkt_line(b"NAK\n")
    for oid in shallow_list:
        out += encode_pkt_line(("shallow %s\n" % oid).encode())
    for oid in unshallow:
        out += encode_pkt_line(("unshallow %s\n" % oid).encode())
    # Always send a packfile, even an empty one, so a client that sent
    # haves never stalls waiting for pack data after the ACK.
    pack = _build_pack(store, objects)
    out += _sideband_pack(pack)
    out += encode_pkt_lines(["flush"])
    return bytes(out)


def handle_upload_pack_request(store, repo_name, body: bytes, version2: bool) -> bytes:
    """Dispatch a POST /git-upload-pack body: ls-refs or fetch (v2), fetch (v0)."""
    if version2:
        first = text_lines(decode_until_flush(body)[0])
        if first and first[0].startswith("command=ls-refs"):
            return handle_ls_refs(store, repo_name, body)
    return handle_upload_pack(store, repo_name, body, version2)


def handle_upload_pack(store, repo_name, body: bytes, version2: bool) -> bytes:
    if version2:
        wants, haves, shallow, depth = _parse_v2_request(body)
        return _fetch_response_v2(store, repo_name, wants, haves, shallow, depth)
    wants, haves, shallow, depth = _parse_v0_request(body)
    return _fetch_response_v0(store, repo_name, wants, haves, shallow, depth)


# ---------------------------------------------------------------- receive-pack

def _parse_receive_pack(body: bytes):
    packets, off = decode_until_flush(body)
    commands = []
    for line in text_lines(packets):
        line = line.rstrip("\n")
        if not line:
            continue
        # First line may carry "\0<capabilities>" after the ref name.
        line = line.split("\0")[0]
        parts = line.split(" ")
        if len(parts) != 3:
            raise GitError(400, "bad receive-pack command line")
        commands.append((parts[0], parts[1], parts[2]))
    pack = body[off:]
    return commands, pack


def handle_receive_pack(store, repo_name, body: bytes) -> bytes:
    try:
        commands, pack = _parse_receive_pack(body)
    except ValueError as exc:
        raise GitError(400, "malformed pkt-line: %s" % exc)
    if not commands:
        raise GitError(400, "empty receive-pack command list")

    # Unpack first: objects land in the store whatever the refs decide.
    unpack_ok = True
    unpack_detail = "ok"
    if pack:
        try:
            parsed = read_pack(pack, resolve_base=store.get_object)
            with store.lock:
                store.put_objects(parsed["objects"])
        except ValueError as exc:
            unpack_ok = False
            unpack_detail = "unpacker error: %s" % exc
    elif any(new != ZERO_OID for _old, new, _ref in commands):
        unpack_ok = False
        unpack_detail = "unpacker error: missing pack"

    report_lines = []
    if unpack_ok:
        report_lines.append("unpack ok")
    else:
        report_lines.append("unpack %s" % unpack_detail)

    with store.lock:
        for old, new, ref in commands:
            current = store.get_ref(repo_name, ref)
            if not unpack_ok:
                report_lines.append("ng %s unpack failed" % ref)
                continue
            reason = _ref_update_reason(store, repo_name, ref, old, new, current)
            if reason is None:
                if new == ZERO_OID:
                    store.delete_ref(repo_name, ref)
                else:
                    # The new tip's objects must be present (or already held).
                    if store.has_object(new) or store.has_object(store.peel(new)):
                        store.set_ref(repo_name, ref, new)
                    else:
                        report_lines.append("ng %s missing object" % ref)
                        continue
                report_lines.append("ok %s" % ref)
            else:
                report_lines.append("ng %s %s" % (ref, reason))

    report = encode_pkt_lines([("%s\n" % line).encode()
                               for line in report_lines])
    # The report-status stream is itself pkt-framed and ends with a flush
    # *inside* band 1; the outer flush below ends the sideband framing.
    report += b"0000"
    # The report-status stream rides inside side-band-64k band 1.
    out = bytearray()
    for off in range(0, len(report), _SIDEBAND_CHUNK):
        out += encode_pkt_line(b"\x01" + report[off:off + _SIDEBAND_CHUNK])
    out += encode_pkt_lines(["flush"])
    return bytes(out)


def _ref_update_reason(store, repo_name, ref, old, new, current):
    """None when the update may proceed, else the ng reason string."""
    current = current or ZERO_OID
    if old != current:
        # A create (old == 0) over an existing ref, or a stale old value.
        if old == ZERO_OID and current != ZERO_OID:
            return "non-fast-forward"
        return "non-fast-forward" if new != ZERO_OID else "non-fast-forward"
    if new == ZERO_OID:
        return None  # delete, old matched
    if old == ZERO_OID:
        return None  # create, ref was absent
    if new == old:
        return None
    if ref.startswith("refs/tags/"):
        # Tag moves are not fast-forwards in this forge: refuse unless the
        # caller is replacing the identical object (handled above).
        return "non-fast-forward"
    if not store.is_ancestor(old, new):
        return "non-fast-forward"
    return None
