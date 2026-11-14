#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
02_vdi_extract.py —— 离线提取 MuMu 模拟器 data.vdi 里的 Android 文件（零窗口，不用起模拟器）

原理
----
MuMu 15 的 vms/<vm>/data.vdi 是 VirtualBox VDI 1.1 格式（魔数 0xBEDA107F），
只是签名注释写成了 "<<< QEMU VM Virtual Disk Image >>>"。
布局：512B 头 + 块映射表(offBlocks) + 数据区(offData)；块映射表每项 u32LE：
    0xFFFFFFFF = 未分配（读出来是 0）
    0xFFFFFFFE = 全零块（读出来是 0）
    其它       = 数据块号，物理偏移 = offData + 值 * blockSize
本脚本不整盘展开，直接做「虚拟偏移 -> 物理偏移」的随机读，然后：
    解析 MBR -> 找分区 -> 解析 ext4（superblock / group desc / inode / extent 树 / 目录项）
    -> 按路径把目标树写到本机目录（带稀疏跳洞、增量跳过、排除规则）。

用法见 --help；不传子命令时等价于 info。

注意：只读打开，绝不写 vdi。模拟器运行中也可以读（可能读到正在写的脏块）。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import struct
import sys
import time
from collections import OrderedDict

# ---------------------------------------------------------------- constants
VDI_MAGIC = 0xBEDA107F
FREE = 0xFFFFFFFF
ZERO = 0xFFFFFFFE

DEFAULT_VDI = r"E:\MuMuPlayer\vms\MuMuPlayer-15.0-0\data.vdi"

EXT4_EXTENTS_FL = 0x80000
EXT4_INLINE_DATA_FL = 0x10000000
EXT4_ENCRYPT_FL = 0x800
EXT4_HUGE_FILE_FL = 0x40000

S_IFMT = 0o170000
S_IFDIR = 0o040000
S_IFREG = 0o100000
S_IFLNK = 0o120000


# ---------------------------------------------------------------- logging
class Log:
    def __init__(self, path=None, quiet=False):
        self.path = path
        self.quiet = quiet
        self.fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg, level="INFO"):
        line = "%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
        if not self.quiet or level in ("WARN", "ERROR", "DONE"):
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("utf-8", "replace").decode("utf-8", "replace"))
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------- VDI
class VDIReader:
    """VirtualBox VDI 1.1 只读随机访问层。"""

    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda m, level="INFO": None)
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            self.header = f.read(4096)
        magic = struct.unpack_from("<I", self.header, 0x40)[0]
        if magic != VDI_MAGIC:
            raise SystemExit("不是 VDI 1.1：0x40 处魔数 = 0x%08X（期望 0x%08X）" % (magic, VDI_MAGIC))
        self.version = struct.unpack_from("<I", self.header, 0x44)[0]
        self.off_blocks = struct.unpack_from("<I", self.header, 0x154)[0]
        self.off_data = struct.unpack_from("<I", self.header, 0x158)[0]
        if not (0 < self.off_blocks < self.off_data <= self.size):
            raise SystemExit("头部偏移不合理：offBlocks=%d offData=%d size=%d"
                             % (self.off_blocks, self.off_data, self.size))
        n_entries = (self.off_data - self.off_blocks) // 4
        with open(path, "rb") as f:
            f.seek(self.off_blocks)
            raw = f.read(n_entries * 4)
        self.map = list(struct.unpack("<%dI" % n_entries, raw))
        self.n_entries = n_entries

        # blockSize 自动判定：要求 数据区末端 < 文件大小，且尽量贴近
        used = [e for e in self.map if e not in (FREE, ZERO)]
        max_blk = max(used) if used else 0
        best = None
        for bs in (1 << 20, 1 << 19, 1 << 21, 1 << 22, 4096):
            end = self.off_data + (max_blk + 1) * bs
            if end <= self.size:
                slack = self.size - end
                if best is None or slack < best[1]:
                    best = (bs, slack)
        if best is None:
            raise SystemExit("无法判定 blockSize（map 最大块号 %d 超出文件）" % max_blk)
        self.block_size = best[0]
        self.slack = best[1]
        self.nblocks = self.n_entries
        self.vsize = self.n_entries * self.block_size
        self.allocated = len(used)
        self.fh = open(path, "rb", buffering=0)
        self._cache = OrderedDict()
        self._cache_limit = 128          # 最多缓存 128 个数据块
        self._reads = 0
        self._bytes_read = 0
        self.log("VDI: %s" % path, "INFO")
        self.log("  magic=0x%08X ver=0x%08X offBlocks=%d offData=%d blockSize=%d (%.1f MiB)"
                 % (magic, self.version, self.off_blocks, self.off_data,
                    self.block_size, self.block_size / 2**20))
        self.log("  虚拟大小 %.2f GiB, 已分配块 %d, 数据区尾部余量 %d 字节"
                 % (self.vsize / 2**30, self.allocated, self.slack))

    # -- 基本读
    def _read_block(self, idx):
        """读一个完整虚拟块（带 LRU）。"""
        c = self._cache.get(idx)
        if c is not None:
            self._cache.move_to_end(idx)
            return c
        e = self.map[idx] if idx < self.nblocks else FREE
        if e in (FREE, ZERO):
            data = b"\x00" * self.block_size
        else:
            self.fh.seek(self.off_data + e * self.block_size)
            data = self.fh.read(self.block_size)
            if len(data) < self.block_size:
                data += b"\x00" * (self.block_size - len(data))
            self._reads += 1
            self._bytes_read += len(data)
        self._cache[idx] = data
        if len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return data

    def read_at(self, off, length):
        """随机读：跨块自动切分；相邻块若物理连续则合并成一次读。"""
        if length <= 0:
            return b""
        if off < 0:
            raise ValueError("负偏移")
        bs = self.block_size
        out = bytearray()
        cur = off
        remain = length
        while remain > 0:
            blk, inner = divmod(cur, bs)
            take = min(bs - inner, remain)
            if blk >= self.nblocks:
                out += b"\x00" * take
                cur += take
                remain -= take
                continue
            # 小块走缓存；大块直接物理读并合并相邻块
            if take <= 65536:
                out += self._read_block(blk)[inner:inner + take]
            else:
                e = self.map[blk]
                if e in (FREE, ZERO):
                    out += b"\x00" * take
                else:
                    # 合并：往后找 virt+1 -> phys+1 的连续块
                    run_blocks = 1
                    while True:
                        nb = blk + run_blocks
                        if nb >= self.nblocks:
                            break
                        if run_blocks * bs - inner >= take:
                            break
                        if self.map[nb] != e + run_blocks:
                            break
                        run_blocks += 1
                    need = take
                    got = 0
                    while need > 0:
                        self.fh.seek(self.off_data + (e + got // bs) * bs + inner + (got % bs))
                        # 一次最多读 8 MiB
                        want = min(need, 8 << 20)
                        d = self.fh.read(want)
                        if not d:
                            break
                        out += d
                        got += len(d)
                        need -= len(d)
                        self._reads += 1
                        self._bytes_read += len(d)
                        if len(d) < want:
                            break
                    if need > 0:
                        out += b"\x00" * need
            cur += take
            remain -= take
        return bytes(out)

    def stream_to(self, voff, length, out_fh):
        """把虚拟偏移 voff 起的 length 字节流到文件对象（相邻物理块尽可能合并成一次读）。

        注意：voff 是**虚拟盘字节偏移**；ext4 的块号必须先经 Ext4.block_off() 换算，
        不能直接拿来查 self.map（那张表是按本层 block_size 索引的）。
        """
        bs = self.block_size
        done = 0
        while done < length:
            blk, inner = divmod(voff + done, bs)
            take = min(bs - inner, length - done)
            e = self.map[blk] if blk < self.nblocks else FREE
            if e in (FREE, ZERO):
                out_fh.write(b"\x00" * take)
                done += take
                continue
            # 合并后续物理连续的块，一次最多 8 MiB
            run = 1
            while run * bs < (8 << 20):
                nb = blk + run
                if nb >= self.nblocks or self.map[nb] != e + run:
                    break
                if done + run * bs - inner >= length:
                    break
                run += 1
            want = min(length - done, run * bs - inner)
            self.fh.seek(self.off_data + e * bs + inner)
            got = 0
            while got < want:
                d = self.fh.read(want - got)
                if not d:
                    break
                out_fh.write(d)
                got += len(d)
            if got < want:
                out_fh.write(b"\x00" * (want - got))
            self._reads += 1
            self._bytes_read += got
            done += want

    def close(self):
        self.fh.close()


# ---------------------------------------------------------------- MBR
class MBR:
    def __init__(self, vdi):
        self.sector = vdi.read_at(0, 512)
        if self.sector[510:512] != b"\x55\xaa":
            raise SystemExit("虚拟盘 0 号扇区没有 MBR 签名 55AA")
        self.parts = []
        for i in range(4):
            e = self.sector[446 + i * 16: 446 + (i + 1) * 16]
            if e == b"\x00" * 16:
                continue
            ptype = e[4]
            lba, cnt = struct.unpack("<II", e[8:16])
            if lba == 0 or cnt == 0:
                continue
            self.parts.append({"index": i, "type": ptype, "start": lba * 512, "size": cnt * 512})


# ---------------------------------------------------------------- ext4
class Ext4:
    """最小只读 ext4 实现：superblock / 64 位 group desc / inode / extent 树 / 目录项。"""

    def __init__(self, vdi, base, log=None):
        self.vdi = vdi
        self.base = base
        self.log = log or (lambda m, level="INFO": None)
        sb = vdi.read_at(base + 1024, 1024)
        if struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
            raise SystemExit("偏移 %d 处不是 ext4 超级块" % base)
        self.sb = sb
        self.inodes_count = struct.unpack_from("<I", sb, 0x00)[0]
        self.blocks_lo = struct.unpack_from("<I", sb, 0x04)[0]
        self.first_data_block = struct.unpack_from("<I", sb, 0x14)[0]
        self.log_bs = struct.unpack_from("<I", sb, 0x18)[0]
        self.blocks_per_group = struct.unpack_from("<I", sb, 0x20)[0]
        self.inodes_per_group = struct.unpack_from("<I", sb, 0x28)[0]
        self.rev = struct.unpack_from("<I", sb, 0x4C)[0]
        self.inode_size = struct.unpack_from("<H", sb, 0x58)[0]
        self.incompat = struct.unpack_from("<I", sb, 0x60)[0]
        self.ro_compat = struct.unpack_from("<I", sb, 0x64)[0]
        self.desc_size = struct.unpack_from("<H", sb, 0xFE)[0]
        self.blocks_hi = struct.unpack_from("<I", sb, 0x150)[0]
        self.label = sb[0x78:0x88].rstrip(b"\x00").decode("utf-8", "replace")
        self.last_mounted = sb[0x88:0xC8].rstrip(b"\x00").decode("utf-8", "replace")
        if self.inode_size == 0:
            self.inode_size = 128
        if self.desc_size == 0:
            self.desc_size = 64 if (self.incompat & 0x80) else 32
        self.block_size = 1024 << self.log_bs
        self.nblocks = (self.blocks_hi << 32) | self.blocks_lo
        self.ngroups = (self.nblocks + self.blocks_per_group - 1) // self.blocks_per_group
        self.is64 = bool(self.incompat & 0x80)
        self.has_extents = bool(self.incompat & 0x40)
        if not self.has_extents:
            self.log("警告：文件系统未启用 extents，回退到 12 级直接块 + 间接块", "WARN")
        self._gd_cache = {}
        self.log("ext4 @%d: label='%s' last_mounted='%s' block=%d inode=%d blocks=%d(%.2f GiB) groups=%d desc=%d"
                 % (base, self.label, self.last_mounted, self.block_size, self.inode_size,
                    self.nblocks, self.nblocks * self.block_size / 2**30, self.ngroups, self.desc_size))
        if self.incompat & 0x10000:
            self.log("警告：incompat 带 ENCRYPT 标志，文件内容可能被 fscrypt 加密", "WARN")
        if self.incompat & 0x8000:
            self.log("警告：incompat 带 INLINE_DATA 标志，小文件可能内联存储", "WARN")

    # -- 低层
    def block_off(self, blk):
        return self.base + blk * self.block_size

    def group_desc(self, g):
        d = self._gd_cache.get(g)
        if d is not None:
            return d
        gd_block = self.first_data_block + 1 + (g * self.desc_size) // self.block_size
        gd_off = (g * self.desc_size) % self.block_size
        raw = self.vdi.read_at(self.block_off(gd_block) + gd_off, self.desc_size)
        itab_lo = struct.unpack_from("<I", raw, 0x08)[0]
        itab_hi = struct.unpack_from("<I", raw, 0x28)[0] if self.desc_size >= 64 else 0
        d = {"inode_table": ((itab_hi << 32) | itab_lo) if self.is64 else itab_lo}
        self._gd_cache[g] = d
        return d

    def inode(self, ino):
        if ino < 1 or ino > self.inodes_count:
            raise ValueError("inode 越界: %d" % ino)
        g, idx = divmod(ino - 1, self.inodes_per_group)
        itab = self.group_desc(g)["inode_table"]
        off = self.block_off(itab) + idx * self.inode_size
        raw = self.vdi.read_at(off, self.inode_size)
        mode = struct.unpack_from("<H", raw, 0x00)[0]
        size_lo = struct.unpack_from("<I", raw, 0x04)[0]
        flags = struct.unpack_from("<I", raw, 0x20)[0]
        size_high = struct.unpack_from("<I", raw, 0x6C)[0]
        size = size_lo | (size_high << 32) if (self.is64 or size_high) else size_lo
        if flags & EXT4_HUGE_FILE_FL and size_high:
            size = size_lo | (size_high << 32)
        return {
            "ino": ino, "mode": mode, "size": size, "flags": flags,
            "links": struct.unpack_from("<H", raw, 0x1A)[0],
            "blocks": struct.unpack_from("<I", raw, 0x1C)[0],
            "mtime": struct.unpack_from("<I", raw, 0x10)[0],
            "block": raw[0x28:0x28 + 60],
            "raw": raw,
        }

    # -- 数据块映射
    def _extent_tree(self, node, depth, out):
        entries = struct.unpack_from("<H", node, 2)[0]
        if depth == 0:
            for i in range(entries):
                off = 12 + i * 12
                ee_block, ee_len, ee_start_hi, ee_start_lo = struct.unpack_from("<IHHI", node, off)
                uninit = ee_len > 32768
                ln = (ee_len - 32768) if uninit else ee_len
                if ln == 0:
                    continue
                out.append({
                    "logical": ee_block,
                    "phys": (ee_start_hi << 32) | ee_start_lo,
                    "len": ln,
                    "uninit": uninit,
                })
            return
        for i in range(entries):
            off = 12 + i * 12
            leaf_lo = struct.unpack_from("<I", node, off + 4)[0]
            leaf_hi = struct.unpack_from("<H", node, off + 8)[0]
            leaf = (leaf_hi << 32) | leaf_lo
            child = self.vdi.read_at(self.block_off(leaf), self.block_size)
            if struct.unpack_from("<H", child, 0)[0] != 0xF30A:
                raise IOError("坏 extent 索引块 %d（魔数不符）" % leaf)
            self._extent_tree(child, depth - 1, out)

    def extents(self, ino_d):
        if ino_d["flags"] & EXT4_EXTENTS_FL:
            hdr = ino_d["block"]
            if struct.unpack_from("<H", hdr, 0)[0] != 0xF30A:
                return []
            depth = struct.unpack_from("<H", hdr, 6)[0]
            out = []
            self._extent_tree(hdr, depth, out)
            return out
        # 传统 12 直接 + 单/双/三级间址
        out = []
        ptrs = struct.unpack_from("<15I", ino_d["block"], 0)
        for i in range(12):
            if ptrs[i]:
                out.append({"logical": i, "phys": ptrs[i], "len": 1, "uninit": False})
        per = self.block_size // 4
        if ptrs[12]:
            self._walk_indirect(ptrs[12], 1, out, 12)
        if ptrs[13]:
            self._walk_indirect(ptrs[13], 2, out, 12 + per)
        if ptrs[14]:
            self._walk_indirect(ptrs[14], 3, out, 12 + per + per * per)
        return out

    def _walk_indirect(self, blk, level, out, start_logical):
        data = self.vdi.read_at(self.block_off(blk), self.block_size)
        per = self.block_size // 4
        ptrs = struct.unpack("<%dI" % per, data)
        for i, p in enumerate(ptrs):
            if not p:
                continue
            logical = start_logical + i * (per ** (level - 1))
            if level == 1:
                out.append({"logical": logical, "phys": p, "len": 1, "uninit": False})
            else:
                self._walk_indirect(p, level - 1, out, logical)

    def iter_file(self, ino_d, fh=None, limit=None):
        """按 extent 顺序把内容流式写出；返回写出字节数。"""
        size = ino_d["size"]
        if limit is not None:
            size = min(size, limit)
        written = 0
        exts = sorted(self.extents(ino_d), key=lambda e: e["logical"])
        pos = 0
        for e in exts:
            log_start = e["logical"] * self.block_size
            if log_start >= size:
                break
            if log_start > pos:
                hole = min(log_start - pos, size - pos)
                if fh:
                    fh.write(b"\x00" * hole)
                written += hole
                pos += hole
            n = min(e["len"] * self.block_size, size - pos)
            if n <= 0:
                break
            if e["uninit"]:
                if fh:
                    fh.write(b"\x00" * n)
            else:
                self.vdi.stream_to(self.block_off(e["phys"]), n,
                                   fh if fh else _NullWriter())
            written += n
            pos += n
        if written < size:
            if fh:
                fh.write(b"\x00" * (size - written))
            written = size
        return written

    def read_file(self, ino_d, offset=0, length=None):
        size = ino_d["size"]
        if length is None:
            length = size - offset
        length = max(0, min(length, size - offset))
        buf = bytearray()
        for e in sorted(self.extents(ino_d), key=lambda x: x["logical"]):
            log_start = e["logical"] * self.block_size
            log_end = log_start + e["len"] * self.block_size
            a = max(log_start, offset)
            b = min(log_end, offset + length)
            if b <= a:
                continue
            if len(buf) < a - offset:
                buf += b"\x00" * (a - offset - len(buf))
            inner = a - log_start
            if e["uninit"]:
                buf += b"\x00" * (b - a)
            else:
                buf += self.vdi.read_at(self.block_off(e["phys"]) + inner, b - a)
        if len(buf) < length:
            buf += b"\x00" * (length - len(buf))
        return bytes(buf)

    # -- 目录
    def ls(self, ino_d):
        """返回 name -> (ino, file_type)；兼容 extent 化 + htree 化目录。"""
        if not (ino_d["mode"] & S_IFMT) == S_IFDIR:
            raise SystemExit("inode %d 不是目录" % ino_d["ino"])
        entries = {}
        blocks = []
        for e in sorted(self.extents(ino_d), key=lambda x: x["logical"]):
            for i in range(e["len"]):
                blocks.append((e["logical"] + i, e["phys"] + i if not e["uninit"] else None))
        for _, phys in blocks:
            if phys is None:
                continue
            data = self.vdi.read_at(self.block_off(phys), self.block_size)
            off = 0
            while off + 8 <= len(data):
                ino, rec_len, name_len, ftype = struct.unpack_from("<IHBB", data, off)
                if rec_len == 0:
                    break
                if ino != 0 and name_len > 0:
                    name = data[off + 8: off + 8 + name_len].decode("utf-8", "surrogateescape")
                    entries[name] = (ino, ftype)
                off += rec_len
        return entries

    def resolve(self, path):
        """绝对路径 -> inode 字典。

        注意：/data 分区里的 ext4 根目录**就是** Android 的 /data，
        所以 Android 路径 `/data/data/com.tencent.KiHan` 在镜像里是
        `/data/com.tencent.KiHan`。这里两种写法都接受：
        先按原样解析，失败且以 /data/ 开头就去掉这个前缀再试一次。
        """
        # 归一 Android 常见写法 -> 镜像内真实路径（ext4 根 == Android 的 /data）
        _n = path.replace(chr(92) + chr(92), '/')
        for _pre in ('/sdcard/', '/storage/emulated/0/', '/data/media/0/'):
            if _n.startswith(_pre):
                _n = '/media/0/' + _n[len(_pre):]
                break
        path = _n
        try:
            return self._resolve_raw(path)
        except SystemExit:
            norm = path.replace("\\", "/")
            # fs 根 == Android /data，所以 Android 绝对路径去掉开头的 /data 就是 fs 路径
            if norm.startswith("/data/"):
                alt = norm[5:]
            elif norm.rstrip("/") == "/data":
                alt = "/"
            else:
                raise
            if alt.lstrip("/") and alt != norm:
                return self._resolve_raw(alt)
            if alt == "/":
                return self.inode(2)
            raise

    def _resolve_raw(self, path):
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        ino = self.inode(2)
        for p in parts:
            if p == "..":
                ino = self.inode(2)
                continue
            ents = self.ls(ino)
            if p not in ents:
                # 文件系统开了 CASEFOLD 时内核做大小写不敏感查找，这里手动兜底
                low = p.lower()
                cand = [k for k in ents if k.lower() == low]
                if len(cand) == 1:
                    p = cand[0]
                else:
                    raise SystemExit("路径不存在: %s（在 '%s' 里找不到）" % (path, p))
            ino = self.inode(ents[p][0])
        return ino

    def walk(self, ino_d, max_depth=999):
        """深度优先遍历，yield (相对路径, inode 字典)；根自己也会被 yield（rel=""）。"""
        stack = [("", ino_d, 0)]
        while stack:
            rel, d, depth = stack.pop()
            yield rel, d
            if depth >= max_depth or (d["mode"] & S_IFMT) != S_IFDIR:
                continue
            try:
                ents = self.ls(d)
            except Exception as exc:                       # noqa: BLE001
                self.log("列目录失败 ino=%d: %s" % (d["ino"], exc), "WARN")
                continue
            children = []
            for name, (ino, _ft) in sorted(ents.items()):
                if name in (".", ".."):
                    continue
                try:
                    child = self.inode(ino)
                except Exception as exc:                   # noqa: BLE001
                    self.log("读 inode 失败 %s/%s: %s" % (rel, name, exc), "WARN")
                    continue
                children.append(((rel + "/" + name) if rel else name, child, depth + 1))
            stack.extend(reversed(children))


class _NullWriter:
    def write(self, b):
        return len(b)


# ---------------------------------------------------------------- 分区发现
def pick_data_partition(vdi, log):
    mbr = MBR(vdi)
    cands = []
    for p in mbr.parts:
        try:
            fs = Ext4(vdi, p["start"], log)
        except SystemExit:
            log("分区 %d (off=%d, %.2f GiB) 不是 ext4，跳过" % (p["index"], p["start"], p["size"] / 2**30))
            continue
        cands.append((p, fs))
        log("分区 %d: off=%d type=0x%02X ext4 label='%s' last_mounted='%s' %.2f GiB"
            % (p["index"], p["start"], p["type"], fs.label, fs.last_mounted, p["size"] / 2**30))
    if not cands:
        raise SystemExit("没有找到任何 ext4 分区")
    # 优先 last_mounted == /data，其次容量最大
    for p, fs in cands:
        if fs.last_mounted == "/data":
            return p, fs
    cands.sort(key=lambda x: -x[0]["size"])
    return cands[0]


# ---------------------------------------------------------------- 命令实现
def cmd_info(args, log):
    vdi = VDIReader(args.vdi, log)
    mbr = MBR(vdi)
    print("\n=== 分区表 ===")
    for p in mbr.parts:
        print("  part%d type=0x%02X start=%d (vblk %.3f) size=%.3f GiB"
              % (p["index"], p["type"], p["start"], p["start"] / vdi.block_size, p["size"] / 2**30))
    part, fs = pick_data_partition(vdi, log)
    print("\n=== 选用分区 part%d (off=%d) ===" % (part["index"], part["start"]))
    root = fs.inode(2)
    print("根目录 inode=2 条目数=%d" % len(fs.ls(root)))
    for name, (ino, ftype) in sorted(fs.ls(root).items()):
        if name in (".", ".."):
            continue
        try:
            d = fs.inode(ino)
            kind = "dir" if (d["mode"] & S_IFMT) == S_IFDIR else (
                "file" if (d["mode"] & S_IFMT) == S_IFREG else "other")
            print("  %-28s ino=%-10d %-5s size=%-12d mode=%o" % (name, ino, kind, d["size"], d["mode"]))
        except Exception as exc:                            # noqa: BLE001
            print("  %-28s ino=%-10d <读取失败 %s>" % (name, ino, exc))
    vdi.close()
    return 0


def cmd_ls(args, log):
    vdi = VDIReader(args.vdi, log)
    _part, fs = pick_data_partition(vdi, log)
    d = fs.resolve(args.path)
    if (d["mode"] & S_IFMT) != S_IFDIR:
        print("%s 是文件 size=%d" % (args.path, d["size"]))
        vdi.close()
        return 0
    ents = fs.ls(d)
    rows = []
    for name, (ino, ftype) in sorted(ents.items()):
        if name in (".", ".."):
            continue
        c = fs.inode(ino)
        isdir = (c["mode"] & S_IFMT) == S_IFDIR
        rows.append((name, ino, isdir, c["size"], c["flags"]))
    print("%s  (%d 项)" % (args.path, len(rows)))
    for name, ino, isdir, size, flags in rows[: args.limit or len(rows)]:
        mark = ""
        if flags & EXT4_ENCRYPT_FL:
            mark += " [ENCRYPTED]"
        if flags & EXT4_INLINE_DATA_FL:
            mark += " [INLINE]"
        print("  %s %-50s ino=%-10d %s%s" % ("d" if isdir else "-", name, ino,
                                             "%d" % size if not isdir else "", mark))
    vdi.close()
    return 0


def cmd_tree(args, log):
    vdi = VDIReader(args.vdi, log)
    _part, fs = pick_data_partition(vdi, log)
    root = fs.resolve(args.path)
    files = dirs = 0
    total = 0
    t0 = time.time()
    for rel, d in fs.walk(root, max_depth=args.depth):
        isdir = (d["mode"] & S_IFMT) == S_IFDIR
        if isdir:
            dirs += 1
        else:
            files += 1
            total += d["size"]
        if args.verbose:
            print("%s %-70s %12d" % ("d" if isdir else "-", rel or ".", d["size"] if not isdir else 0))
    log("tree 完成：目录 %d，文件 %d，合计 %.3f GiB，用时 %.1fs"
        % (dirs, files, total / 2**30, time.time() - t0), "DONE")
    vdi.close()
    return 0


def cmd_extract(args, log):
    dest = os.path.abspath(args.dest)
    os.makedirs(dest, exist_ok=True)
    vdi = VDIReader(args.vdi, log)
    _part, fs = pick_data_partition(vdi, log)
    root = fs.resolve(args.path)
    log("源路径 %s (ino=%d)  ->  %s" % (args.path, root["ino"], dest))

    excludes = args.exclude or []
    stats = {"files": 0, "dirs": 0, "bytes": 0, "skipped": 0, "errors": 0, "dry": 0}
    t0 = time.time()
    last_report = t0
    panic = args.max_seconds
    stack = [("", root)]
    while stack:
        rel, d = stack.pop()
        isdir = (d["mode"] & S_IFMT) == S_IFDIR
        local = os.path.join(dest, rel.replace("/", os.sep)) if rel else dest
        if isdir:
            stats["dirs"] += 1
            if not args.dry_run:
                os.makedirs("\\\\?\\" + local if os.name == "nt" else local, exist_ok=True)
            try:
                ents = fs.ls(d)
            except Exception as exc:                       # noqa: BLE001
                log("列目录失败 %s: %s" % (rel, exc), "WARN")
                stats["errors"] += 1
                continue
            for name, (ino, _ft) in sorted(ents.items()):
                if name in (".", ".."):
                    continue
                child_rel = (rel + "/" + name) if rel else name
                if any(fnmatch.fnmatch(child_rel, pat) or fnmatch.fnmatch(name, pat)
                       for pat in excludes):
                    stats["skipped"] += 1
                    continue
                try:
                    child = fs.inode(ino)
                except Exception as exc:                   # noqa: BLE001
                    log("读 inode 失败 %s: %s" % (child_rel, exc), "WARN")
                    stats["errors"] += 1
                    continue
                stack.append((child_rel, child))
        else:
            if (d["mode"] & S_IFMT) == S_IFLNK:
                stats["skipped"] += 1
                continue
            stats["files"] += 1
            stats["bytes"] += d["size"]
            if args.dry_run:
                stats["dry"] += 1
                if stats["dry"] % 5000 == 0:
                    log("dry-run 扫描：%d 文件 %.2f GiB" % (stats["files"], stats["bytes"] / 2**30))
                continue
            try:
                dirname = os.path.dirname(local)
                if dirname and not os.path.isdir(dirname):
                    os.makedirs("\\\\?\\" + dirname if os.name == "nt" else dirname, exist_ok=True)
                wpath = "\\\\?\\" + local if os.name == "nt" else local
                if args.incremental and os.path.exists(wpath):
                    if os.path.getsize(wpath) == d["size"] and not args.force:
                        stats.setdefault("exists", 0)
                        stats["exists"] += 1
                        continue
                if d["flags"] & EXT4_ENCRYPT_FL:
                    log("文件带 ENCRYPT 标志（内容可能是密文）：%s" % rel, "WARN")
                tmp = wpath + ".part"
                with open(tmp, "wb") as fh:
                    n = fs.iter_file(d, fh)
                os.replace(tmp, wpath)
                if n != d["size"]:
                    log("大小不符 %s: 写入 %d 期望 %d" % (rel, n, d["size"]), "WARN")
            except Exception as exc:                       # noqa: BLE001
                stats["errors"] += 1
                log("写文件失败 %s: %s" % (rel, exc), "WARN")
            now = time.time()
            if now - last_report > 10:
                last_report = now
                log("进度：文件 %d / %.2f GiB / 读 %.2f GiB / %.1f s"
                    % (stats["files"], stats["bytes"] / 2**30, vdi._bytes_read / 2**30, now - t0))
            if panic and (now - t0) > panic:
                log("达到 --max-seconds=%s 上限，提前收工（已写 %d 文件）"
                    % (panic, stats["files"]), "WARN")
                break

    dur = time.time() - t0
    stats["seconds"] = round(dur, 1)
    stats["vdi_bytes_read"] = vdi._bytes_read
    log("extract 完成：文件 %d，目录 %d，数据 %.3f GiB，跳过 %d，错误 %d，用时 %.1fs，均速 %.1f MiB/s"
        % (stats["files"], stats["dirs"], stats["bytes"] / 2**30, stats["skipped"],
           stats["errors"], dur, (stats["bytes"] / 2**20) / max(dur, 0.001)), "DONE")
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"path": args.path, "dest": dest, "stats": stats,
                       "exclude": excludes, "dry_run": args.dry_run}, f, ensure_ascii=False, indent=2)
        log("统计写入 %s" % args.json)
    vdi.close()
    return 0


def cmd_cat(args, log):
    """拉单个文件到本机（用于 metadata 这种大文件校验）。"""
    vdi = VDIReader(args.vdi, log)
    _part, fs = pick_data_partition(vdi, log)
    d = fs.resolve(args.path)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    t0 = time.time()
    with open(args.out, "wb") as fh:
        n = fs.iter_file(d, fh, limit=args.limit)
    log("cat %s -> %s (%d 字节, 用时 %.1fs)" % (args.path, args.out, n, time.time() - t0), "DONE")
    vdi.close()
    return 0


# ---------------------------------------------------------------- CLI
def build_parser():
    p = argparse.ArgumentParser(
        prog="02_vdi_extract.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="离线读取 MuMu data.vdi（VDI 1.1 -> MBR -> ext4）并提取 /data 下的文件，零窗口、不需要起模拟器。",
        epilog="""示例:
  # 盘结构 / 分区 / 根目录
  python 02_vdi_extract.py info

  # 列目录
  python 02_vdi_extract.py ls --path /data/data/com.tencent.KiHan

  # 只统计不落盘（先看体量）
  python 02_vdi_extract.py extract --path /data/data/com.tencent.KiHan/files --dest work\\vdi-extract --dry-run

  # 真拉，排除 UnityCache / Audio
  python 02_vdi_extract.py extract --path /data/data/com.tencent.KiHan/files ^
      --dest work\\vdi-extract --exclude "*UnityCache*" --exclude "*Audio*" --json logs\\pull.json

  # 单文件（比如 global-metadata.dat）
  python 02_vdi_extract.py cat --path /data/data/com.tencent.KiHan/files/il2cpp/Metadata/global-metadata.dat --out work\\global-metadata.dat
""")
    p.add_argument("--vdi", default=DEFAULT_VDI, help="data.vdi 路径（默认 %(default)s）")
    p.add_argument("--log", default=None, help="日志文件路径（默认 logs\\02_vdi_extract.log）")
    p.add_argument("--quiet", action="store_true", help="只打印 WARN/ERROR/DONE")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("info", help="打印 VDI 头、分区表、/data 根目录")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("ls", help="列目录")
    s.add_argument("--path", required=True, help="ext4 内绝对路径，如 /data/data/com.tencent.KiHan")
    s.add_argument("--limit", type=int, default=200, help="最多显示多少项（0=全部）")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser("tree", help="递归统计某个子树（文件数/字节），不落盘")
    s.add_argument("--path", required=True)
    s.add_argument("--depth", type=int, default=999)
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_tree)

    s = sub.add_parser("extract", help="把一棵子树完整提取到本机目录")
    s.add_argument("--path", required=True, help="ext4 内绝对路径")
    s.add_argument("--dest", required=True, help="本机目标目录")
    s.add_argument("--exclude", action="append", default=[],
                   help="排除匹配（对相对路径和文件名做 fnmatch，可多次给）")
    s.add_argument("--dry-run", action="store_true", help="只统计不写盘（默认建议先跑这个）")
    s.add_argument("--incremental", action="store_true", help="已存在且大小一致就跳过")
    s.add_argument("--force", action="store_true", help="覆盖已存在文件")
    s.add_argument("--json", default=None, help="把统计写到这个 json")
    s.add_argument("--max-seconds", type=float, default=0, help="超时提前收工（0=不限）")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("cat", help="取单个文件")
    s.add_argument("--path", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_cat)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        args.func = cmd_info
    if not args.log:
        here = os.path.dirname(os.path.abspath(__file__))
        args.log = os.path.join(os.path.dirname(here), "logs", "02_vdi_extract.log")
    log = Log(args.log, quiet=args.quiet)
    log("=== 02_vdi_extract.py %s ===" % (getattr(args, "cmd", "info")))
    try:
        return args.func(args, log)
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
