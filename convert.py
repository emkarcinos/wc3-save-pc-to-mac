"""Convert Warcraft III Reforged saves made on Windows so they load on macOS.

Problem: some pathing objects in the save hold a table. Windows writes its header as 4 u32, Mac reads 5:

    Windows: [0][C][D][N]    + N entries
    Mac:     [A][A][C][D][N] + N entries,  A = D * in-memory entry size

Mac reads everything after it one u32 off: the load fails with "There was an error trying to load
the selected save game", or the game crashes.

Only 5 object classes have such a table. Blocks have no class tag, so the class comes from the block
shape: a table header at the class's fixed offset, with entries filling the block exactly.

The objects sit in tagged sub-sections. The game writes the same objects twice:

    [len3][N1 blocks][len4][N2 blocks][len5][N1 blocks][len6][N2 blocks]
     ^ converted here                  ^ second pass, no tables, kept as is

Usage: python3 convert.py <out_dir> <windows_save.w3z>...
"""
import collections
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also works with python3 -I
import w3z  # noqa: E402

TAG = b"espi"


def header(d: bytes, o: int):
    """Windows table header at o -> (0, C, D, N), or None if it can't be one."""
    if o < 0 or o + 16 > len(d):
        return None
    h = struct.unpack_from("<4I", d, o)
    c = h[1]
    return h if h[0] == 0 and c <= 0x10000 and c & (c - 1) == 0 else None  # C: 0 or a power of two


def fixed(o: int, entry: int, tail: int):
    def fit(d):
        h = header(d, o)
        return [o] if h and o + 16 + entry * h[3] + tail == len(d) else None
    return fit


def clrpath(d):
    """Two tables: at byte 56 and 10 bytes after the first one ends."""
    h1 = header(d, 56)
    if not h1:
        return None
    o2 = 56 + 16 + 8 * h1[3] + 10
    h2 = header(d, o2)
    return [56, o2] if h2 and o2 + 16 + 8 * h2[3] + 10 + 32 == len(d) else None


# class -> (table offsets finder, in-memory entry size used by the Mac reader)
LAYOUTS = {
    "CLrPathingAcc": (fixed(32, 12, 4), 0xC),
    "CLrPath": (clrpath, 0x8),
    "CPrCluster": (fixed(0, 40, 84), 0x38),
    "CRlAgentDef": (fixed(100, 44, 0), 0x50),
    "CPoPoInterfLis": (fixed(128, 12, 0), 0xC),
}


def block_len(p: bytes, pos: int):
    """Length of the tagged block [u32 len][data][espi] at pos, or None."""
    if pos + 4 > len(p):
        return None
    ln = struct.unpack_from("<I", p, pos)[0]
    return ln if p[pos + 4 + ln:pos + 8 + ln] == TAG else None


def sub_section(p: bytes, at: int):
    """(block count, end) if the u32 at `at` is a length that tagged blocks fill exactly, else None."""
    end, pos, n = at + 4 + struct.unpack_from("<I", p, at)[0], at + 4, 0
    while pos < end and (ln := block_len(p, pos)) is not None:
        pos, n = pos + 8 + ln, n + 1
    return (n, end) if pos == end and n else None


def locate(p: bytes) -> tuple[int, int]:
    """Length offsets of sub-sections 3 and 4."""
    tags, t = [], p.find(TAG)
    while t != -1:
        tags.append(t)
        t = p.find(TAG, t + 1)
    # sub-sections 4, 5 and 6 each start right after a tag (the end of the one before)
    after_tag = {t + 4: s for t in tags if t + 8 <= len(p) and (s := sub_section(p, t + 4))}
    # blocks 2..N of a sub-section also start right after a tag: block end -> block start, to walk back
    starts_at = {t + 12 + ln: t + 4 for t in tags if (ln := block_len(p, t + 4)) is not None}
    hits = []
    for s4, (n2, e4) in after_tag.items():
        if e4 in after_tag and (s6 := after_tag[e4][1]) in after_tag and after_tag[s6][0] == n2:
            n1 = after_tag[e4][0]
            # sub-section 3: n1 blocks ending at s4, its length field right before the first block
            pos, back = s4, 0
            while back < n1 - 1 and pos in starts_at:
                pos, back = starts_at[pos], back + 1
            for h in range(pos - 8, max(4, pos - 0x1000000), -1):  # block 1 has no tag before it
                if block_len(p, h) == pos - 8 - h and sub_section(p, h - 4) == (n1, s4):
                    hits.append((h - 4, s4))
                    break
    assert len(hits) == 1, f"expected one sub-section 3+4 pair, found {[(hex(a), hex(b)) for a, b in hits]}"
    return hits[0]


def convert_block(d: bytes, stats: collections.Counter) -> bytes:
    fits = [(name, offs, size) for name, (fit, size) in LAYOUTS.items() if (offs := fit(d))]
    assert len(fits) <= 1, f"block fits {[f[0] for f in fits]}"
    if not fits:
        return d
    name, offs, entry_size = fits[0]
    stats[name] += 1
    out = bytearray(d)
    for o in reversed(offs):  # back to front, so earlier offsets stay valid
        dd = struct.unpack_from("<I", d, o + 8)[0]
        out[o:o + 4] = struct.pack("<II", entry_size * dd, entry_size * dd)
    return bytes(out)


def convert_sub_section(p: bytes, len_off: int, stats: collections.Counter) -> bytes:
    end, pos, body = len_off + 4 + struct.unpack_from("<I", p, len_off)[0], len_off + 4, bytearray()
    while pos < end:
        ln = block_len(p, pos)
        d = convert_block(p[pos + 4:pos + 4 + ln], stats)
        body += struct.pack("<I", len(d)) + d + TAG
        pos += 8 + ln
    return p[:len_off] + struct.pack("<I", len(body)) + bytes(body) + p[end:]


def convert_save(src: str, dst: str) -> str:
    raw = open(src, "rb").read()
    hdr, p = w3z.parse(raw)
    assert not hdr["errors"], hdr["errors"]
    s3, s4 = locate(p)
    stats = collections.Counter()
    new = convert_sub_section(p, s4, stats)
    new = convert_sub_section(new, s3, stats)  # sub-section 3 is before 4: its offset is unchanged
    assert stats, "no tables found: already a Mac save?"
    out = w3z.build(raw, hdr, new)
    h2, p2 = w3z.parse(out)
    assert not h2["errors"] and p2 == new, h2["errors"]
    with open(dst, "xb") as f:  # never overwrite
        f.write(out)
    return f"ok, {sum(stats.values())} objects converted"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    out_dir, *saves = sys.argv[1:]
    failed = 0
    for src in saves:
        try:
            print(os.path.basename(src), "->", convert_save(src, os.path.join(out_dir, os.path.basename(src))))
        except (AssertionError, ValueError, OSError) as e:
            failed += 1
            print(os.path.basename(src), "-> FAILED:", e)
    sys.exit(1 if failed else 0)
