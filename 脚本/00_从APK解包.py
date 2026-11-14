#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""00_从APK解包.py —— 把 base.apk 里的 assets/bundle 解出来

为什么需要这一步：
  表情的图集(EmojiAtlas_1)和 Spine 骨架(common_emojispines_*) **只在安装包内**，
  下载目录 files/999/bundle 里没有。只扫下载目录会得出「图集不存在」的错误结论。

用法:
  python 00_从APK解包.py <base.apk> [-o 输出目录] [--filter 关键字]

示例:
  python 00_从APK解包.py "E:/naruto/火影忍者.apk" -o "E:/naruto/apk_bundles"
  python 00_从APK解包.py base.apk --filter emoji      # 只解名字里带 emoji 的包
"""
import argparse
import os
import sys
import zipfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('apk')
    ap.add_argument('-o', '--out', default=None, help='输出目录，默认 <apk目录>/apk_bundles')
    ap.add_argument('--filter', default='', help='只解文件名含该关键字的包')
    a = ap.parse_args()

    if not os.path.isfile(a.apk):
        print('APK 不存在:', a.apk)
        sys.exit(1)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.apk)), 'apk_bundles')
    os.makedirs(out, exist_ok=True)

    z = zipfile.ZipFile(a.apk)
    names = [i for i in z.infolist() if i.filename.startswith('assets/bundle/') and not i.is_dir()]
    if a.filter:
        names = [i for i in names if a.filter.lower() in i.filename.lower()]
    print('APK 内 assets/bundle 条目: %d' % len(names))

    n = total = 0
    for info in names:
        fn = os.path.basename(info.filename)
        if not fn:
            continue
        with z.open(info) as f, open(os.path.join(out, fn), 'wb') as o:
            d = f.read()
            o.write(d)
            total += len(d)
        n += 1
        if n % 1000 == 0:
            print('  解出 %d ...' % n)

    # 顺手把 config/bundle.xml 也拿出来（未加密版，能查资源路径映射）
    for info in z.infolist():
        if info.filename == 'assets/config/bundle.xml':
            with z.open(info) as f, open(os.path.join(out, '..', 'bundle.xml'), 'wb') as o:
                o.write(f.read())
            print('  另存 config/bundle.xml')
            break

    print('完成: %d 个 bundle / %.1f MB -> %s' % (n, total / 1048576, out))


if __name__ == '__main__':
    main()
