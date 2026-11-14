# 火影忍者手游 · 表情提取器

一键提取火影忍者手游（腾讯 KiHan）里的聊天表情，三类全部导出：

| 类型 | 说明 | 输出 | 本次实测数量 |
|------|------|------|------|
| 静态表情 | NGUI 图集 `EmojiAtlas_1` 里的 sprite | PNG | **11 张** |
| 帧动画表情 | 同一张图集里的多帧序列（少女雏田/天天/井野/樱 等） | GIF | **17 个** |
| 动态 Spine 表情 | 骨骼动画（`Common/EmojiSpines/*`） | 透明底 GIF | **64 个** |

合计 **92 个**成品。

> **一句话流程**：APK 解包 → 拿 `assets/bundle` → 跑 `表情提取器.py` → 92 个表情。

## 功能特点

- 自动定位 EmojiConfig（表情总表）/ EmojiAtlas（图集）/ Spine bundle——它们的数字 ID 每次版本更新都会变，无需手动指定。
- 完整处理 Spine 3.8 渲染的全部坑：图集 rotation 转正、region/mesh 两套 UV、offset/orig 白边修正、加权网格 + deform 形变、颜色/混合时间线、二进制 `.skel` 解析等。
- 内置特例修复：垂钓表情鱼线过长自动裁剪、驰骋表情 path≠name 采错贴图等。
- 提供图形界面版（`表情提取器GUI.py`）与命令行版（`表情提取器.py`）。
- 附带**能打开火影包的魔改版 AssetStudio**（`工具/`），方便肉眼翻包。

## 环境要求

- Python 3.8+
- 依赖：`pip install pillow numpy unitypy`

## 快速开始

### 0. 先把素材拿到手（重要）

表情的图集和 Spine 骨架**只在安装包里**，下载目录里没有。两个位置是互补的：

| 位置 | 内容 | 规模 |
|---|---|---|
| `base.apk` → `assets/bundle/` | **图集、Spine 骨架、Lua、大部分 UI** | 本次 8,444 个包 / 1.09 GB |
| `files/999/bundle/` | 热更新下载的资源 | 本次 63,157 个包 |

```bash
# 从 APK 解出 assets/bundle（纯 Python，不需要任何工具）
python 脚本/00_从APK解包.py <base.apk> -o apk_bundles
```

想从 MuMu 模拟器直接把 `files/999/bundle` 拿下来（不用手动复制 20GB）：

```bash
# 离线直读 VDI：不开模拟器、不占 CPU，19.4GB 约 93 秒
python 脚本/01_提取MuMu磁盘文件.py extract \
    --path /sdcard/Android/data/com.tencent.KiHan/files \
    --dest D:\kihan\files --incremental
```

### 1. 提取表情

```bash
# 一键提取全部三类
python 脚本/表情提取器.py extract apk_bundles -o 表情输出

# 只提取静态/帧动画（跳过慢的 Spine 部分）
python 脚本/表情提取器.py extract apk_bundles -o 表情输出 --no-spine

# 只解析并打印表情总表
python 脚本/表情提取器.py list apk_bundles
```

图形界面：双击 `一键提取表情.bat`（或运行 `脚本/表情提取器GUI.py`）。

实测输出：

```
配置 bundle: 3572711878.assetbundle
图集 bundle: 4182536819.assetbundle
Spine bundle: 64 个
图集里共 445 个 sprite
静态表情: 11 个    帧动画: 17 组    动态表情: 64 个
```

## 版本更新后复用

游戏发新版后，用新 APK 重跑 `00_从APK解包.py` + `表情提取器.py` 即可，无需改代码——程序会自动重新定位资源。

## 目录结构

```
脚本/
  00_从APK解包.py     # ★ APK → assets/bundle（表情素材在这里）
  01_提取MuMu磁盘文件.py # 离线直读 MuMu 的 data.vdi，不用开模拟器
  表情提取器.py       # 命令行核心（一键提取三类表情）
  表情提取器GUI.py    # 图形界面
  火影解包器.py       # KHFS 解包 + Spine 3.8 渲染引擎
  _scan_spine.py      # bundle 快速扫描
  _spine_bin.py       # 二进制 .skel → JSON
  _整理成品.py        # 成品重命名/整理
工具/
  AssetStudio-火影魔改版.zip  # 能打开 UnityKH*FS 包的 AssetStudio（容器解密用）
资料文档/
  常见问题.md            # ★ 为什么普通 AssetStudio 打不开 / 怎么拿 MuMu 文件 / 方法通用性
  表情提取器使用说明.md   # 用户手册
  表情总表.md            # 表情总表与命名映射
  火影手游资源解包.md    # KHFS 格式逆向笔记
  解包结果说明.md        # 解包结果说明
必读-工作区规范.md       # 三类表情真面目 + 四大坑
一键提取表情.bat         # 图形界面启动入口
```

## 踩过的坑（务必看 `资料文档/常见问题.md`）

1. **表情素材在 APK 里，不在下载目录**——只扫下载目录会得出「图集不存在」的错误结论。
2. **图集 sprite 有 rotation 字段**（rect 后第 1 个 int，1=顺时针 90° 存放）。必须
   `crop(x, y, x+h, y+w).rotate(-90)`，否则「方向错 + 像被裁一半 + 裁进隔壁表情」。
3. **帧动画必须用完整 rect**，不能按连通域裁，否则帧间抖。
4. **Spine region / mesh 两套 UV 不能混**，混了就是「多火焰 / 糊成一团」。
5. **把 PyInstaller 解包目录塞进 `PYTHONPATH` 复用 UnityPy 时**，要把目录里自带的
   `numpy` / `astc_encoder` / `archspec` / `texture2ddecoder` 改名禁用，否则会盖住
   site-packages 里的正常版本，报 `numpy.core._multiarray_umath` / `EOFError`。

## 说明

- 本仓库仅包含提取工具代码、文档与容器解密工具，**不包含任何游戏资源文件**（bundle、图集、成品表情均不入库）。
- 仅供学习研究使用。
