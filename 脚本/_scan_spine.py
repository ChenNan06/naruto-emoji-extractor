import struct, os, glob, time, re, json

def fast_lz4_decode(data, pos, chunk_size=0x20000):
    out = bytearray(); ip = pos; n = len(data)
    while len(out) < chunk_size:
        if ip >= n: break
        token = data[ip]; ip += 1
        lit_len = token >> 4
        if lit_len == 15:
            while ip < n:
                b = data[ip]; ip += 1; lit_len += b
                if b != 255: break
        if ip + lit_len > n: lit_len = n - ip
        out += data[ip:ip+lit_len]; ip += lit_len
        if len(out) >= chunk_size: break
        if ip + 2 > n: break
        offset = struct.unpack_from("<H", data, ip)[0]; ip += 2
        if offset == 0 or offset > len(out): return None, ip
        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while ip < n:
                b = data[ip]; ip += 1; match_len += b
                if b != 255: break
        start = len(out) - offset
        if match_len > chunk_size - len(out):
            match_len = chunk_size - len(out)
        if offset >= match_len:
            out += out[start:start+match_len]
        else:
            remaining = match_len
            while remaining > 0:
                take = min(offset, remaining)
                out += out[start:start+take]
                start += take
                remaining -= take
    return bytes(out), ip

def fast_decode_bundle(path):
    data = open(path, "rb").read()
    if not data.startswith(b"UnityKH"):
        return None
    p = 0
    p = data.index(b"\x00", p) + 1
    p += 4
    p = data.index(b"\x00", p) + 1
    p = data.index(b"\x00", p) + 1
    size = struct.unpack_from(">Q", data, p)[0]; p += 8
    comp = struct.unpack_from(">I", data, p)[0]; p += 4
    uncomp = struct.unpack_from(">I", data, p)[0]; p += 4
    flags = struct.unpack_from(">I", data, p)[0]; p += 4
    hdr_end = p
    start = (hdr_end + 15) & ~15
    data_start = (start + comp + 15) & ~15
    out = bytearray(); pos = data_start; n = len(data)
    while pos < n:
        chunk, npos = fast_lz4_decode(data, pos)
        if chunk is None: break
        out += chunk
        if npos <= pos: break
        pos = npos
    return bytes(out)

SPINE_KEYS = (b'"skeleton"', b'"bones"', b'"animations"', b'"skins"')

def is_spine(dec):
    for k in SPINE_KEYS:
        if k not in dec:
            return False
    return True

def extract_names(dec):
    # find the atlas/texture name pattern (bq_xxx) or the json name
    names = set()
    for m in re.finditer(rb'([a-zA-Z0-9_]{4,40}\.(?:atlas|png|json))', dec):
        s = m.group(1).decode('utf-8', 'ignore')
        names.add(s)
    # also look for "bq_" emoji pattern
    for m in re.finditer(rb'(bq_[a-zA-Z0-9_]{2,40})', dec):
        s = m.group(1).decode('utf-8', 'ignore')
        names.add(s)
    return sorted(names)

def main():
    dirs = sys_argv()
    results = []
    total = 0
    for d in dirs:
        files = glob.glob(os.path.join(d, "*.assetbundle"))
        for path in files:
            total += 1
            try:
                dec = fast_decode_bundle(path)
                if dec and is_spine(dec):
                    names = extract_names(dec)
                    results.append((os.path.basename(path), d, names))
            except Exception:
                pass
    # write results
    with open("spine_scan_result.txt", "w", encoding="utf-8") as f:
        f.write(f"scanned {total} bundles, found {len(results)} spine bundles\n")
        for fn, d, names in results:
            f.write(f"{d}\t{fn}\t{', '.join(names)}\n")
    print(f"scanned {total}, found {len(results)} spine bundles")

def sys_argv():
    import sys
    dirs = sys.argv[1:]
    if not dirs:
        dirs = ["E:/naruto/com.tencent.KiHan/files/999/bundle"]
    return dirs

if __name__ == "__main__":
    main()
