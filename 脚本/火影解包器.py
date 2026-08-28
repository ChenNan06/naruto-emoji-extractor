# -*- coding: utf-8 -*-
"""
火影忍者手游 资源解包器（一体化）
================================
功能：
  1. 解包 KHFS 加密的 AssetBundle（火影自定义加密格式）
  2. 提取任意资源（贴图/文本/精灵/字体等）
  3. 识别并渲染 Spine 动态表情为 GIF

用法：
  python 火影解包器.py extract <bundle文件或目录> [-o 输出目录]
  python 火影解包器.py emoji   <bundle目录>         [-o 输出目录]
  python 火影解包器.py render  <skeleton.json> <atlas.txt> <texture.png> [-o 输出]

说明：
  KHFS 格式 = 标准 UnityFS 头（签名换成 UnityKHFS/UnityKH1FS，版本=8）
              + 加密的目录表 + 未加密的 LZ4 数据块（每块 128KB）。
  本工具跳过加密目录，直接按 128KB 分块 LZ4 解压，得到标准 SerializedFile。
"""
import struct, os, sys, json, math, glob, argparse
import numpy as np
from PIL import Image

CHUNK = 0x20000  # 128KB
D2R = math.pi / 180.0

# ====================================================================
# 第一部分：KHFS 解包
# ====================================================================

def parse_header(data):
    p = 0
    end = data.index(b"\x00", p); sig = data[p:end]; p = end + 1
    version = struct.unpack_from(">I", data, p)[0]; p += 4
    end = data.index(b"\x00", p); vp = data[p:end]; p = end + 1
    end = data.index(b"\x00", p); ve = data[p:end]; p = end + 1
    size = struct.unpack_from(">Q", data, p)[0]; p += 8
    comp = struct.unpack_from(">I", data, p)[0]; p += 4
    uncomp = struct.unpack_from(">I", data, p)[0]; p += 4
    flags = struct.unpack_from(">I", data, p)[0]; p += 4
    return dict(sig=sig, version=version, comp=comp, uncomp=uncomp, flags=flags, hdr_end=p)

def decode_chunk(data, pos):
    """解码一个 LZ4 块（128KB 未压缩），返回 (输出字节, 下一压缩偏移)。"""
    out = bytearray(); ip = pos; n = len(data)
    while len(out) < CHUNK:
        if ip >= n: break
        token = data[ip]; ip += 1
        lit_len = token >> 4
        if lit_len == 15:
            while ip < n:
                b = data[ip]; ip += 1; lit_len += b
                if b != 255: break
        if ip + lit_len > n: lit_len = n - ip
        out += data[ip:ip+lit_len]; ip += lit_len
        if len(out) >= CHUNK: break
        if ip + 2 > n: break
        offset = struct.unpack_from("<H", data, ip)[0]; ip += 2
        if offset == 0 or offset > len(out): return None, ip
        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while ip < n:
                b = data[ip]; ip += 1; match_len += b
                if b != 255: break
        start_idx = len(out) - offset
        for i in range(match_len):
            if len(out) >= CHUNK: break
            out.append(out[start_idx + i])
    return bytes(out), ip

def decode_bundle(path):
    """解码 KHFS bundle，返回解压后的 SerializedFile 数据。"""
    data = open(path, "rb").read()
    if not data.startswith(b"UnityKH"):
        raise ValueError("不是 KHFS bundle: " + path)
    h = parse_header(data)
    start = (h["hdr_end"] + 15) & ~15
    data_start = (start + h["comp"] + 15) & ~15
    out = bytearray(); pos = data_start; n = len(data)
    while pos < n:
        chunk, npos = decode_chunk(data, pos)
        if chunk is None: break
        out += chunk
        if npos <= pos: break
        pos = npos
    return bytes(out), h

def is_khfs(path):
    with open(path, "rb") as f:
        return f.read(7) == b"UnityKH"

# ====================================================================
# 第二部分：资源提取（借助 UnityPy）
# ====================================================================

def extract_bundle(path, out_dir):
    """提取单个 bundle 的所有资源。返回提取统计。"""
    from UnityPy import Environment
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.files.SerializedFile import SerializedFile
    from UnityPy.export.Texture2DConverter import parse_image_data

    decoded, h = decode_bundle(path)
    file_size = struct.unpack_from(">Q", decoded, 0x18)[0]
    env = Environment()
    sf = SerializedFile(EndianBinaryReader(decoded), parent=env)
    ab_name = sf.assetbundle.m_Name if sf.assetbundle else "bundle"
    base = os.path.splitext(os.path.basename(path))[0]
    count = {"Texture2D": 0, "Sprite": 0, "TextAsset": 0}

    for pid, o in sf.objects.items():
        tn = o.type.name
        try:
            obj = o.read()
        except Exception:
            continue
        nm = (getattr(obj, "m_Name", "") or str(pid)).replace("/", "_").replace("\\", "_")
        try:
            if tn == "TextAsset":
                d = obj.m_Script
                if isinstance(d, str): d = d.encode("utf-8")
                d = bytes(d)
                ext = ".json" if d.lstrip()[:1] == b"{" else ".txt"
                with open(os.path.join(out_dir, base + "__" + nm + ext), "wb") as f:
                    f.write(d)
                count["TextAsset"] += 1
            elif tn == "Texture2D":
                sd = getattr(obj, "m_StreamData", None)
                if sd is not None and getattr(sd, "size", 0) > 0:
                    res = decoded[file_size + sd.offset : file_size + sd.offset + sd.size]
                    img = parse_image_data(res, obj.m_Width, obj.m_Height, obj.m_TextureFormat, (2021, 3, 16), 13, None, flip=True)
                else:
                    img = obj.image
                img.save(os.path.join(out_dir, base + "__" + nm + ".png"))
                count["Texture2D"] += 1
            elif tn == "Sprite":
                img = obj.image
                img.save(os.path.join(out_dir, base + "__" + nm + ".png"))
                count["Sprite"] += 1
        except Exception:
            pass
    return ab_name, count

# ====================================================================
# 第三部分：Spine 渲染
# ====================================================================

class Region:
    __slots__ = ("name", "x", "y", "w", "h", "ow", "oh", "ox", "oy", "degrees")
    def __init__(self, name):
        self.name = name; self.x = 0; self.y = 0; self.w = 0; self.h = 0
        self.ow = 0; self.oh = 0; self.ox = 0; self.oy = 0; self.degrees = 0

def parse_atlas(text):
    regions = {}; cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("//"): continue
        if ":" in line:
            k, v = line.split(":", 1); k = k.strip(); v = v.strip()
            if cur is None: continue
            if k == "rotate":
                if v == "true": cur.degrees = 90
                elif v == "false": cur.degrees = 0
                else: cur.degrees = int(v)  # 180 / 270
            elif k == "xy": cur.x, cur.y = map(int, v.split(","))
            elif k == "size": cur.w, cur.h = map(int, v.split(","))
            elif k == "orig": cur.ow, cur.oh = map(int, v.split(","))
            elif k == "offset": cur.ox, cur.oy = map(int, v.split(","))
        else:
            if line[0].isspace() or line.endswith(".png") or line.startswith(("format", "filter", "repeat", "pma")):
                continue
            cur = Region(line); regions[line] = cur
    return regions

def uv_to_atlas(reg, u, v):
    # 与 Spine TextureAtlasRegion.updateUVs 逐像素对齐，含 offset/orig!=size 修正。
    x, y = reg.x, reg.y
    w, h = reg.w, reg.h          # size（打包后尺寸）
    ow, oh = reg.ow, reg.oh      # orig（原始尺寸）
    ox, oy = reg.ox, reg.oy      # offset（白边裁剪偏移）
    if reg.degrees == 90:
        px = x - (oh - oy - h) + v * oh
        py = y - (ow - ox - w) + (1.0 - u) * ow
        return px, py
    if reg.degrees == 180:
        px = x - (ow - ox - w) + (1.0 - u) * ow
        py = y - oy + (1.0 - v) * oh
        return px, py
    if reg.degrees == 270:
        px = x - oy + (1.0 - v) * oh
        py = y - ox + u * ow
        return px, py
    # 0 度
    px = x - ox + u * ow
    py = y - (oh - oy - h) + v * oh
    return px, py

def region_uvs(reg):
    # RegionAttachment.setRegion 的直接 UV(图集像素坐标, 与 spine-core.js 逐像素一致)。
    # 顺序 [左下, 右下, 右上, 左上](与 render_frame 角点顺序一致)。
    x, y = reg.x, reg.y
    w, h = reg.w, reg.h
    if reg.degrees == 90:
        u2, v2 = x + h, y + w
        return [(u2, v2), (u2, y), (x, y), (x, v2)]
    # 0/180/270 用非 rotate 分支(u2=x+w, v2=y+h; w/h 为原始值, 180/270 时可能为负)
    u2, v2 = x + w, y + h
    return [(x, v2), (u2, v2), (u2, y), (x, y)]

class Bone:
    __slots__ = ("name", "parent", "x", "y", "rotation", "scaleX", "scaleY", "shearX", "shearY",
                 "length", "transform", "a", "b", "c", "d", "worldX", "worldY",
                 "ax", "ay", "arotation", "ascaleX", "ascaleY", "ashearX", "ashearY")
    def __init__(self, d):
        self.name = d.get("name"); self.parent = None
        self.x = d.get("x", 0.0); self.y = d.get("y", 0.0)
        self.rotation = d.get("rotation", 0.0)
        self.scaleX = d.get("scaleX", 1.0); self.scaleY = d.get("scaleY", 1.0)
        self.shearX = d.get("shearX", 0.0); self.shearY = d.get("shearY", 0.0)
        self.length = d.get("length", 0.0); self.transform = d.get("transform", "normal")
        self.a = 1.0; self.b = 0.0; self.c = 0.0; self.d = 1.0; self.worldX = 0.0; self.worldY = 0.0
        self.ax = self.ay = 0.0; self.arotation = 0.0
        self.ascaleX = self.ascaleY = 1.0; self.ashearX = self.ashearY = 0.0

class Slot:
    __slots__ = ("name", "bone", "attachment", "color", "blend", "data")
    def __init__(self, d):
        self.name = d.get("name"); self.bone = d.get("bone")
        self.attachment = d.get("attachment"); self.color = d.get("color", "ffffffff")
        self.blend = d.get("blend", "normal"); self.data = d

class Skeleton:
    def __init__(self, data):
        self.data = data
        self.bones = {}; self.bone_list = []
        for bd in data["bones"]:
            b = Bone(bd); self.bones[b.name] = b; self.bone_list.append(b)
        for i, bd in enumerate(data["bones"]):
            pn = bd.get("parent")
            if pn and pn in self.bones: self.bone_list[i].parent = self.bones[pn]
        self.slots = {}; self.slot_list = []
        for sd in data.get("slots", []):
            s = Slot(sd); self.slots[s.name] = s; self.slot_list.append(s)
        self.skins = data.get("skins", [])
        self.animations = data.get("animations", {})
        self.ik = data.get("ik", [])
        self.transform = data.get("transform", [])
        self.skin_attachments = self.skins[0].get("attachments", {}) if self.skins else {}

    def reset(self):
        for i, bd in enumerate(self.data["bones"]):
            b = self.bone_list[i]
            b.x = bd.get("x", 0.0); b.y = bd.get("y", 0.0); b.rotation = bd.get("rotation", 0.0)
            b.scaleX = bd.get("scaleX", 1.0); b.scaleY = bd.get("scaleY", 1.0)
            b.shearX = bd.get("shearX", 0.0); b.shearY = bd.get("shearY", 0.0)
        for s in self.slot_list:
            s.attachment = s.data.get("attachment"); s.color = s.data.get("color", "ffffffff")

def compute_world(sk):
    for b in sk.bone_list:
        r = b.rotation * D2R; sx = b.shearX * D2R; sy = b.shearY * D2R
        if b.parent is None:
            la = math.cos(r + sx) * b.scaleX
            lb = -math.sin(r + sy) * b.scaleY
            lc = math.sin(r + sx) * b.scaleX
            ld = math.cos(r + sy) * b.scaleY
            b.a = la; b.b = lb; b.c = lc; b.d = ld
            b.worldX = b.x; b.worldY = b.y
            continue
        p = b.parent
        pa, pb, pc, pd = p.a, p.b, p.c, p.d
        b.worldX = pa * b.x + pb * b.y + p.worldX
        b.worldY = pc * b.x + pd * b.y + p.worldY
        tm = b.transform
        if tm == "normal":
            la = math.cos(r + sx) * b.scaleX
            lb = -math.sin(r + sy) * b.scaleY
            lc = math.sin(r + sx) * b.scaleX
            ld = math.cos(r + sy) * b.scaleY
            b.a = pa * la + pb * lc; b.b = pa * lb + pb * ld
            b.c = pc * la + pd * lc; b.d = pc * lb + pd * ld
        elif tm == "onlyTranslation":
            la = math.cos(r + sx) * b.scaleX
            lb = -math.sin(r + sy) * b.scaleY
            lc = math.sin(r + sx) * b.scaleX
            ld = math.cos(r + sy) * b.scaleY
            b.a = la; b.b = lb; b.c = lc; b.d = ld
        elif tm == "noRotationOrReflection":
            s = pa * pa + pc * pc
            if s > 0.0001:
                s = abs(pa * pd - pb * pc) / s
                pb2 = pc * s
                pd2 = pa * s
                prx = math.atan2(pc, pa) * 180.0 / math.pi
            else:
                pb2 = pb; pd2 = pd
                pa = 0.0; pc = 0.0
                prx = 90.0 - math.atan2(pd, pb) * 180.0 / math.pi
            rx = (b.rotation + b.shearX - prx) * D2R
            ry = (b.rotation + b.shearY - prx + 90.0) * D2R
            la = math.cos(rx) * b.scaleX
            lb = math.cos(ry) * b.scaleY
            lc = math.sin(rx) * b.scaleX
            ld = math.sin(ry) * b.scaleY
            b.a = pa * la - pb2 * lc
            b.b = pa * lb - pb2 * ld
            b.c = pc * la + pd2 * lc
            b.d = pc * lb + pd2 * ld
        elif tm in ("noScale", "noScaleOrReflection"):
            cosr = math.cos(r); sinr = math.sin(r)
            za = pa * cosr + pb * sinr
            zc = pc * cosr + pd * sinr
            s = math.sqrt(za * za + zc * zc)
            if s > 0.00001: s = 1.0 / s
            za *= s; zc *= s
            s = math.sqrt(za * za + zc * zc)
            if tm == "noScale" and (pa * pd - pb * pc < 0):
                s = -s
            r2 = math.pi / 2 + math.atan2(zc, za)
            zb = math.cos(r2) * s
            zd = math.sin(r2) * s
            la = math.cos(sx) * b.scaleX
            lb = -math.sin(sy) * b.scaleY
            lc = math.sin(sx) * b.scaleX
            ld = math.cos(sy) * b.scaleY
            b.a = za * la + zb * lc
            b.b = za * lb + zb * ld
            b.c = zc * la + zd * lc
            b.d = zc * lb + zd * ld

def apply_ik_one(sk, ikd):
    """Apply a single 2-bone IK constraint (Spine IkConstraint.Apply)."""
    names = ikd["bones"]
    target = sk.bones[ikd["target"]]
    bend = 1 if ikd.get("bendPositive", True) else -1
    mix = ikd.get("mix", 1.0)
    if mix == 0.0:
        return
    if len(names) == 2:
        parent = sk.bones[names[0]]
        child = sk.bones[names[1]]
    elif len(names) == 1:
        child = sk.bones[names[0]]
        parent = child.parent
        if parent is None: return
    else:
        return
    px, py = parent.ax, parent.ay
    psx, psy = parent.ascaleX, parent.ascaleY
    csx = child.ascaleX
    cx, cy = child.ax, child.ay
    cwx = parent.a * cx + parent.b * cy + parent.worldX
    cwy = parent.c * cx + parent.d * cy + parent.worldY
    pp = parent.parent
    if pp is None:
        dx = cwx - px; dy = cwy - py
        tx = target.worldX - px; ty = target.worldY - py
    else:
        a, b, c, d = pp.a, pp.b, pp.c, pp.d
        idet = 1.0 / (a * d - b * c)
        x = cwx - pp.worldX; y = cwy - pp.worldY
        dx = (x * d - y * b) * idet - px
        dy = (y * a - x * c) * idet - py
        x = target.worldX - pp.worldX; y = target.worldY - pp.worldY
        tx = (x * d - y * b) * idet - px
        ty = (y * a - x * c) * idet - py
    l1 = math.hypot(dx, dy)
    l2 = child.length * csx
    if l1 < 0.0001:
        return
    dd = tx * tx + ty * ty
    l2 *= psx
    cos = (dd - l1 * l1 - l2 * l2) / (2 * l1 * l2)
    cos = max(-1.0, min(1.0, cos))
    a2 = math.acos(cos) * bend
    aa = l1 + l2 * cos
    bb = l2 * math.sin(a2)
    a1 = math.atan2(ty * aa - tx * bb, tx * aa + ty * bb)
    os = math.atan2(cy, cx)
    rot = parent.arotation
    a1d = (a1 - os) * 180.0 / math.pi - rot
    while a1d > 180: a1d -= 360
    while a1d < -180: a1d += 360
    parent.rotation = rot + a1d * mix
    rot = child.arotation
    a2d = ((a2 + os) * 180.0 / math.pi - child.ashearX) - rot
    while a2d > 180: a2d -= 360
    while a2d < -180: a2d += 360
    child.rotation = rot + a2d * mix

def apply_transform(sk, tcd):
    """Apply one Spine transform constraint (world-space default, per Spine JSON)."""
    names = tcd["bones"]
    target = sk.bones[tcd["target"]]
    rmx = tcd.get("rotateMix", 0) or 0
    tmx = tcd.get("translateMix", 0) or 0
    smx = tcd.get("scaleMix", 0) or 0
    shmx = tcd.get("shearMix", 0) or 0
    local = tcd.get("local", False)  # Spine default is WORLD space (local=false)
    relative = tcd.get("relative", False)
    offrot = tcd.get("rotation", 0) or 0
    offx = tcd.get("x", 0) or 0
    offy = tcd.get("y", 0) or 0
    offsx = tcd.get("scaleX", 0) or 0
    offsy = tcd.get("scaleY", 0) or 0
    offshy = tcd.get("shearY", 0) or 0
    for name in names:
        bone = sk.bones.get(name)
        if not bone: continue
        if not local:
            # ===== WORLD-space constraint (Spine TransformConstraint.ApplyAbsoluteWorld) =====
            ta, tb, tc, td = target.a, target.b, target.c, target.d
            if tmx != 0:
                tx = ta * offx + tb * offy + target.worldX
                ty = tc * offx + td * offy + target.worldY
                if relative:
                    nwx = bone.worldX + tx * tmx; nwy = bone.worldY + ty * tmx
                else:
                    nwx = bone.worldX + (tx - bone.worldX) * tmx
                    nwy = bone.worldY + (ty - bone.worldY) * tmx
                # 把新的世界坐标换算回本地坐标，随后 compute_world 会据此重算
                if bone.parent:
                    p = bone.parent
                    det = p.a * p.d - p.b * p.c
                    dx = nwx - p.worldX; dy = nwy - p.worldY
                    bone.x = (dx * p.d - dy * p.b) / det
                    bone.y = (dy * p.a - dx * p.c) / det
                else:
                    bone.x = nwx; bone.y = nwy
            if rmx != 0:
                a, b, c, d = bone.a, bone.b, bone.c, bone.d
                if relative:
                    r = math.atan2(tc, ta) + offrot * D2R
                else:
                    r = math.atan2(tc, ta) - math.atan2(c, a) + offrot * D2R
                while r > math.pi: r -= 2 * math.pi
                while r < -math.pi: r += 2 * math.pi
                r *= rmx
                cr, sr = math.cos(r), math.sin(r)
                bone.a = cr * a - sr * c; bone.b = cr * b - sr * d
                bone.c = sr * a + cr * c; bone.d = sr * b + cr * d
                if bone.parent:
                    pr = math.atan2(bone.parent.c, bone.parent.a)
                    bone.rotation = math.degrees(math.atan2(bone.c, bone.a) - pr)
            if smx > 0:
                s = math.sqrt(bone.a * bone.a + bone.c * bone.c)
                if s != 0:
                    tgt = math.sqrt(ta * ta + tc * tc)
                    if relative:
                        s = ((tgt - 1 + offsx) * smx) + 1
                    else:
                        s = (s + (tgt - s + offsx) * smx) / s
                    bone.a *= s; bone.c *= s
                s = math.sqrt(bone.b * bone.b + bone.d * bone.d)
                if s != 0:
                    tgt = math.sqrt(tb * tb + td * td)
                    if relative:
                        s = ((tgt - 1 + offsy) * smx) + 1
                    else:
                        s = (s + (tgt - s + offsy) * smx) / s
                    bone.b *= s; bone.d *= s
        else:
            # ===== LOCAL-space constraint (Spine TransformConstraint.ApplyAbsoluteLocal) =====
            rotation = bone.arotation
            if rmx != 0:
                if relative:
                    rotation += (target.arotation + offrot) * rmx
                else:
                    r = target.arotation - rotation + offrot
                    r = (r + 180.0) % 360.0 - 180.0
                    rotation += r * rmx
            x = bone.ax; y = bone.ay
            if relative:
                if tmx != 0:
                    x += (target.ax + offx) * tmx
                    y += (target.ay + offy) * tmx
            else:
                if tmx != 0:
                    x += (target.ax - x + offx) * tmx
                    y += (target.ay - y + offy) * tmx
            scaleX = bone.ascaleX; scaleY = bone.ascaleY
            if smx != 0:
                if relative:
                    scaleX *= ((target.ascaleX - 1 + offsx) * smx) + 1
                    scaleY *= ((target.ascaleY - 1 + offsy) * smx) + 1
                else:
                    if scaleX != 0: scaleX = (scaleX + (target.ascaleX - scaleX + offsx) * smx) / scaleX
                    if scaleY != 0: scaleY = (scaleY + (target.ascaleY - scaleY + offsy) * smx) / scaleY
            shearY = bone.ashearY
            if shmx != 0:
                if relative:
                    shearY += (target.ashearY + offshy) * shmx
                else:
                    r = target.ashearY - shearY + offshy
                    r = (r + 180.0) % 360.0 - 180.0
                    shearY += r * shmx
            bone.x = x; bone.y = y; bone.rotation = rotation
            bone.scaleX = scaleX; bone.scaleY = scaleY
            bone.shearY = shearY

def apply_constraints(sk):
    """Apply IK + transform constraints in Spine's update order."""
    items = []
    for ikd in sk.ik:
        items.append((ikd.get("order", 0), "ik", ikd))
    for tcd in sk.transform:
        items.append((tcd.get("order", 0), "transform", tcd))
    items.sort(key=lambda it: it[0])
    compute_world(sk)
    if not items:
        return
    for b in sk.bone_list:
        b.ax = b.x; b.ay = b.y; b.arotation = b.rotation
        b.ascaleX = b.scaleX; b.ascaleY = b.scaleY
        b.ashearX = b.shearX; b.ashearY = b.shearY
    for _, kind, cd in items:
        if kind == "ik":
            apply_ik_one(sk, cd)
        else:
            apply_transform(sk, cd)
        compute_world(sk)

def apply_constraint_timelines(sk, transform_tls, ik_tls, time):
    """把 transform/ik 约束的 mix 值按时间线动画化(覆盖静态默认值)。"""
    for tcd in sk.transform:
        ct = transform_tls.get(tcd.get("name"))
        if not ct: continue
        for typ in ("rotateMix", "translateMix", "scaleMix", "shearMix"):
            keys = ct.get(typ)
            if keys:
                tcd[typ] = sample_timeline(keys, time, tcd.get(typ, 0.0))
    for ikd in sk.ik:
        ct = ik_tls.get(ikd.get("name"))
        if not ct: continue
        if "mix" in ct and ct["mix"]:
            ikd["mix"] = sample_timeline(ct["mix"], time, ikd.get("mix", 0.0))
        if "softness" in ct and ct["softness"]:
            ikd["softness"] = sample_timeline(ct["softness"], time, ikd.get("softness", 0.0))
        if "bendPositive" in ct and ct["bendPositive"]:
            ikd["bendPositive"] = sample_timeline(ct["bendPositive"], time, 1.0) > 0.0

def bezier_y(cx1, cy1, cx2, cy2, x):
    t = x
    for _ in range(24):
        mt = 1.0 - t
        xt = 3 * mt * mt * t * cx1 + 3 * mt * t * t * cx2 + t * t * t
        dxt = 3 * (mt * mt - 2 * mt * t) * cx1 + 3 * (2 * mt * t - t * t) * cx2 + 3 * t * t
        if abs(dxt) < 1e-9: break
        t = max(0.0, min(1.0, t - (xt - x) / dxt))
    mt = 1.0 - t
    return 3 * mt * mt * t * cy1 + 3 * mt * t * t * cy2 + t * t * t

def parse_curve(k):
    if "curve" not in k: return None
    c = k["curve"]
    if c == "stepped": return "stepped"
    if isinstance(c, (int, float)):
        return (float(c), float(k.get("c2", 0.0)), float(k.get("c3", 1.0)), float(k.get("c4", 1.0)))
    return None

def curve_factor(curve, t):
    if curve is None: return t
    if curve == "stepped": return 0.0
    if isinstance(curve, tuple): return bezier_y(curve[0], curve[1], curve[2], curve[3], t)
    return t

def parse_timelines(anim):
    bone_tls = {}
    for bname, tls in anim.get("bones", {}).items():
        bt = {}
        for typ, keys in tls.items():
            parsed = []
            for k in keys:
                t = k.get("time", 0.0)
                if typ == "rotate":
                    # Spine stores rotation keyframes as an OFFSET from setup rotation.
                    val = k.get("angle", 0.0)
                elif typ == "scale":
                    # Scale keyframes are MULTIPLIERS of the setup scale (default 1).
                    val = (k.get("x", 1.0), k.get("y", 1.0))
                else:  # translate / shear are OFFSETS from setup (default 0).
                    val = (k.get("x", 0.0), k.get("y", 0.0))
                parsed.append({"time": t, "value": val, "curve": parse_curve(k)})
            bt[typ] = parsed
        bone_tls[bname] = bt
    slot_tls = {}
    for sname, tls in anim.get("slots", {}).items():
        st = {}
        for typ, keys in tls.items():
            parsed = []
            for k in keys:
                t = k.get("time", 0.0)
                if typ == "attachment": val = k.get("name")
                elif typ == "color": val = hex_to_rgba(k.get("color", "ffffffff"))
                else: val = k.get("value")
                parsed.append({"time": t, "value": val, "curve": parse_curve(k)})
            st[typ] = parsed
        slot_tls[sname] = st
    return bone_tls, slot_tls

def sample_timeline(keys, time, default, is_angle=False):
    if time < keys[0]["time"]:
        return default
    if time <= keys[0]["time"]:
        return keys[0]["value"]
    last = keys[-1]
    if time >= last["time"]:
        return last["value"]
    for i in range(len(keys) - 1):
        k0 = keys[i]; k1 = keys[i + 1]
        if k0["time"] <= time <= k1["time"]:
            span = k1["time"] - k0["time"]
            if span == 0:
                return k1["value"]
            f = curve_factor(k0["curve"], (time - k0["time"]) / span)
            v0 = k0["value"]; v1 = k1["value"]
            if v0 is None or v1 is None or isinstance(v0, str) or isinstance(v1, str):
                return v0
            if is_angle:
                d = (v1 - v0 + 180.0) % 360.0 - 180.0
                return v0 + d * f
            if isinstance(v0, tuple):
                return tuple(v0[j] + (v1[j] - v0[j]) * f for j in range(len(v0)))
            return v0 + (v1 - v0) * f
    return last["value"]

def parse_deform(anim):
    deform = {}
    for skin, slots in anim.get("deform", {}).items():
        for slot_name, attachments in slots.items():
            for att_name, keys in attachments.items():
                parsed = []
                for k in keys:
                    parsed.append({"time": k.get("time", 0.0), "offsets": k.get("vertices"),
                                   "start": k.get("offset", 0), "curve": parse_curve(k)})
                deform[(slot_name, att_name)] = parsed
    return deform

def parse_transform_timelines(anim):
    """解析 transform 约束的时间线(rotateMix/translateMix/scaleMix/shearMix)。"""
    out = {}
    for cname, tls in anim.get("transform", {}).items():
        ct = {}
        for typ, keys in tls.items():
            parsed = []
            for k in keys:
                parsed.append({"time": k.get("time", 0.0), "value": k.get(typ, 0.0), "curve": parse_curve(k)})
            ct[typ] = parsed
        out[cname] = ct
    return out

def parse_ik_timelines(anim):
    """解析 IK 约束的时间线(mix/softness/bendPositive)。"""
    out = {}
    for cname, tls in anim.get("ik", {}).items():
        ct = {}
        for typ, keys in tls.items():
            parsed = []
            for k in keys:
                val = k.get(typ, 0.0)
                if typ == "bendPositive":
                    val = 1.0 if k.get("bendPositive") else -1.0
                parsed.append({"time": k.get("time", 0.0), "value": val, "curve": parse_curve(k)})
            ct[typ] = parsed
        out[cname] = ct
    return out

def sample_deform(keys, time, total_len):
    result = [0.0] * total_len
    if not keys:
        return result
    full = []
    for k in keys:
        arr = [0.0] * total_len
        if k["offsets"] is not None:
            st = k["start"]
            for j, v in enumerate(k["offsets"]):
                if st + j < total_len:
                    arr[st + j] = v
        full.append(arr)
    if time <= keys[0]["time"]:
        return full[0]
    if time >= keys[-1]["time"]:
        return full[-1]
    for i in range(len(keys) - 1):
        k0, k1 = keys[i], keys[i + 1]
        if k0["time"] <= time <= k1["time"]:
            span = k1["time"] - k0["time"]
            f = curve_factor(k0["curve"], (time - k0["time"]) / span) if span > 0 else 0.0
            a, b = full[i], full[i + 1]
            return [a[j] + (b[j] - a[j]) * f for j in range(total_len)]
    return full[-1]

def apply_animation(sk, bone_tls, slot_tls, time):
    for bname, tls in bone_tls.items():
        b = sk.bones.get(bname)
        if not b: continue
        if "rotate" in tls: b.rotation += sample_timeline(tls["rotate"], time, 0.0, True)
        if "translate" in tls:
            dx, dy = sample_timeline(tls["translate"], time, (0.0, 0.0)); b.x += dx; b.y += dy
        if "scale" in tls:
            sx, sy = sample_timeline(tls["scale"], time, (1.0, 1.0)); b.scaleX *= sx; b.scaleY *= sy
        if "shear" in tls:
            dx, dy = sample_timeline(tls["shear"], time, (0.0, 0.0)); b.shearX += dx; b.shearY += dy
    for sname, tls in slot_tls.items():
        s = sk.slots.get(sname)
        if not s: continue
        if "attachment" in tls: s.attachment = sample_timeline(tls["attachment"], time, s.attachment)
        if "color" in tls: s.color = sample_timeline(tls["color"], time, hex_to_rgba(s.color))

def hex_to_rgba(h):
    h = h.lstrip("#")
    if len(h) == 8: r, g, b, a = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
    elif len(h) == 6: r, g, b, a = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255
    else: return (255, 255, 255, 255)
    return (r, g, b, a)

def draw_triangle(frame, tex, p0, p1, p2, uvs, color, blend="normal"):
    x0, y0 = p0; x1, y1 = p1; x2, y2 = p2
    minx = max(0, int(math.floor(min(x0, x1, x2)))); maxx = min(frame.shape[1] - 1, int(math.ceil(max(x0, x1, x2))))
    miny = max(0, int(math.floor(min(y0, y1, y2)))); maxy = min(frame.shape[0] - 1, int(math.ceil(max(y0, y1, y2))))
    if maxx < minx or maxy < miny: return
    det = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    if abs(det) < 1e-6: return
    inv = 1.0 / det
    xx = np.arange(minx, maxx + 1, dtype=np.float64)
    yy = np.arange(miny, maxy + 1, dtype=np.float64)
    X, Y = np.meshgrid(xx, yy)
    w1 = ((X - x0) * (y2 - y0) - (Y - y0) * (x2 - x0)) * inv
    w2 = ((x1 - x0) * (Y - y0) - (y1 - y0) * (X - x0)) * inv
    w0 = 1 - w1 - w2
    inside = (w0 >= -0.001) & (w1 >= -0.001) & (w2 >= -0.001)
    if not inside.any(): return
    (u0, v0), (u1, v1), (u2, v2) = uvs
    U = w0 * u0 + w1 * u1 + w2 * u2
    V = w0 * v0 + w1 * v1 + w2 * v2
    Ui = np.clip(np.floor(U).astype(np.int32), 0, tex.shape[1] - 1)
    Vi = np.clip(np.floor(V).astype(np.int32), 0, tex.shape[0] - 1)
    px = tex[Vi, Ui].astype(np.float32)
    cr, cg, cb, ca = color
    px[:, :, 0] *= cr / 255.0; px[:, :, 1] *= cg / 255.0; px[:, :, 2] *= cb / 255.0; px[:, :, 3] *= ca / 255.0
    sub = frame[miny:maxy + 1, minx:maxx + 1]
    a = px[:, :, 3:4] / 255.0
    srgb = px[:, :, :3]
    drgb = sub[:, :, :3].astype(np.float32)
    da = sub[:, :, 3:4].astype(np.float32)
    if blend == "additive":
        out_rgb = srgb * a + drgb
        out_a = da
    elif blend == "multiply":
        out_rgb = srgb * drgb / 255.0 * a + drgb * (1 - a)
        out_a = px[:, :, 3:4] + da * (1 - a)
    elif blend == "screen":
        out_rgb = (255.0 - (255.0 - srgb) * (255.0 - drgb) / 255.0) * a + drgb * (1 - a)
        out_a = px[:, :, 3:4] + da * (1 - a)
    else:  # normal
        out_rgb = srgb * a + drgb * (1 - a)
        out_a = px[:, :, 3:4] + da * (1 - a)
    out = np.concatenate([out_rgb, out_a], axis=2)
    sub[inside] = np.clip(out[inside], 0, 255).astype(np.uint8)

def render_frame(sk, atlas_img, regions, W, H, scale, ox, oy, time=0.0, deform_tls=None):
    frame = np.zeros((H, W, 4), dtype=np.uint8)
    tex = np.array(atlas_img.convert("RGBA"))
    def to_canvas(wx, wy):
        return (wx * scale + ox, oy - wy * scale)
    for s in sk.slot_list:
        if not s.attachment: continue
        b = sk.bones.get(s.bone)
        if not b: continue
        att = sk.skin_attachments.get(s.name, {}).get(s.attachment)
        if att is None: continue
        color = s.color if isinstance(s.color, tuple) else hex_to_rgba(s.color)
        if "uvs" in att:
            region = regions.get(att.get("path")) or regions.get(att.get("name")) or regions.get(s.attachment)
            if region is None: continue
            uvs = att["uvs"]; tris = att["triangles"]; verts = att["vertices"]
            vertex_count = len(uvs) // 2
            world_pts = []
            if len(verts) == 2 * vertex_count:
                def_off = sample_deform(deform_tls.get((s.name, s.attachment), []), time, len(verts)) if deform_tls else None
                for i in range(vertex_count):
                    lx = verts[2 * i]; ly = verts[2 * i + 1]
                    if def_off is not None:
                        lx += def_off[2 * i]; ly += def_off[2 * i + 1]
                    world_pts.append((b.a * lx + b.b * ly + b.worldX, b.c * lx + b.d * ly + b.worldY))
            else:
                nbe = 0
                vi = 0
                while vi < len(verts):
                    nbe += int(verts[vi]); vi += 1 + int(verts[vi]) * 4
                def_off = sample_deform(deform_tls.get((s.name, s.attachment), []), time, nbe * 2) if deform_tls else None
                vi = 0
                bi = 0
                while vi < len(verts):
                    bone_count = int(verts[vi]); vi += 1
                    wx = 0.0; wy = 0.0
                    for _ in range(bone_count):
                        bone_index = int(verts[vi]); vi += 1
                        lx = verts[vi]; ly = verts[vi + 1]; vi += 2
                        weight = verts[vi]; vi += 1
                        if def_off is not None:
                            lx += def_off[bi * 2]; ly += def_off[bi * 2 + 1]
                        bi += 1
                        bb = sk.bone_list[bone_index]
                        wx += weight * (bb.a * lx + bb.b * ly + bb.worldX)
                        wy += weight * (bb.c * lx + bb.d * ly + bb.worldY)
                    world_pts.append((wx, wy))
            for ti in range(0, len(tris), 3):
                i0, i1, i2 = tris[ti], tris[ti + 1], tris[ti + 2]
                pc0 = to_canvas(*world_pts[i0]); pc1 = to_canvas(*world_pts[i1]); pc2 = to_canvas(*world_pts[i2])
                u0 = uv_to_atlas(region, uvs[2 * i0], uvs[2 * i0 + 1])
                u1 = uv_to_atlas(region, uvs[2 * i1], uvs[2 * i1 + 1])
                u2 = uv_to_atlas(region, uvs[2 * i2], uvs[2 * i2 + 1])
                draw_triangle(frame, tex, pc0, pc1, pc2, (u0, u1, u2), color, s.blend)
        else:
            region = regions.get(att.get("path")) or regions.get(att.get("name")) or regions.get(s.attachment)
            if region is None: continue
            w = att.get("width", region.ow); h = att.get("height", region.oh)
            oxx = att.get("x", 0); oyy = att.get("y", 0)
            scx = att.get("scaleX", 1); scy = att.get("scaleY", 1)
            rot = att.get("rotation", 0) * D2R
            # 与 Spine RegionAttachment.updateOffset 一致：含 offset/orig!=size 的修正
            rsx = (w / region.ow) * scx if region.ow else scx
            rsy = (h / region.oh) * scy if region.oh else scy
            lx = -w / 2 * scx + region.ox * rsx
            ly = -h / 2 * scy + region.oy * rsy
            lx2 = lx + region.w * rsx
            ly2 = ly + region.h * rsy
            # 角点顺序 [左下, 右下, 右上, 左上]
            corners = [(lx, ly), (lx2, ly), (lx2, ly2), (lx, ly2)]
            cr = math.cos(rot); sr = math.sin(rot)
            corners = [(x * cr - y * sr + oxx, x * sr + y * cr + oyy) for (x, y) in corners]
            wp = [(b.a * x + b.b * y + b.worldX, b.c * x + b.d * y + b.worldY) for (x, y) in corners]
            pc = [to_canvas(x, y) for (x, y) in wp]
            # 角点顺序 [左下, 右下, 右上, 左上]，UV 用 RegionAttachment.setRegion 的直接区域坐标(rotation 通过角点排列体现)
            uv = region_uvs(region)
            draw_triangle(frame, tex, pc[0], pc[1], pc[2], (uv[0], uv[1], uv[2]), color, s.blend)
            draw_triangle(frame, tex, pc[0], pc[2], pc[3], (uv[0], uv[2], uv[3]), color, s.blend)
    return frame

def render_spine(json_path, atlas_path, png_path, out_gif, fps=30, scale=3.0):
    """渲染一个 Spine 动画到 GIF。"""
    j = json.load(open(json_path, encoding="utf-8"))
    atlas_text = open(atlas_path, encoding="utf-8").read()
    atlas_img = Image.open(png_path).convert("RGBA")
    regions = parse_atlas(atlas_text)
    sk = Skeleton(j)
    anim = sk.animations.get("animation") or next(iter(sk.animations.values()))
    bone_tls, slot_tls = parse_timelines(anim)
    deform_tls = parse_deform(anim)
    transform_tls = parse_transform_timelines(anim)
    ik_tls = parse_ik_timelines(anim)
    duration = anim.get("duration")
    if not duration:
        # JSON 可能缺 duration，从所有时间线的最大关键帧时间推算
        duration = 0.0
        for bt in bone_tls.values():
            for keys in bt.values():
                if keys: duration = max(duration, keys[-1]["time"])
        for st in slot_tls.values():
            for keys in st.values():
                if keys: duration = max(duration, keys[-1]["time"])
        duration = max(duration, 0.1)
    nframes = max(2, int(round(duration * fps)))

    xs = []; ys = []
    for ti in range(min(nframes, 60)):
        t = ti / fps
        sk.reset(); apply_animation(sk, bone_tls, slot_tls, t); apply_constraint_timelines(sk, transform_tls, ik_tls, t); apply_constraints(sk)
        for s in sk.slot_list:
            if not s.attachment: continue
            b = sk.bones.get(s.bone)
            if not b: continue
            att = sk.skin_attachments.get(s.name, {}).get(s.attachment)
            if att is None: continue
            if "uvs" in att:
                verts = att["vertices"]; vc = len(att["uvs"]) // 2
                if len(verts) == 2 * vc:
                    def_off = sample_deform(deform_tls.get((s.name, s.attachment), []), t, len(verts))
                    for i in range(vc):
                        lx = verts[2 * i] + def_off[2 * i]; ly = verts[2 * i + 1] + def_off[2 * i + 1]
                        xs.append(b.a * lx + b.b * ly + b.worldX); ys.append(b.c * lx + b.d * ly + b.worldY)
                else:
                    nbe = 0; vi = 0
                    while vi < len(verts):
                        nbe += int(verts[vi]); vi += 1 + int(verts[vi]) * 4
                    def_off = sample_deform(deform_tls.get((s.name, s.attachment), []), t, nbe * 2)
                    vi = 0; bi = 0
                    while vi < len(verts):
                        n = int(verts[vi]); vi += 1
                        wx = 0.0; wy = 0.0
                        for _ in range(n):
                            bii = int(verts[vi]); lx = verts[vi + 1] + def_off[bi * 2]; ly = verts[vi + 2] + def_off[bi * 2 + 1]; w = verts[vi + 3]; vi += 4; bi += 1
                            bb = sk.bone_list[bii]
                            wx += w * (bb.a * lx + bb.b * ly + bb.worldX)
                            wy += w * (bb.c * lx + bb.d * ly + bb.worldY)
                        xs.append(wx); ys.append(wy)
            else:
                region = regions.get(att.get("path")) or regions.get(att.get("name")) or regions.get(s.attachment)
                if region is None: continue
                w = att.get("width", region.ow); h = att.get("height", region.oh)
                oxx = att.get("x", 0); oyy = att.get("y", 0)
                scx = att.get("scaleX", 1); scy = att.get("scaleY", 1)
                rot = att.get("rotation", 0) * D2R
                hw = w / 2 * scx; hh = h / 2 * scy
                cr = math.cos(rot); sr = math.sin(rot)
                for cx, cy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
                    px = cx * cr - cy * sr + oxx; py = cx * sr + cy * cr + oyy
                    xs.append(b.a * px + b.b * py + b.worldX); ys.append(b.c * px + b.d * py + b.worldY)
    mx = max(xs) - min(xs); my = max(ys) - min(ys)
    padx = max(8, mx * 0.07); pady = max(8, my * 0.07)
    W = int(mx * scale + 2 * padx); H = int(my * scale + 2 * pady)
    ox = -min(xs) * scale + padx; oy = max(ys) * scale + pady

    frames = []
    for ti in range(nframes):
        t = ti / fps
        sk.reset(); apply_animation(sk, bone_tls, slot_tls, t); apply_constraint_timelines(sk, transform_tls, ik_tls, t); apply_constraints(sk)
        frames.append(Image.fromarray(render_frame(sk, atlas_img, regions, W, H, scale, ox, oy, t, deform_tls), "RGBA"))
    frames[0].save(out_gif, save_all=True, append_images=frames[1:], duration=int(1000 / fps), loop=0, disposal=2)
    return W, H, nframes

# ====================================================================
# 第四部分：命令行
# ====================================================================

def cmd_extract(args):
    paths = []
    if os.path.isdir(args.input):
        paths = sorted(glob.glob(os.path.join(args.input, "*.assetbundle")))
    else:
        paths = [args.input]
    os.makedirs(args.out, exist_ok=True)
    total = 0
    for p in paths:
        if not is_khfs(p): continue
        try:
            ab_name, count = extract_bundle(p, args.out)
            total += 1
            if count["Texture2D"] or count["Sprite"] or count["TextAsset"]:
                print("提取 %s -> %s (贴图%d 精灵%d 文本%d)" % (os.path.basename(p), ab_name, count["Texture2D"], count["Sprite"], count["TextAsset"]))
        except Exception as e:
            print("跳过 %s: %s" % (os.path.basename(p), str(e)[:60]))
    print("完成，共处理 %d 个 bundle，输出到 %s" % (total, args.out))

def cmd_emoji(args):
    os.makedirs(args.out, exist_ok=True)
    paths = sorted(glob.glob(os.path.join(args.input, "*.assetbundle")))
    from UnityPy import Environment
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.files.SerializedFile import SerializedFile
    for p in paths:
        if not is_khfs(p): continue
        try:
            decoded, _ = decode_bundle(p)
            sf = SerializedFile(EndianBinaryReader(decoded), parent=Environment())
            ab = sf.assetbundle.m_Name if sf.assetbundle else ""
            if "emojispine" not in ab and "bq_" not in ab:
                continue
            # 提取这个表情的 skeleton/atlas/texture
            files = {}
            for pid, o in sf.objects.items():
                tn = o.type.name
                try: obj = o.read()
                except Exception: continue
                nm = (getattr(obj, "m_Name", "") or str(pid)).replace("/", "_")
                if tn == "TextAsset":
                    d = obj.m_Script
                    if isinstance(d, str): d = d.encode("utf-8")
                    d = bytes(d)
                    files["json" if d.lstrip()[:1] == b"{" else "atlas"] = d
                elif tn == "Texture2D":
                    from UnityPy.export.Texture2DConverter import parse_image_data
                    file_size = struct.unpack_from(">Q", decoded, 0x18)[0]
                    sd = getattr(obj, "m_StreamData", None)
                    if sd is not None and getattr(sd, "size", 0) > 0:
                        res = decoded[file_size + sd.offset : file_size + sd.offset + sd.size]
                        img = parse_image_data(res, obj.m_Width, obj.m_Height, obj.m_TextureFormat, (2021, 3, 16), 13, None, flip=True)
                    else:
                        img = obj.image
                    files["png"] = img
            if "json" in files and "atlas" in files and "png" in files:
                base = os.path.join(args.out, ab.replace(".assetbundle", ""))
                jp = base + ".json"; ap = base + ".atlas"; pp = base + ".png"; gp = base + ".gif"
                open(jp, "wb").write(files["json"]); open(ap, "wb").write(files["atlas"]); files["png"].save(pp)
                W, H, n = render_spine(jp, ap, pp, gp)
                print("表情 %s -> %s (%dx%d, %d帧)" % (ab, gp, W, H, n))
        except Exception as e:
            print("跳过 %s: %s" % (os.path.basename(p), str(e)[:60]))
    print("完成，输出到 %s" % args.out)

def cmd_render(args):
    render_spine(args.json, args.atlas, args.png, args.out if args.out else "out.gif")
    print("已渲染:", args.out if args.out else "out.gif")

def main():
    ap = argparse.ArgumentParser(description="火影忍者手游资源解包器")
    sub = ap.add_subparsers(dest="cmd")

    s1 = sub.add_parser("extract", help="解包提取任意 bundle 的资源")
    s1.add_argument("input", help="bundle 文件或目录")
    s1.add_argument("-o", "--out", default="extracted", help="输出目录")
    s1.set_defaults(func=cmd_extract)

    s2 = sub.add_parser("emoji", help="提取所有表情并渲染 GIF")
    s2.add_argument("input", help="bundle 目录")
    s2.add_argument("-o", "--out", default="表情GIF", help="输出目录")
    s2.set_defaults(func=cmd_emoji)

    s3 = sub.add_parser("render", help="渲染单个 Spine 动画")
    s3.add_argument("json"); s3.add_argument("atlas"); s3.add_argument("png")
    s3.add_argument("-o", "--out", default="out.gif")
    s3.set_defaults(func=cmd_render)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    args.func(args)

if __name__ == "__main__":
    main()
