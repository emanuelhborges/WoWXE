"""
Minimal read-only reader for a local World of Warcraft CASC install, just enough to pull one
file out by its FileDataID (icons and map pictures are plain BLP files).

It reads, in order:
  .build.info                   -> which build is installed
  Data/config/..                -> that build's config (root + encoding keys)
  Data/data/*.idx + data.NNN    -> where each stored file lives, and the files themselves
  encoding file                 -> content key  -> stored key
  root file                     -> FileDataID   -> content key

Every stage can raise CascError with a plain-language message, and `describe()` prints what was
found so a problem can be diagnosed from the console output.

Only the Python standard library and numpy are needed.
"""
import bisect
import os
import struct
import zlib
from pathlib import Path

import numpy as np


class CascError(Exception):
    pass


# ---------------------------------------------------------------- BLTE
def blte_decode(blob):
    if blob[:4] != b"BLTE":
        raise CascError("data is not BLTE (bad magic %r)" % blob[:4])
    header_size = struct.unpack(">I", blob[4:8])[0]
    chunks = []  # (compressed_size, decompressed_size)
    if header_size == 0:
        body = blob[8:]
        return _blte_chunk(body)
    flags = blob[8]
    count = int.from_bytes(blob[9:12], "big")
    if flags != 0x0F:
        raise CascError("unexpected BLTE chunk table flags 0x%02x" % flags)
    pos = 12
    for _ in range(count):
        csize, dsize = struct.unpack(">II", blob[pos:pos + 8])
        chunks.append((csize, dsize))
        pos += 24
    out = []
    pos = header_size
    for csize, dsize in chunks:
        out.append(_blte_chunk(blob[pos:pos + csize]))
        pos += csize
    return b"".join(out)


def _blte_chunk(data):
    kind = data[:1]
    if kind == b"N":
        return data[1:]
    if kind == b"Z":
        return zlib.decompress(data[1:])
    if kind == b"E":
        raise CascError("file is encrypted (BLTE 'E')")
    raise CascError("unsupported BLTE chunk type %r" % kind)


# ---------------------------------------------------------------- helpers
def _find_roots(start):
    """Return (build_info_path, data_dir) searching `start` and its parents."""
    start = Path(start)
    for base in [start, *list(start.parents)[:3]]:
        bi = base / ".build.info"
        data = base / "Data"
        if bi.is_file() and data.is_dir():
            return bi, data
    # product folder may carry its own Data
    for base in [start, *list(start.parents)[:3]]:
        if (base / "Data" / "data").is_dir():
            cand = [base / ".build.info", base.parent / ".build.info"]
            for c in cand:
                if c.is_file():
                    return c, base / "Data"
    raise CascError("could not find .build.info and a Data folder at or above %s" % start)


def parse_build_info(path):
    lines = [l for l in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    header = [h.split("!")[0] for h in lines[0].split("|")]
    rows = [dict(zip(header, l.split("|"))) for l in lines[1:]]
    return rows


def parse_config(path):
    cfg = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


class CascReader:
    def __init__(self, game_dir, product=None, log=print):
        self.log = log
        self.game_dir = Path(game_dir)
        self.build_info, self.data_dir = _find_roots(self.game_dir)
        self.log(f"CASC: .build.info = {self.build_info}")
        self.log(f"CASC: Data folder  = {self.data_dir}")
        self.build_key = self._pick_build(product)
        cfg_path = self.data_dir / "config" / self.build_key[:2] / self.build_key[2:4] / self.build_key
        if not cfg_path.is_file():
            raise CascError(f"build config not found at {cfg_path}")
        self.config = parse_config(cfg_path)
        self.log(f"CASC: build config {self.build_key} ({self.config.get('build-name', '?')})")
        self._load_indexes()
        self._load_encoding()
        self._load_root()

    # ---- which build
    def _pick_build(self, product):
        rows = parse_build_info(self.build_info)
        wanted = product
        if not wanted:
            flavor = self.game_dir / ".flavor.info"
            if flavor.is_file():
                parts = flavor.read_text(errors="replace").split()
                wanted = parts[-1] if parts else None
        for r in rows:
            self.log("CASC: build.info row: branch=%s product=%s active=%s version=%s" % (
                r.get("Branch"), r.get("Product"), r.get("Active"), r.get("Version")))
        chosen = None
        if wanted:
            for r in rows:
                if r.get("Product") == wanted:
                    chosen = r
                    break
            if chosen is None:
                raise CascError(f"product {wanted!r} not found in .build.info; pass --product with one of the names printed above")
        else:
            active = [r for r in rows if r.get("Active") == "1"]
            chosen = (active or rows)[0]
        key = chosen.get("Build Key")
        if not key:
            raise CascError("no 'Build Key' in .build.info")
        self.log("CASC: using product %s" % chosen.get("Product"))
        return key

    # ---- index files
    def refresh_indexes(self, min_gap=5.0):
        """Re-read the index files (the game may have stored new files since we started)."""
        import time
        now = time.time()
        if now - getattr(self, "_last_refresh", 0) < min_gap:
            return False
        self._last_refresh = now
        before = len(self.index)
        self._load_indexes(quiet=True)
        return len(self.index) != before

    def _load_indexes(self, quiet=False):
        folder = self.data_dir / "data"
        best = {}
        for f in folder.glob("*.idx"):
            name = f.stem
            if len(name) != 10:
                continue
            bucket, version = int(name[:2], 16), int(name[2:], 16)
            if bucket not in best or version > best[bucket][0]:
                best[bucket] = (version, f)
        if not best:
            raise CascError(f"no .idx files in {folder}")
        self.index = {}
        for bucket, (_, f) in sorted(best.items()):
            self._read_idx(f)
        if not quiet:
            self.log(f"CASC: {len(self.index)} stored files indexed from {len(best)} index files")

    def _read_idx(self, path):
        raw = Path(path).read_bytes()
        hdr_size = struct.unpack("<I", raw[0:4])[0]
        candidates = []
        base = (8 + hdr_size + 15) & ~15
        for off in (base, 0x28, 0x20, 0x18, 0x30):
            if off not in candidates:
                candidates.append(off)
        for off in candidates:
            if off + 8 > len(raw):
                continue
            size = struct.unpack("<I", raw[off:off + 4])[0]
            if size and size % 18 == 0 and off + 8 + size <= len(raw):
                start = off + 8
                break
        else:
            raise CascError(f"cannot understand index file {path}")
        idx = self.index
        for pos in range(start, start + size, 18):
            ekey = raw[pos:pos + 9]
            off5 = int.from_bytes(raw[pos + 9:pos + 14], "big")
            esize = struct.unpack("<I", raw[pos + 14:pos + 18])[0]
            idx[ekey] = (off5 >> 30, off5 & 0x3FFFFFFF, esize)

    def _read_blob(self, ekey):
        entry = self.index.get(bytes(ekey[:9]))
        if not entry:
            raise CascError("stored key %s is not in the local archives (file not downloaded?)" % bytes(ekey).hex())
        arch, off, size = entry
        path = self.data_dir / "data" / ("data.%03d" % arch)
        with open(path, "rb") as fh:
            fh.seek(off)
            raw = fh.read(size)
        return raw[30:]  # skip the 30-byte per-file header

    def read_ekey(self, ekey):
        return blte_decode(self._read_blob(ekey))

    # ---- encoding
    def _load_encoding(self):
        ckey_hex, ekey_hex = self.config["encoding"].split()[:2]
        data = self.read_ekey(bytes.fromhex(ekey_hex))
        if data[:2] != b"EN":
            raise CascError("encoding file has bad magic")
        _, ck_size, ek_size, cpage_kb, _espec_kb, ccount, _ecount, _u, espec_size = struct.unpack(">BBBHHIIBI", data[2:22])
        pos = 22 + espec_size
        self._enc = data
        self._enc_ck_size, self._enc_ek_size = ck_size, ek_size
        self._enc_page_size = cpage_kb * 1024
        first = []
        for i in range(ccount):
            first.append(data[pos + i * (ck_size + 16): pos + i * (ck_size + 16) + ck_size])
        self._enc_first = first
        self._enc_pages_at = pos + ccount * (ck_size + 16)
        self.log(f"CASC: encoding file loaded ({ccount} pages)")

    def ckey_to_ekey(self, ckey):
        keys = self.ckey_to_ekeys(ckey)
        return keys[0] if keys else None

    def ckey_to_ekeys(self, ckey):
        """All stored keys the encoding file lists for a content key (a file can have several)."""
        i = bisect.bisect_right(self._enc_first, bytes(ckey)) - 1
        if i < 0:
            return None
        ck, ek = self._enc_ck_size, self._enc_ek_size
        pos = self._enc_pages_at + i * self._enc_page_size
        end = pos + self._enc_page_size
        d = self._enc
        while pos < end:
            n = d[pos]
            if n == 0:
                return None
            key = d[pos + 6:pos + 6 + ck]
            if key == bytes(ckey):
                e0 = pos + 6 + ck
                return [d[e0 + j * ek:e0 + (j + 1) * ek] for j in range(n)]
            pos += 6 + ck + n * ek
        return None

    # ---- root
    def _load_root(self):
        root_ckey = bytes.fromhex(self.config["root"].split()[0])
        ekey = self.ckey_to_ekey(root_ckey)
        if ekey is None:
            raise CascError("root content key not found in the encoding file")
        data = self.read_ekey(ekey)
        ids, keys = self._parse_root(data)
        order = np.argsort(ids, kind="stable")
        self._root_ids = ids[order]
        self._root_keys = keys[order]
        self.log(f"CASC: root file loaded ({len(ids)} files)")

    @staticmethod
    def _parse_root(d):
        magic = d[:4]
        if magic not in (b"TSFM", b"MFST"):
            raise CascError("root file is not in the MFST format (magic %r); this client may use a newer format" % magic)
        a, b = struct.unpack("<II", d[4:12])
        version = 0
        if b in (1, 2) and a in (0x14, 0x18, 0x1C, 0x20):   # new header: size, version, counts
            header_size, version = a, b
            total, named = struct.unpack("<II", d[12:20])
            pos = header_size
        else:                                                # old header: total, named
            total, named = a, b
            pos = 12
        v2 = version >= 2
        ids_out, keys_out = [], []
        n_seen = 0
        while pos < len(d) and n_seen < total:
            n = struct.unpack("<I", d[pos:pos + 4])[0]
            if v2:
                locale = struct.unpack("<I", d[pos + 4:pos + 8])[0]
                unk1, unk2 = struct.unpack("<II", d[pos + 8:pos + 16])
                unk3 = d[pos + 16]
                content = unk1 | unk2 | (unk3 << 17)
                pos += 17
            else:
                content, locale = struct.unpack("<II", d[pos + 4:pos + 12])
                pos += 12
            deltas = np.frombuffer(d, dtype="<i4", count=n, offset=pos)
            pos += 4 * n
            ckeys = np.frombuffer(d, dtype="u1", count=16 * n, offset=pos).reshape(n, 16)
            pos += 16 * n
            if not (content & 0x10000000):
                pos += 8 * n   # name hashes
            n_seen += n
            if content & 0x80:
                continue       # low-violence variants
            csum = np.cumsum(deltas.astype(np.int64) + 1) - 1
            ids_out.append(csum)
            keys_out.append(ckeys)
        if n_seen != total:
            raise CascError(f"root file records ({n_seen}) do not add up to its header ({total})")
        if not ids_out:
            raise CascError("root file contains no usable records")
        return np.concatenate(ids_out), np.concatenate(keys_out)

    def describe_id(self, file_data_id):
        """Plain-language status of one file id (for diagnostics)."""
        i = int(np.searchsorted(self._root_ids, file_data_id))
        if i >= len(self._root_ids) or self._root_ids[i] != file_data_id:
            return "not listed in the game's file table"
        ekeys = self.ckey_to_ekeys(bytes(self._root_keys[i])) or []
        if not ekeys:
            return "listed, but has no stored copy in the encoding file"
        local = sum(1 for e in ekeys if bytes(e[:9]) in self.index)
        return f"listed; {len(ekeys)} stored cop{'y' if len(ekeys) == 1 else 'ies'}, {local} on this PC"

    # ---- public
    def has(self, file_data_id):
        i = int(np.searchsorted(self._root_ids, file_data_id))
        return i < len(self._root_ids) and self._root_ids[i] == file_data_id

    def read(self, file_data_id):
        i = int(np.searchsorted(self._root_ids, file_data_id))
        if i >= len(self._root_ids) or self._root_ids[i] != file_data_id:
            raise CascError(f"FileDataID {file_data_id} is not in the root file")
        ckey = bytes(self._root_keys[i])
        ekeys = self.ckey_to_ekeys(ckey)
        if not ekeys:
            raise CascError(f"FileDataID {file_data_id}: content key not in the encoding file")
        last = None
        for attempt in (0, 1):
            for ekey in ekeys:               # use whichever stored copy is present locally
                if bytes(ekey[:9]) in self.index:
                    return self.read_ekey(ekey)
                last = ekey
            if attempt == 0 and not self.refresh_indexes():
                break
        raise CascError(f"FileDataID {file_data_id}: none of its {len(ekeys)} stored copies is in the local archives "
                        f"(stored key {bytes(last).hex()}); the install may not include this file")
