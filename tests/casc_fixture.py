"""Builds a tiny fake WoW install in the CASC layout so the reader can be tested without the game."""
import hashlib
import io
import struct
import zlib
from pathlib import Path


def blte(data, chunked=False):
    if not chunked:
        body = b"Z" + zlib.compress(data)
        return b"BLTE" + struct.pack(">I", 0) + body
    parts = [data[i:i + 100] for i in range(0, len(data), 100)] or [b""]
    enc = [b"N" + p if i % 2 else b"Z" + zlib.compress(p) for i, p in enumerate(parts)]
    table = b"".join(struct.pack(">II", len(e), len(p)) + hashlib.md5(e).digest() for e, p in zip(enc, parts))
    header_size = 12 + len(table)
    return b"BLTE" + struct.pack(">I", header_size) + bytes([0x0F]) + len(parts).to_bytes(3, "big") + table + b"".join(enc)


def build(root_dir, files, root_version=2, product="wow_test", decoy_first=False):
    """files: {fileDataID: bytes}. Returns the game folder (the `_test_` product folder)."""
    root_dir = Path(root_dir)
    game = root_dir / "_test_"
    data_dir = root_dir / "Data"
    (data_dir / "data").mkdir(parents=True)
    game.mkdir()
    archive = bytearray()
    index = []  # (ekey, offset, size)

    def store(content, chunked=False):
        enc = blte(content, chunked)
        ekey = hashlib.md5(enc).digest()
        header = ekey[::-1] + struct.pack("<I", len(enc) + 30) + b"\x00\x00" + b"\x00" * 8
        index.append((ekey, len(archive), len(enc) + 30))
        archive.extend(header + enc)
        return ekey

    mapping = {}  # ckey -> ekey
    id_to_ckey = {}
    for n, (fid, content) in enumerate(sorted(files.items())):
        ck = hashlib.md5(content).digest()
        mapping[ck] = store(content, chunked=(n == 0 and len(content) > 200))
        id_to_ckey[fid] = ck

    # root file
    ids = sorted(id_to_ckey)
    deltas, prev = [], 0
    for i in ids:
        deltas.append(i - prev)
        prev = i + 1
    block_body = struct.pack("<%di" % len(ids), *deltas) + b"".join(id_to_ckey[i] for i in ids)
    if root_version == 2:
        block_hdr = struct.pack("<II", len(ids), 0xFFFFFFFF) + struct.pack("<II", 0x10000000, 0) + b"\x00"
        root = b"TSFM" + struct.pack("<IIII", 0x18, 2, len(ids), 0) + b"\x00\x00\x00\x00" + block_hdr + block_body
    else:
        block_hdr = struct.pack("<III", len(ids), 0x10000000, 0xFFFFFFFF)
        root = b"TSFM" + struct.pack("<II", len(ids), 0) + block_hdr + block_body
    root_ck = hashlib.md5(root).digest()
    mapping[root_ck] = store(root)

    # encoding file: one page
    page = b""
    for ck, ek in sorted(mapping.items()):
        if decoy_first and ck != root_ck:   # an extra stored copy that is not in the local archives
            page += bytes([2]) + (0).to_bytes(5, "big") + ck + hashlib.md5(ck).digest() + ek
        else:
            page += bytes([1]) + (0).to_bytes(5, "big") + ck + ek
    page_size = 4096 if not decoy_first else 8192
    assert len(page) < page_size
    page = page.ljust(page_size, b"\x00")
    first = sorted(mapping)[0]
    encoding = (b"EN" + struct.pack(">BBBHHIIBI", 1, 16, 16, page_size // 1024, 4, 1, 0, 0, 0)
                + first + hashlib.md5(page).digest() + page)
    enc_ck = hashlib.md5(encoding).digest()
    enc_ek = store(encoding)

    (data_dir / "data" / "data.000").write_bytes(bytes(archive))
    entries = b""
    for ekey, off, size in sorted(index):
        entries += ekey[:9] + off.to_bytes(5, "big") + struct.pack("<I", size)
    idx = struct.pack("<II", 0x10, 0) + struct.pack("<HBBBBBB", 7, 0, 0, 4, 5, 9, 30) + struct.pack("<Q", 1 << 30)
    idx = idx.ljust(32, b"\x00") + struct.pack("<II", len(entries), 0) + entries
    (data_dir / "data" / "0000000001.idx").write_bytes(idx)

    build_key = "ab" * 16
    cfg_dir = data_dir / "config" / "ab" / "ab"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / build_key).write_text(
        f"# Build Configuration\nroot = {root_ck.hex()}\nencoding = {enc_ck.hex()} {enc_ek.hex()}\nbuild-name = FAKE\n")
    (root_dir / ".build.info").write_text(
        "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|Version!STRING:0|Product!STRING:0\n"
        f"us|1|{build_key}|1.0.0.1|{product}\n")
    (game / ".flavor.info").write_text(f"Product Flavor!STRING:0\n{product}\n")
    return game
