"""Read and write the Warcraft III .w3z save container (same container as .w3g replays).

Layout (all little-endian):
  0x00 char[28]  "Warcraft III recorded game\\x1a\\0"
  0x1C u32       header size (0x44)
  0x20 u32       file size (compressed)
  0x24 u32       header version (1)
  0x28 u32       decompressed payload size
  0x2C u32       block count
  0x30 char[4]   product ("PX3W" = W3XP reversed)
  0x34 u32       game version
  0x38 u16       build
  0x3A u16       flags
  0x3C u32       length (game time, ms)
  0x40 u32       CRC32 of header bytes 0..0x43 with this field zeroed
  0x44 blocks: u32 comp_size, u32 decomp_size, u32 checksum, comp_size bytes of zlib data

Block checksum = crc16(block header with checksum zeroed) | crc16(compressed data) << 16,
where crc16(x) = crc32(x) folded: (c ^ (c >> 16)) & 0xFFFF.
Blocks are zlib streams flushed with Z_SYNC_FLUSH and no final block, so zlib.decompress()
fails with "incomplete stream"; a decompressobj accepts it. The last block is padded with
zeros up to decomp_size; the payload is trimmed to the header's decompressed size.

Usage: python3 w3z.py <in.w3z> <out.bin>     (dumps the payload, prints a header summary)
"""
import json
import struct
import sys
import zlib

MAGIC = b"Warcraft III recorded game\x1a\x00"


def fold16(data: bytes) -> int:
    c = zlib.crc32(data)
    return (c ^ (c >> 16)) & 0xFFFF


def parse(raw: bytes) -> tuple[dict, bytes]:
    if raw[:28] != MAGIC:
        raise ValueError("bad magic")
    hsize, fsize, hver, dsize, nblocks = struct.unpack_from("<5I", raw, 0x1C)
    product, gver, build, flags, length_ms, hcrc = struct.unpack_from("<4sIHHII", raw, 0x30)
    hdr = bytearray(raw[:hsize])
    hdr[0x40:0x44] = b"\0\0\0\0"
    header = {
        "header_size": hsize, "file_size_field": fsize, "file_size_actual": len(raw),
        "header_version": hver, "decompressed_size": dsize, "block_count": nblocks,
        "product": product.decode("latin-1"), "game_version": gver, "build": build,
        "flags": flags, "length_ms": length_ms, "header_crc": f"{hcrc:08x}",
        "header_crc_ok": zlib.crc32(bytes(hdr)) == hcrc,
    }
    errors = []
    if not header["header_crc_ok"]:
        errors.append("header crc mismatch")
    if fsize != len(raw):
        errors.append(f"file size field {fsize} != actual {len(raw)}")

    out, off, blocks = [], hsize, []
    for i in range(nblocks):
        csize, usize, chk = struct.unpack_from("<3I", raw, off)
        bh = raw[off:off + 8] + b"\0\0\0\0"
        data = raw[off + 12:off + 12 + csize]
        if len(data) != csize:
            raise ValueError(f"block {i}: truncated at 0x{off:x}")
        expect = fold16(bh) | (fold16(data) << 16)
        d = zlib.decompressobj()
        plain = d.decompress(data) + d.flush(zlib.Z_SYNC_FLUSH)
        ok = expect == chk
        if not ok:
            errors.append(f"block {i} @0x{off:x}: checksum {chk:08x} != {expect:08x}")
        if len(plain) > usize:
            errors.append(f"block {i}: inflated {len(plain)} > declared {usize}")
        out.append(plain.ljust(usize, b"\0"))
        blocks.append({"offset": off, "comp": csize, "decomp": usize, "inflated": len(plain), "checksum_ok": ok})
        off += 12 + csize
    if off != len(raw):
        errors.append(f"{len(raw) - off} trailing bytes after last block")
    payload = b"".join(out)
    if len(payload) < dsize:
        errors.append(f"payload {len(payload)} < declared {dsize}")
    header["blocks"] = blocks
    header["errors"] = errors
    return header, payload[:dsize]


BLOCK = 0x100000


def encode_block(plain: bytes) -> bytes:
    """Encode one block the way the game does: zlib level 1, Z_SYNC_FLUSH."""
    c = zlib.compressobj(1)
    comp = c.compress(plain) + c.flush(zlib.Z_SYNC_FLUSH)
    bh = struct.pack("<II", len(comp), len(plain))
    return bh + struct.pack("<I", fold16(bh + b"\0" * 4) | fold16(comp) << 16) + comp


def build(raw: bytes, header: dict, payload: bytes) -> bytes:
    """New container for payload, keeping the original header except sizes and CRC."""
    hsize = header["header_size"]
    blocks = [encode_block(payload[i:i + BLOCK].ljust(BLOCK, b"\0")) for i in range(0, len(payload), BLOCK)]
    body = b"".join(blocks)
    hdr = bytearray(raw[:hsize])
    struct.pack_into("<I", hdr, 0x20, hsize + len(body))
    struct.pack_into("<I", hdr, 0x28, len(payload))
    struct.pack_into("<I", hdr, 0x2C, len(blocks))
    struct.pack_into("<I", hdr, 0x40, 0)
    struct.pack_into("<I", hdr, 0x40, zlib.crc32(bytes(hdr)))
    return bytes(hdr) + body


def _selfcheck() -> None:
    # Build a 1-block container the way the game does and parse it back.
    plain = b"hello save" * 100
    c = zlib.compressobj()
    comp = c.compress(plain) + c.flush(zlib.Z_SYNC_FLUSH)
    bh = struct.pack("<II", len(comp), len(plain))
    blk = bh + struct.pack("<I", fold16(bh + b"\0" * 4) | fold16(comp) << 16) + comp
    hdr = bytearray(MAGIC + struct.pack("<5I4sIHHII", 0x44, 0x44 + len(blk), 1, len(plain), 1, b"PX3W", 1, 2, 0, 3, 0))
    struct.pack_into("<I", hdr, 0x40, zlib.crc32(bytes(hdr)))
    raw = bytes(hdr) + blk
    h, p = parse(raw)
    assert p == plain and not h["errors"], h["errors"]
    h2, p2 = parse(build(raw, h, plain + b"!"))
    assert p2 == plain + b"!" and not h2["errors"], h2["errors"]


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _selfcheck()
        print("selfcheck ok")
        sys.exit(0)
    raw = open(sys.argv[1], "rb").read()
    header, payload = parse(raw)
    with open(sys.argv[2], "wb") as f:
        f.write(payload)
    summary = {k: v for k, v in header.items() if k != "blocks"}
    summary["blocks_bad"] = sum(not b["checksum_ok"] for b in header["blocks"])
    print(json.dumps(summary, indent=1))
    sys.exit(1 if header["errors"] else 0)
