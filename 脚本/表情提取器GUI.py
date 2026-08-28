# -*- coding: utf-8 -*-
"""
火影忍者手游 表情提取器 - 图形界面版
====================================
点开即用。选择 bundle 目录和输出目录, 点「开始提取」即可一键导出全部表情。

依赖: pip install pillow numpy unitypy
"""

import os, sys, threading, queue, importlib.util, argparse, types

if getattr(sys, "frozen", False):
    # PyInstaller 打包后, 数据文件解包到 sys._MEIPASS
    SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


def _load_ex():
    spec = importlib.util.spec_from_file_location("ex", os.path.join(SCRIPT_DIR, "表情提取器.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EX = _load_ex()


class _LogStream:
    """把 print 输出重定向到队列, 供 GUI 轮询显示。"""
    def __init__(self, q):
        self.q = q
        self._buf = ""
    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(line + "\n")
    def flush(self):
        if self._buf.strip():
            self.q.put(self._buf + "\n")
            self._buf = ""


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.done = True
        root.title("火影忍者手游 表情提取器")
        root.geometry("720x520")
        root.minsize(640, 440)

        pad = {"padx": 10, "pady": 6}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)

        # bundle 目录
        ttk.Label(top, text="bundle 目录:").grid(row=0, column=0, sticky="w")
        self.bundle_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.bundle_var, width=60).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="浏览…", command=self.pick_bundle).grid(row=0, column=2)

        # 输出目录
        ttk.Label(top, text="输出目录:").grid(row=1, column=0, sticky="w")
        self.out_var = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "表情导出"))
        ttk.Entry(top, textvariable=self.out_var, width=60).grid(row=1, column=1, sticky="we", padx=6)
        ttk.Button(top, text="浏览…", command=self.pick_out).grid(row=1, column=2)

        # 选项
        self.no_spine_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="跳过动态 Spine 表情(只做静态+帧动画, 快很多)",
                        variable=self.no_spine_var).grid(row=2, column=0, columnspan=3, sticky="w")

        top.columnconfigure(1, weight=1)

        # 按钮
        mid = ttk.Frame(root)
        mid.pack(fill="x", **pad)
        self.start_btn = ttk.Button(mid, text="开始提取", command=self.start)
        self.start_btn.pack(side="left")
        ttk.Button(mid, text="清空日志", command=self.clear_log).pack(side="left", padx=8)

        # 日志
        self.log_box = scrolledtext.ScrolledText(root, height=20, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log("欢迎使用表情提取器。")
        self.log("选择 bundle 目录(如 火影忍者_1.78.78.8/assets/bundle)后点「开始提取」。")
        self.log("首次扫描约需 1~2 分钟, 请耐心等待。\n")

    def pick_bundle(self):
        d = filedialog.askdirectory(title="选择 bundle 目录(含 *.assetbundle)")
        if d:
            self.bundle_var.set(d)

    def pick_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    def log(self, s):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", s)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        self.root.update_idletasks()

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def start(self):
        bundle = self.bundle_var.get().strip().strip('"')
        out = self.out_var.get().strip().strip('"')
        if not bundle or not os.path.isdir(bundle):
            messagebox.showerror("错误", "请选择有效的 bundle 目录")
            return
        if not out:
            messagebox.showerror("错误", "请填写输出目录")
            return
        if not self.done:
            messagebox.showinfo("提示", "正在提取中, 请稍候…")
            return
        self.done = False
        self.start_btn.configure(state="disabled")
        args = types.SimpleNamespace(bundle=bundle, out=out, no_spine=self.no_spine_var.get())
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, args):
        old = sys.stdout
        sys.stdout = _LogStream(self.q)
        try:
            EX.cmd_extract(args)
            self.q.put("== 全部完成 ==\n")
        except Exception as e:
            self.q.put("发生错误: %s\n" % e)
        finally:
            sys.stdout = old
            self.q.put(None)

    def _poll(self):
        try:
            while True:
                s = self.q.get_nowait()
                if s is None:
                    self.done = True
                    self.start_btn.configure(state="normal")
                    self.log("已完成。输出目录: %s\n" % self.out_var.get())
                    return
                self.log(s)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
