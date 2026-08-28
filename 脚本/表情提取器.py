#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火影忍者手游 表情提取器  —  面向人类用户的完整工具
=====================================================

把游戏资源里的三类表情全部导出:
  1. 静态表情   (NGUI 图集 EmojiAtlas 里的 sprite)
  2. 帧动画表情 (同一张图集里的多帧序列, 合成 GIF)
  3. 动态 Spine 表情 (骨骼动画, 渲染成 GIF)

用法:
  python 表情提取器.py extract <bundle目录> [-o 输出目录]     # 一键提取全部表情
  python 表情提取器.py list    <bundle目录>                    # 只解析并打印表情总表
  python 表情提取器.py render  <json> <atlas> <png> [-o 输出]  # 渲染单个 Spine 表情

示例:
  python 表情提取器.py extract "E:/naruto/火影忍者_1.78.78.8/assets/bundle" -o "E:/naruto/表情导出"

说明:
  - bundle 目录里是 *.assetbundle 文件(数字 ID 命名)。
  - 程序会自动扫描定位 EmojiConfig(表情总表)和 EmojiAtlas(图集), 无需手动指定 ID。
  - 首次扫描 1.4 万个 bundle 约需 1~2 分钟; 只做静态/帧动画可加 --no-spine 跳过慢的部分。

依赖: pip install pillow numpy unitypy
"""

import argparse, os, sys, glob, struct, json, re, importlib.util
from collections import defaultdict

if getattr(sys, "frozen", False):
    SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from PIL import Image


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPT_DIR, fn))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


unpack = _load("unpack", "火影解包器.py")
scanmod = _load("scanmod", "_scan_spine.py")
skelbin = _load("skelbin", "_spine_bin.py")
fast_decode_bundle = scanmod.fast_decode_bundle
render_spine = unpack.render_spine


# ==================================================================
# 1. EmojiConfig protobuf 解析
# ==================================================================
def read_varint(buf, i):
    shift = 0
    val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return val, i


def parse_msg(buf, i, end):
    fields = {}
    while i < end:
        tag, i = read_varint(buf, i)
        if tag == 0:
            break
        fnum = tag >> 3
        wt = tag & 7
        if wt == 0:
            v, i = read_varint(buf, i)
            fields[fnum] = v
        elif wt == 2:
            ln, i = read_varint(buf, i)
            fields[fnum] = buf[i:i + ln]
            i += ln
        elif wt == 5:
            fields[fnum] = buf[i:i + 4]
            i += 4
        elif wt == 1:
            fields[fnum] = buf[i:i + 8]
            i += 8
        else:
            break
    return fields, i


def _as_bytes(d):
    if isinstance(d, str):
        # UnityPy 把二进制 TextAsset 按 utf-8+surrogateescape 解成 str, 必须同样方式还原字节
        return d.encode("utf-8", errors="surrogateescape")
    return bytes(d)


def parse_emojiconfig(data):
    """解析 EmojiConfig protobuf, 返回条目列表 [{id,name,type,kind,path,sprite,frames}]。"""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        tag, i = read_varint(data, i)
        if tag == 0:
            break
        fnum = tag >> 3
        wt = tag & 7
        if fnum == 1 and wt == 2:
            ln, i = read_varint(data, i)
            sub, _ = parse_msg(data, i, i + ln)
            i += ln
            entries.append(sub)
        elif wt == 0:
            _, i = read_varint(data, i)
        elif wt == 2:
            ln, i = read_varint(data, i)
            i += ln
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break

    out = []
    for e in entries:
        eid = e.get(1, 0)
        name = e.get(2, b"").decode("utf-8", "replace")
        typ = e.get(3, 0)
        pathraw = e.get(6, b"")
        sprite = e.get(7, b"").decode("utf-8", "replace")
        frames = e.get(8, 0)
        if b"EmojiSpines" in pathraw:
            kind = "spine"
            path = pathraw.decode("latin-1").replace("Common/EmojiSpines/", "")
        elif b"EmojiAtlas" in pathraw:
            # 静态表情 sprite 名是 icon-biaoqing-xxx-100+100; 帧动画 sprite 是短前缀(如 jy_/tt_/ct_/xy_)
            kind = "static" if sprite.startswith("icon-") else ("frame" if sprite else "static")
            path = pathraw.decode("latin-1")
        else:
            kind = "?"
            path = pathraw.decode("latin-1")
        out.append({"id": eid, "name": name, "type": typ, "kind": kind,
                    "path": path, "sprite": sprite, "frames": frames})
    return out


# ==================================================================
# 2. 图集 sprite 扫描 (含 rotation 字段)
# ==================================================================
def scan_atlas_sprites(decoded):
    """从 EmojiAtlas bundle 扫描全部 sprite, 返回 [{name,x,y,w,h,rot}]。"""
    sprites = []
    i = 0
    n = len(decoded)
    while i < n - 4:
        ln = struct.unpack_from("<i", decoded, i)[0]
        if 2 <= ln <= 60:
            s = decoded[i + 4:i + 4 + ln]
            if all(32 <= b < 127 for b in s):
                p = i + 4 + ln
                p = (p + 3) & ~3
                if p + 20 <= n:
                    x, y, w, h, rot = struct.unpack_from("<5i", decoded, p)
                    if 0 <= x < 4096 and 0 <= y < 4096 and 0 < w <= 512 and 0 < h <= 512 and 0 <= rot <= 3:
                        sprites.append({"name": s.decode(), "x": x, "y": y, "w": w, "h": h, "rot": rot})
        i += 4
    return sprites


# ==================================================================
# 3. 静态/帧动画切图
# ==================================================================
def crop_sprite(atlas, s):
    x, y, w, h, rot = s["x"], s["y"], s["w"], s["h"], s["rot"]
    if rot == 1:
        # rotation=1 的 sprite 在贴图里横着放, 实际占 h×w 区域, 要交换 w/h 再转正
        sub = atlas.crop((x, y, x + h, y + w)).rotate(-90, expand=True)
    elif rot == 2:
        sub = atlas.crop((x, y, x + w, y + h)).rotate(180, expand=True)
    elif rot == 3:
        sub = atlas.crop((x, y, x + h, y + w)).rotate(90, expand=True)
    else:
        sub = atlas.crop((x, y, x + w, y + h))
    return sub


def alpha_bbox(sub, thresh=16):
    a = sub.getchannel("A").load()
    w, h = sub.size
    minx = miny = 10 ** 9
    maxx = maxy = -1
    for y in range(h):
        for x in range(w):
            if a[x, y] > thresh:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return None
    return (minx, miny, maxx + 1, maxy + 1)


def trim_transparent(sub, margin=2, thresh=16):
    bb = alpha_bbox(sub, thresh)
    if not bb:
        return sub
    l, t, r, b = bb
    l = max(0, l - margin)
    t = max(0, t - margin)
    r = min(sub.width, r + margin)
    b = min(sub.height, b + margin)
    return sub.crop((l, t, r, b))


# ==================================================================
# 4. bundle 扫描定位
# ==================================================================
def scan_bundles(bundle_dir, progress=True):
    """扫描所有 bundle, 返回 {config_bundle, atlas_bundle, spine_bundles}。"""
    files = sorted(glob.glob(os.path.join(bundle_dir, "*.assetbundle")))
    config_bundle = None
    atlas_bundle = None
    spine_bundles = []
    total = len(files)
    for idx, p in enumerate(files):
        if progress and (idx % 2000 == 0):
            print("  扫描 %d/%d ..." % (idx, total))
        try:
            dec = fast_decode_bundle(p)
            if not dec:
                continue
            if config_bundle is None and b"EmojiConfig" in dec:
                config_bundle = p
            # 图集定位: 名字 emojiatlas 会被大量引用/依赖 bundle 命中, 必须用真实 sprite 列表验证
            if atlas_bundle is None and b"emojiatlas" in dec:
                for sp in scan_atlas_sprites(dec):
                    if sp["name"] == "icon-biaoqing-chongbai-100+100":
                        atlas_bundle = p
                        break
            if b"emojispines" in dec:
                spine_bundles.append(p)
        except Exception:
            pass
    return {"config": config_bundle, "atlas": atlas_bundle, "spines": spine_bundles}


def crop_fishing_line(gif_path):
    """垂钓表情专用：裁掉下方过长的鱼线(细线)，保留角色+鱼竿。"""
    import numpy as np
    im = Image.open(gif_path)
    n = getattr(im, "n_frames", 1)
    duration = im.info.get("duration", 33)
    frames = []
    for i in range(n):
        im.seek(i)
        frames.append(np.array(im.convert("RGBA")))
    F = np.array(frames)
    union = (F[..., 3] > 8).any(axis=0)
    H = union.shape[0]
    ys, xs = np.where(union)
    if len(xs) == 0:
        return
    row_w = []
    for y in range(H):
        xr = np.where(union[y])[0]
        row_w.append(int(xr.max()) - int(xr.min()) + 1 if len(xr) else 0)
    mx = max(row_w)
    thr = max(10, mx * 0.2)
    cutoff = H
    for y in range(H - 1, -1, -1):
        if row_w[y] >= thr:
            cutoff = y + 1
            break
    cutoff = min(H, cutoff + 2)
    if cutoff >= H:
        return
    cropped = [Image.fromarray(f[:cutoff]) for f in frames]
    cropped[0].save(gif_path, save_all=True, append_images=cropped[1:],
                    duration=duration, loop=0, disposal=2)


# ==================================================================
# 5. 动态 Spine 提取 + 渲染
# ==================================================================
def extract_spine_bundle(path, out_dir, name_map):
    """从单个 spine bundle 提取 json/atlas/png 并渲染 GIF。返回输出 gif 路径或 None。"""
    from UnityPy import Environment
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.files.SerializedFile import SerializedFile
    from UnityPy.export.Texture2DConverter import parse_image_data

    decoded, _ = unpack.decode_bundle(path)
    file_size = struct.unpack_from(">Q", decoded, 0x18)[0]
    sf = SerializedFile(EndianBinaryReader(decoded), parent=Environment())
    ab = sf.assetbundle.m_Name if sf.assetbundle else ""
    files = {}
    asset_name = ""
    for pid, o in sf.objects.items():
        tn = o.type.name
        try:
            obj = o.read()
        except Exception:
            continue
        nm = (getattr(obj, "m_Name", "") or str(pid)).replace("/", "_")
        if tn == "TextAsset":
            d = obj.m_Script
            if isinstance(d, str):
                d = d.encode("utf-8", errors="surrogateescape")
            d = bytes(d)
            if d.lstrip()[:1] == b"{":
                files["json"] = d
            elif b"format:" in d and b"size:" in d:
                files["atlas"] = d
                if not asset_name:
                    asset_name = nm
            else:
                files["skel"] = d
        elif tn == "Texture2D":
            sd = getattr(obj, "m_StreamData", None)
            if sd is not None and getattr(sd, "size", 0) > 0:
                res = decoded[file_size + sd.offset: file_size + sd.offset + sd.size]
                img = parse_image_data(res, obj.m_Width, obj.m_Height, obj.m_TextureFormat,
                                       (2021, 3, 16), 13, None, flip=True)
            else:
                img = obj.image
            files["png"] = img
            if not asset_name:
                asset_name = nm

    # 二进制 .skel 转 JSON
    if "json" not in files and "skel" in files:
        try:
            files["json"] = json.dumps(skelbin.parse_skel(files["skel"]), ensure_ascii=False).encode("utf-8")
        except Exception as e:
            print("  .skel 解析失败 %s: %s" % (os.path.basename(path), str(e)[:60]))
            return None
    if "json" not in files or "atlas" not in files or "png" not in files:
        return None

    # 用 EmojiConfig 的路径(bundle 名后缀)映射中文名; 取不到就用资产名
    suffix = ab.replace("common_emojispines_", "").replace(".assetbundle", "")
    cn = name_map.get(suffix, "")
    label = (cn + "__" + asset_name) if cn else asset_name
    safe = re.sub(r'[\\/:*?"<>|]', "_", label)
    base = os.path.join(out_dir, safe)
    jp, ap, pp, gp = base + ".json", base + ".atlas", base + ".png", base + ".gif"
    open(jp, "wb").write(files["json"])
    open(ap, "wb").write(files["atlas"])
    files["png"].save(pp)
    try:
        render_spine(jp, ap, pp, gp)
        # 垂钓表情专用：鱼线太长，裁掉下方细线
        if "diaoyu" in (asset_name or "").lower() or "chuidiao" in (asset_name or "").lower():
            try:
                crop_fishing_line(gp)
            except Exception as e:
                print("  裁剪鱼线失败 %s: %s" % (label, str(e)[:60]))
    except Exception as e:
        print("  渲染失败 %s: %s" % (label, str(e)[:60]))
        return None
    return gp


# ==================================================================
# CLI 命令
# ==================================================================
def cmd_extract(args):
    bundle_dir = args.bundle
    out_dir = args.out
    if not os.path.isdir(bundle_dir):
        print("错误: bundle 目录不存在:", bundle_dir)
        sys.exit(1)
    os.makedirs(out_dir, exist_ok=True)

    print("== 第 1 步: 扫描 bundle 定位关键资源 ==")
    found = scan_bundles(bundle_dir)
    if found["config"] is None:
        print("警告: 未找到 EmojiConfig(表情总表)。动态/静态表情的名称映射会缺失。")
    if found["atlas"] is None:
        print("警告: 未找到 EmojiAtlas_1(图集)。静态表情和帧动画将跳过。")
    print("  配置 bundle: %s" % (os.path.basename(found["config"]) if found["config"] else "未找到"))
    print("  图集 bundle: %s" % (os.path.basename(found["atlas"]) if found["atlas"] else "未找到"))
    print("  Spine bundle: %d 个" % len(found["spines"]))

    # 解析 EmojiConfig
    name_map = {}   # spine 路径后缀 -> 中文名
    config_entries = []
    if found["config"]:
        dec, _ = unpack.decode_bundle(found["config"])
        # 找 TextAsset EmojiConfig
        from UnityPy import Environment
        from UnityPy.streams import EndianBinaryReader
        from UnityPy.files.SerializedFile import SerializedFile
        sf = SerializedFile(EndianBinaryReader(dec), parent=Environment())
        for o in sf.objects.values():
            if o.type.name == "TextAsset":
                try:
                    obj = o.read()
                    if getattr(obj, "m_Name", "") == "EmojiConfig":
                        config_entries = parse_emojiconfig(_as_bytes(obj.m_Script))
                        break
                except Exception:
                    continue
        for e in config_entries:
            if e["kind"] == "spine":
                name_map[e["path"]] = e["name"]

    # 静态 + 帧动画
    if found["atlas"]:
        print("== 第 2 步: 提取静态表情 + 帧动画 ==")
        dec, _ = unpack.decode_bundle(found["atlas"])
        sprites = scan_atlas_sprites(dec)
        print("  图集里共 %d 个 sprite" % len(sprites))
        # 找 Texture2D EmojiAtlas_1 并导出
        from UnityPy import Environment
        from UnityPy.streams import EndianBinaryReader
        from UnityPy.files.SerializedFile import SerializedFile
        from UnityPy.export.Texture2DConverter import parse_image_data
        sf = SerializedFile(EndianBinaryReader(dec), parent=Environment())
        file_size = struct.unpack_from(">Q", dec, 0x18)[0]
        atlas_img = None
        for o in sf.objects.values():
            if o.type.name == "Texture2D":
                try:
                    obj = o.read()
                    if getattr(obj, "m_Name", "") == "EmojiAtlas_1":
                        sd = getattr(obj, "m_StreamData", None)
                        if sd is not None and getattr(sd, "size", 0) > 0:
                            res = dec[file_size + sd.offset: file_size + sd.offset + sd.size]
                            atlas_img = parse_image_data(res, obj.m_Width, obj.m_Height,
                                                         obj.m_TextureFormat, (2021, 3, 16), 13, None, flip=True)
                        else:
                            atlas_img = obj.image
                        break
                except Exception:
                    continue
        if atlas_img is None:
            print("  错误: 图集纹理导出失败")
        else:
            static_dir = os.path.join(out_dir, "静态表情")
            frame_dir = os.path.join(out_dir, "帧动画")
            os.makedirs(static_dir, exist_ok=True)
            os.makedirs(frame_dir, exist_ok=True)

            # 静态: 配置里引用到的 icon-biaoqing sprite(去重, 第一次出现的名字优先;
            # 战绩展示/申请出战 会复用 fennu sprite, 不能被它们覆盖掉「愤怒」)
            static_cn = {}
            static_sprite_names = []
            for e in config_entries:
                if e["kind"] == "static" and e["sprite"] and e["sprite"] not in static_cn:
                    static_cn[e["sprite"]] = e["name"]
                    static_sprite_names.append(e["sprite"])
            for nm in static_sprite_names:
                s = next((x for x in sprites if x["name"] == nm), None)
                if not s:
                    continue
                im = trim_transparent(crop_sprite(atlas_img, s))
                pinyin = nm.replace("icon-biaoqing-", "").replace("-100+100", "")
                label = static_cn.get(nm, pinyin)
                im.save(os.path.join(static_dir, "%s_%s.png" % (label, pinyin)))
            print("  静态表情: %d 个 -> %s" % (len(static_sprite_names), static_dir))

            # 帧动画: 前缀_0001.. 分组
            frame_cn = {e["sprite"]: e["name"] for e in config_entries
                        if e["kind"] == "frame" and e["sprite"]}
            prefixes = sorted(set(frame_cn.keys()))
            count = 0
            for pref in prefixes:
                # pref 形如 "jy_"(带下划线), 帧名形如 "jy_0001"(前缀+4位数字)
                frames = sorted([s for s in sprites
                                 if s["name"].startswith(pref)
                                 and len(s["name"]) == len(pref) + 4
                                 and s["name"][len(pref):].isdigit()],
                                key=lambda s: int(s["name"][len(pref):]))
                if not frames:
                    continue
                imgs = [crop_sprite(atlas_img, s) for s in frames]
                maxw = max(im.width for im in imgs)
                maxh = max(im.height for im in imgs)
                gif_frames = []
                for im in imgs:
                    canvas = Image.new("RGBA", (maxw, maxh), (0, 0, 0, 0))
                    canvas.paste(im, (0, 0), im)
                    gif_frames.append(canvas)
                label = frame_cn[pref]
                gp = os.path.join(frame_dir, "%s_%s.gif" % (label, pref.rstrip("_")))
                gif_frames[0].save(gp, save_all=True, append_images=gif_frames[1:],
                                   duration=100, loop=0, disposal=2)
                count += 1
            print("  帧动画: %d 组 -> %s" % (count, frame_dir))

    # Spine
    if not args.no_spine and found["spines"]:
        print("== 第 3 步: 提取并渲染动态 Spine 表情 ==")
        spine_dir = os.path.join(out_dir, "动态表情")
        os.makedirs(spine_dir, exist_ok=True)
        ok = 0
        for i, p in enumerate(found["spines"]):
            if (i % 10 == 0):
                print("  处理 %d/%d ..." % (i, len(found["spines"])))
            try:
                gp = extract_spine_bundle(p, spine_dir, name_map)
                if gp:
                    ok += 1
            except Exception as e:
                print("  跳过 %s: %s" % (os.path.basename(p), str(e)[:60]))
        print("  动态表情: %d 个 -> %s" % (ok, spine_dir))

    # 表情总表
    if config_entries:
        table_path = os.path.join(out_dir, "表情总表.txt")
        lines = ["id\t类型\t中文名\t路径/素材"]
        kind_cn = {"static": "静态", "frame": "帧动画", "spine": "动态"}
        for e in config_entries:
            lines.append("%s\t%s\t%s\t%s" % (e["id"], kind_cn.get(e["kind"], "?"),
                                             e["name"], e["path"] + (":" + e["sprite"] if e["sprite"] else "")))
        open(table_path, "w", encoding="utf-8").write("\n".join(lines))
        print("表情总表: %s" % table_path)

    print("== 完成! 输出目录: %s ==" % out_dir)


def cmd_list(args):
    bundle_dir = args.bundle
    if not os.path.isdir(bundle_dir):
        print("错误: bundle 目录不存在:", bundle_dir)
        sys.exit(1)
    found = scan_bundles(bundle_dir)
    if found["config"] is None:
        print("未找到 EmojiConfig。")
        return
    dec, _ = unpack.decode_bundle(found["config"])
    from UnityPy import Environment
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.files.SerializedFile import SerializedFile
    sf = SerializedFile(EndianBinaryReader(dec), parent=Environment())
    entries = []
    for o in sf.objects.values():
        if o.type.name == "TextAsset":
            try:
                obj = o.read()
                if getattr(obj, "m_Name", "") == "EmojiConfig":
                    entries = parse_emojiconfig(bytes(obj.m_Script))
                    break
            except Exception:
                continue
    kind_cn = {"static": "静态", "frame": "帧动画", "spine": "动态"}
    for e in entries:
        print("%-4s %-4s %-10s %s" % (e["id"], kind_cn.get(e["kind"], "?"),
                                      e["name"], e["path"] + (":" + e["sprite"] if e["sprite"] else "")))


def cmd_render(args):
    out = args.out if args.out else os.path.splitext(args.json)[0] + ".gif"
    render_spine(args.json, args.atlas, args.png, out)
    print("已渲染:", out)


def main():
    ap = argparse.ArgumentParser(
        description="火影忍者手游 表情提取器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    s1 = sub.add_parser("extract", help="一键提取全部表情(静态+帧动画+动态)")
    s1.add_argument("bundle", help="bundle 目录(含 *.assetbundle)")
    s1.add_argument("-o", "--out", default="表情导出", help="输出目录(默认 表情导出)")
    s1.add_argument("--no-spine", action="store_true", help="跳过动态 Spine(只做静态+帧动画, 快很多)")
    s1.set_defaults(func=cmd_extract)

    s2 = sub.add_parser("list", help="只解析并打印表情总表")
    s2.add_argument("bundle", help="bundle 目录")
    s2.set_defaults(func=cmd_list)

    s3 = sub.add_parser("render", help="渲染单个 Spine 表情")
    s3.add_argument("json")
    s3.add_argument("atlas")
    s3.add_argument("png")
    s3.add_argument("-o", "--out", default=None, help="输出 gif 路径")
    s3.set_defaults(func=cmd_render)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
