import os, shutil, glob, re

# 动态表情 资产名 -> 中文名 完整映射(56 配置内 + 11 额外)
SPINE_CN = {
    "bq_mbps": "破防了", "bq_bq": "爆发", "bq_9wei_yiwen": "九尾疑问", "bq_yy": "应援",
    "bq_9wei_xxyan": "九尾星星眼", "bq_9wei_zhayan": "九尾眨眼", "bq_bowenlu_2025": "哈哈笑",
    "bq_xutuo": "虚脱", "bq_le": "乐", "bq_shen_peien_tiandao": "神(佩恩天道)",
    "bq_nayouruhe": "那又如何", "bq_zaizhan": "再战一次", "bq_dq": "捶你", "bq_shenian_2025": "乙巳(蛇年)",
    "bq_xinsui": "心碎", "bq_dianzan": "赞喔", "bq_wudi_wulian_ok": "败给你了", "bq_jiuzhe": "就这",
    "bq_zhi_feiji": "纸飞机", "bq_9wei_jingxia": "九尾惊讶", "bq_wfzs": "无法直视(旧)",
    "bq_jy": "惊叹", "bq_hanliujiabei": "汗流浃背", "bq_sao_bao_xianhua": "献花",
    "bq_weiyan_touying": "威严投影", "bq_9wei_shengqi": "九尾生气", "bq_haqi": "哈气",
    "bq_si_ha": "嘶哈", "bq_zundujiadu": "尊嘟假嘟", "bq_chaofeng": "起舞", "bq_kuku_you": "落泪",
    "bq_jiaogeinile": "交给你了", "bq_9wei_beishang": "九尾悲伤", "bq_songhua": "送你花花",
    "bq_100fen": "满分", "bq_shanfeng": "扇风", "bq_cdz": "吹笛子", "bq_jushihuo": "聚是一团火",
    "bq_xiongdi_rangwoshang": "兄弟让我上", "bq_juqi": "舞龙", "bq_yuzhibofantan": "反弹",
    "bq_2026_baibingbang": "吃冰棒", "bq_2026_diaoyu": "垂钓", "bq_2026_niguolaiya": "放马过来",
    "bq_2026_qianshoufeijian": "漂泊扉间", "bq_chi_yuan_xiao_2026": "吃元宵",
    "bq_manian_qipaoren": "套马杆(驰骋)", "bq_ming_shi_chu_gao_zhao": "名师出高招",
    "bq_mouhua": "思考", "bq_sabi": "派福利", "bq_touxi": "恶作剧", "bq_xia_ri": "喝冷饮",
    "bq_xiaobenben": "记本本", "bq_xiaying_zuozhu": "侠隐佐助", "bq_xinchun_asima": "神驹阿斯玛",
    "bq_xinchun_xiaonan": "神驹小南", "bq_yaoqi": "摇旗", "bq_zaibuzhan": "漂泊再不斩",
    # 额外(下载/旧版)
    "download_bq_cuo": "搓", "download_bq_jinbi": "金币", "download_bq_ku": "哭",
    "download_bq_nu": "怒", "download_bq_shengluhao": "省略号", "download_bq_xiao": "笑",
    "download_bq_xin": "心", "download_bq_zan": "赞", "emoji_jingyin": "静音",
}

SRC = r"E:\naruto\输出结果\表情"
DST = r"E:\naruto\表情成品"
os.makedirs(DST, exist_ok=True)
copied = []

def safe(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s)

# 1. 静态表情(PNG)
for f in sorted(glob.glob(os.path.join(SRC, "静态表情", "*.png"))):
    b = os.path.basename(f)
    if "EmojiAtlas" in b or "_预览" in b:
        continue
    cn = b.rsplit("_", 1)[0].replace("_", "")  # 崇拜_chongbai.png -> 崇拜
    out = safe("静态_%s.png" % cn)
    shutil.copy2(f, os.path.join(DST, out))
    copied.append(out)

# 2. 帧动画(GIF)
for f in sorted(glob.glob(os.path.join(SRC, "帧动画表情", "*.gif"))):
    b = os.path.basename(f)
    cn = b.rsplit("_", 1)[0].replace("_", "")  # 少女井野_jy.gif -> 少女井野; 捶你_旧版_dq -> 捶你旧版
    out = safe("动态_%s.gif" % cn)
    shutil.copy2(f, os.path.join(DST, out))
    copied.append(out)

# 3. 动态 Spine(GIF)
for f in sorted(glob.glob(os.path.join(SRC, "动态表情", "*.gif"))):
    b = os.path.basename(f)[:-4]  # 去掉 .gif
    asset = b.split("__", 1)[-1]  # 476728200__bq_jushihuo -> bq_jushihuo
    cn = SPINE_CN.get(asset, asset)
    out = safe("动态_%s.gif" % cn)
    dst_path = os.path.join(DST, out)
    # 重名冲突(如 无法直视 帧动画 vs 旧 spine)加序号
    n = 2
    while os.path.exists(dst_path):
        out = safe("动态_%s_%d.gif" % (cn, n))
        dst_path = os.path.join(DST, out)
        n += 1
    shutil.copy2(f, dst_path)
    copied.append(out)

# 去重统计(重名已处理, 这里统计最终文件数)
final = sorted(set(os.listdir(DST)))
lines = ["表情成品清单 (%d 个文件)" % len(final), ""]
# 分类统计
static = [x for x in final if x.startswith("静态_")]
dyn = [x for x in final if x.startswith("动态_")]
lines.append("静态 %d 个:" % len(static))
lines.extend("  " + x for x in static)
lines.append("")
lines.append("动态 %d 个:" % len(dyn))
lines.extend("  " + x for x in dyn)
open(os.path.join(DST, "_清单.txt"), "w", encoding="utf-8").write("\n".join(lines))
print("copied", len(copied), "files, 静态=%d 动态=%d" % (len(static), len(dyn)))