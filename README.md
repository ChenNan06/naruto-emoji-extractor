# 火影忍者手游 · 表情提取器

一键提取火影忍者手游（腾讯 KiHan）里的聊天表情，分三类导出：

| 类型 | 说明 | 输出 |
|------|------|------|
| 静态表情 | NGUI 图集 `EmojiAtlas_1` 里的 sprite（对战期间镜头表情） | PNG |
| 帧动画表情 | 同一张图集里的多帧序列（少女雏田/天天/井野/樱 等） | GIF |
| 动态 Spine 表情 | 骨骼动画（`Common/EmojiSpines/*`） | 透明底 GIF |

## 功能特点

- 自动定位 EmojiConfig（表情总表）/ EmojiAtlas（图集）/ Spine bundle——它们的数字 ID 每次版本更新都会变，无需手动指定。
- 完整处理 Spine 3.8 渲染的全部坑：图集 rotation 转正、region/mesh 两套 UV、offset/orig 白边修正、加权网格 + deform 形变、颜色/混合时间线、二进制 `.skel` 解析等。
- 内置特例修复：垂钓表情鱼线过长自动裁剪、驰骋表情 path≠name 采错贴图等。
- 提供图形界面版（`表情提取器GUI.py`）与命令行版（`表情提取器.py`）。

## 环境要求

- Python 3.8+
- 依赖：`pip install pillow numpy unitypy`

## 快速开始

命令行：

```bash
# 一键提取全部表情（静态 + 帧动画 + 动态 Spine）
python 脚本/表情提取器.py extract <bundle目录> -o <输出目录>

# 只提取静态/帧动画（跳过慢的动态部分）
python 脚本/表情提取器.py extract <bundle目录> -o <输出目录> --no-spine

# 只解析并打印表情总表
python 脚本/表情提取器.py list <bundle目录>
```

图形界面：双击 `一键提取表情.bat`（或直接运行 `脚本/表情提取器GUI.py`），选择 bundle 目录与输出目录后点「开始提取」。

## 版本更新后复用

游戏发新版后，把新 APK 解包得到新的 `assets/bundle` 目录（或下载目录 `com.tencent.KiHan/files/999/bundle`），直接重跑上面的命令即可，无需改代码——程序会自动重新定位资源。

## 目录结构

```
脚本/
  表情提取器.py       # 命令行核心
  表情提取器GUI.py    # 图形界面
  火影解包器.py       # KHFS 解包 + Spine 渲染引擎
  _scan_spine.py      # bundle 快速扫描
  _spine_bin.py       # 二进制 .skel → JSON
  _整理成品.py        # 成品重命名/整理
资料文档/
  表情提取器使用说明.md   # 用户手册
  表情总表.md            # 表情总表与命名映射
  火影手游资源解包.md    # 资源解包方法
  解包结果说明.md        # 解包结果说明
必读-工作区规范.md       # 三类表情真面目 + 四大坑
一键提取表情.bat         # 图形界面启动入口
```

## 说明

- 本仓库仅包含提取工具代码与文档，**不包含任何游戏资源文件**（bundle、图集、成品表情均不入库）。
- 仅供学习研究使用。
