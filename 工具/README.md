# 工具目录

## AssetStudio-KiHan-mod-v0.16.zip（火影魔改版）

> **下载**：见本仓库 [Releases](https://github.com/ChenNan06/naruto-emoji-extractor/releases)
> 里的 **`AssetStudio-KiHan-mod-v0.16.zip`**（约 10 MB）。二进制不发在 git 里，避免 clone 变慢。
> 下好后解压到本目录即可。

**能打开火影 `UnityKH*FS` 包的 AssetStudio**（AssetStudio v0.16.0.0 + 可插拔解密层）。

双击解压后运行 `AssetStudioGUI.exe` 即可。它比官方版多的**只有容器解密**：

```
AssetStudio.dll 里新增：
  ProcessBundle / CanBeDecrypted / CreateDecryptor / DecryptData / DecryptVersion
  SingleByteXorLoader / XorStream / _xorKey
  <FairGuard2Loader>…__CustomRc4 / <FairguardLoader>…__CustomCrc32
```

也就是「认出魔数 → 解密 BlocksInfo 目录表 → 还原成标准 UnityFS → 交给原版逻辑」。

### 它**不能**做的事

- **不会**解析 NGUI 图集（DLL 里 `mSprites` / `UISpriteData` / `pixelSize` 全 0 命中），
  只能把 `EmojiAtlas_1` 整张 2048×2048 导成一张 PNG。
- **不会**切 sprite、**不会**渲染 Spine。这两件事是 `脚本/表情提取器.py` 干的。

### 什么时候用它

- 想肉眼翻 bundle 里有什么资产、导出单张贴图/模型；
- 排查某个资源到底在哪个包里（File → Load File，一次只喂一个 `*.assetbundle`，
  **不要整目录加载**，会卡死）。

### 注意

这个版本需要 .NET 6+ 运行时。如果双击闪退，装一下
[.NET Desktop Runtime 6/8](https://dotnet.microsoft.com/download/dotnet)，或直接跑 `AssetStudioGUI.exe` 看控制台报错。

---

> 如果你只是想把表情导出来，**不需要这个工具**，直接：
>
> ```bash
> python 脚本/00_从APK解包.py <base.apk> -o apk_bundles
> python 脚本/表情提取器.py extract apk_bundles -o 表情输出
> ```
>
> 纯 Python，一条命令出 11 张静态 PNG + 17 个帧动画 GIF + 64 个 Spine GIF。
