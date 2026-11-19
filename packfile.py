"""Packfile read/write (pack format v2) and pkt-line framing.

Read supports plain objects plus OFS_DELTA and REF_DELTA (including thin
packs whose bases live in the caller's store). Write emits plain objects
only, which every git client accepts.
"""

from __future__ import annotations

import hashlib
import zlib

from git_objects import apply_delta, code_type, hash_object, type_code

MAX_PKT = 65520


# ---------------------------------------------------------------- pkt-line

def encode_pkt_line(payload: bytes) -> bytes:
    if len(payload) > MAX_PKT - 4:
        raise ValueError("pkt-line payload too long")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def encode_pkt_lines(items) -> bytes:
    """items: bytes payloads, or 'flush' / 'delim' / 'response-end'."""
    out = bytearray()
    for item in items:
        if item == "flush":
            out += b"0000"
        elif item == "delim":
            out += b"0001"
        elif item == "response-end":
            out += b"0002"
        else:
            out += encode_pkt_line(item)
    return bytes(out)


def _read_one(data: bytes, off: int):
    """Return (kind, payload, next_off). kind in data/flush/delim/response-end."""
    if off + 4 > len(data):
        raise ValueError("truncated pkt-line header")
    head = data[off:off + 4]
    try:
        length = int(head.decode("ascii"), 16)
    except ValueError:
        raise ValueError("invalid pkt-line length %r" % head)
    if length == 0:
        return "flush", b"", off + 4
    if length == 1:
        return "delim", b"", off + 4
    if length == 2:
        return "response-end", b"", off + 4
    if length < 4 or length > MAX_PKT:
        raise ValueError("bad pkt-line length %d" % length)
    if off + length > len(data):
        raise ValueError("truncated pkt-line body")
    return "data", data[off + 4:off + length], off + length


def decode_until_flush(data: bytes, off: int = 0):
    """Decode pkt-lines up to and including the first flush.

    Returns (packets, offset_after_flush) where packets is a list of
    (kind, payload). Raises ValueError on malformed framing.
    """
    packets = []
    while True:
        kind, payload, off = _read_one(data, off)
        packets.append((kind, payload))
        if kind == "flush":
            return packets, off


def decode_all(data: bytes):
    """Decode a whole buffer of pkt-lines; trailing bytes are an error."""
    packets, off = [], 0
    while off < len(data):
        kind, payload, off = _read_one(data, off)
        packets.append((kind, payload))
    return packets


def text_lines(packets):
    """Text of every data packet, in order."""
    return [p.decode("utf-8", "replace") for kind, p in packets if kind == "data"]


# ---------------------------------------------------------------- pack write

def _pack_object_header(obj_type: str, size: int) -> bytes:
    code = type_code(obj_type)
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects) -> bytes:
    """objects: iterable of (type, bytes). Plain objects, no deltas."""
    items = list(objects)
    parts = [b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")]
    for obj_type, data in items:
        parts.append(_pack_object_header(obj_type, len(data)))
        parts.append(zlib.compress(data, 6))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def pack_checksum_ok(pack: bytes) -> bool:
    if len(pack) < 20:
        return False
    return hashlib.sha1(pack[:-20]).digest() == pack[-20:]


# ---------------------------------------------------------------- pack read

def _inflate_at(buf: bytes, off: int, end: int):
    """Inflate one zlib stream starting at off; return (data, next_off)."""
    decomp = zlib.decompressobj()
    data = decomp.decompress(buf[off:end])
    consumed = len(buf[off:end]) - len(decomp.unused_data)
    if not decomp.eof:
        raise ValueError("zlib stream at %d is truncated" % off)
    return data, off + consumed


def read_pack(pack: bytes, resolve_base=None):
    """Read a packfile.

    resolve_base(oid) -> (type, bytes) | None supplies thin-pack bases.
    Returns {'version': int, 'count': int, 'objects': {oid: (type, bytes)},
    'delta_count': int}. Raises ValueError on any corruption.
    """
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(pack[8:12], "big")
    trailer_start = len(pack) - 20
    if not pack_checksum_ok(pack):
        raise ValueError("pack trailer checksum mismatch")

    raw = []          # (offset, type_code, size, data, base_offset|base_oid)
    by_offset = {}
    off = 12
    for _ in range(count):
        entry_off = off
        byte = pack[off]
        first = byte
        size = byte & 0x0F
        shift = 4
        off += 1
        while byte & 0x80:
            byte = pack[off]
            off += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        tcode = (first >> 4) & 0x07
        base_off = None
        base_oid = None
        if tcode == 6:  # OFS_DELTA
            byte = pack[off]
            off += 1
            value = byte & 0x7F
            while byte & 0x80:
                byte = pack[off]
                off += 1
                value = ((value + 1) << 7) | (byte & 0x7F)
            base_off = entry_off - value
            if base_off < 0 or base_off >= entry_off:
                raise ValueError("ofs-delta base offset out of range")
        elif tcode == 7:  # REF_DELTA
            base_oid = pack[off:off + 20].hex()
            off += 20
        elif tcode not in (1, 2, 3, 4):
            raise ValueError("pack entry with unknown type code %d" % tcode)
        data, off = _inflate_at(pack, off, trailer_start)
        if len(data) != size:
            raise ValueError("pack entry size mismatch")
        entry = (entry_off, tcode, data, base_off, base_oid)
        raw.append(entry)
        by_offset[entry_off] = entry
    if off != trailer_start:
        raise ValueError("pack has trailing bytes before trailer")

    objects = {}
    resolved = {}     # offset -> (type, bytes, oid)
    by_oid = {}
    resolving = set()

    def resolve(entry):
        entry_off, tcode, data, base_off, base_oid = entry
        if entry_off in resolved:
            return resolved[entry_off]
        if entry_off in resolving:
            raise ValueError("delta cycle in pack")
        resolving.add(entry_off)
        if tcode == 6:
            base_entry = by_offset.get(base_off)
            if base_entry is None:
                raise ValueError("ofs-delta base not in pack")
            btype, bdata, _ = resolve(base_entry)
            otype, odata = btype, apply_delta(bdata, data)
        elif tcode == 7:
            base_entry = by_oid.get(base_oid)
            if base_entry is not None:
                btype, bdata, _ = resolve(base_entry)
                otype, odata = btype, apply_delta(bdata, data)
            elif resolve_base is not None:
                found = resolve_base(base_oid)
                if found is None:
                    raise ValueError("ref-delta base %s missing" % base_oid)
                otype, odata = found[0], apply_delta(found[1], data)
            else:
                raise ValueError("ref-delta base %s missing" % base_oid)
        else:
            otype, odata = code_type(tcode), data
        oid = hash_object(otype, odata)
        resolved[entry_off] = (otype, odata, oid)
        by_oid[oid] = entry
        resolving.discard(entry_off)
        return resolved[entry_off]

    delta_count = 0
    for entry in raw:
        if entry[1] in (6, 7):
            delta_count += 1
        otype, odata, oid = resolve(entry)
        objects.setdefault(oid, (otype, odata))
    return {"version": version, "count": count, "objects": objects,
            "delta_count": delta_count}
