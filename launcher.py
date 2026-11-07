# -*- coding: utf-8 -*-
"""
Minecraft 启动器 —— 图形界面
依赖：mc_core.py（纯标准库）
运行：python launcher.py
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk

import mc_core as core

APP_TITLE = "MC 启动器"
DEFAULT_VERSION = "1.12.1"


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config()
        self.q = queue.Queue()
        self.manifest = None
        self.versions = []            # 清单条目
        self.busy = False
        self.game = None              # GameProcess
        self.auth_flow = None         # 微软登录线程
        self.cur_java_info = ("", "")  # (path, major)
        self.install_prog = None
        self.skin_server = None       # SkinServer
        self.skin_img = None          # 皮肤预览 PhotoImage 引用

        root.title(APP_TITLE)
        root.geometry("980x680")
        root.minsize(860, 560)
        self._build_style()
        self._build_ui()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_skin_server()
        self._refresh_versions(background=True)
        self._refresh_account_label()
        self._refresh_java_label()
        self._preview_skin()

    def _start_skin_server(self):
        try:
            skins_dir = os.path.join(self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR, "skins")
            self.skin_server = core.SkinServer(self.cfg.get("accounts", []), skins_dir, log=self.log)
            port = self.skin_server.start()
            self.log(f"皮肤服务已启动（端口 {port}）")
        except Exception as e:
            self.log(f"[警告] 皮肤服务启动失败：{e}")

    # ---------------- UI 构建 ----------------
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            try:
                style.theme_use("clam")
            except Exception:
                pass
        style.configure("Big.TLabel", font=("Segoe UI", 28, "bold"))

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.tab_launch = ttk.Frame(nb)
        self.tab_account = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        nb.add(self.tab_launch, text=" 启动 ")
        nb.add(self.tab_account, text=" 账号 ")
        nb.add(self.tab_settings, text=" 设置 ")
        self._build_launch_tab()
        self._build_account_tab()
        self._build_settings_tab()
        # 底部状态栏
        bar = ttk.Frame(self.root, relief="sunken", padding=(6, 3))
        bar.pack(fill="x", side="bottom")
        self.lbl_account = ttk.Label(bar, text="账号：未设置")
        self.lbl_account.pack(side="left", padx=(0, 18))
        self.lbl_java = ttk.Label(bar, text="Java：未检测")
        self.lbl_java.pack(side="left", padx=(0, 18))
        self.lbl_gamedir = ttk.Label(bar, text="游戏目录：…")
        self.lbl_gamedir.pack(side="left")

    def _build_launch_tab(self):
        paned = ttk.PanedWindow(self.tab_launch, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        top = ttk.Frame(left)
        top.pack(fill="x")
        self.var_filter = tk.StringVar(value="全部")
        ttk.Combobox(top, textvariable=self.var_filter, state="readonly", width=8,
                     values=["全部", "正式版", "快照"]).pack(side="left")
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *a: self._apply_filter())
        ttk.Entry(top, textvariable=self.var_search).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="刷新列表", command=lambda: self._refresh_versions(force=True)).pack(side="left")
        self.var_filter.trace_add("write", lambda *a: self._apply_filter())

        cols = ("version", "type", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("version", text="版本")
        self.tree.heading("type", text="类型")
        self.tree.heading("status", text="状态")
        self.tree.column("version", width=130, anchor="w")
        self.tree.column("type", width=60, anchor="center")
        self.tree.column("status", width=70, anchor="center")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, pady=4)
        sb.pack(side="right", fill="y", pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_version)

        right = ttk.Frame(paned)
        paned.add(right, weight=4)
        info = ttk.LabelFrame(right, text="版本信息")
        info.pack(fill="x")
        self.var_info = tk.StringVar(value="请选择版本")
        ttk.Label(info, textvariable=self.var_info, justify="left", padding=8,
                  wraplength=480).pack(fill="x")
        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=6)
        self.btn_install = ttk.Button(btns, text="安装", command=self._install_selected)
        self.btn_install.pack(side="left", padx=(0, 6))
        self.btn_launch = ttk.Button(btns, text="启动", command=self._launch_selected)
        self.btn_launch.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text="停止游戏", command=self._stop_game, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_open_dir = ttk.Button(btns, text="打开游戏目录", command=self._open_game_dir)
        self.btn_open_dir.pack(side="left", padx=(0, 6))
        self.btn_terracotta = ttk.Button(btns, text="陶瓦联机", command=self._start_terracotta)
        self.btn_terracotta.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="删除版本", command=self._delete_selected).pack(side="right")

        self.progress = ttk.Progressbar(right, maximum=1000)
        self.progress.pack(fill="x", pady=(0, 2))
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(right, textvariable=self.var_status, anchor="w").pack(fill="x")
        logf = ttk.LabelFrame(right, text="日志")
        logf.pack(fill="both", expand=True)
        self.log_box = scrolledtext.ScrolledText(logf, height=10, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Button(logf, text="清空日志", command=self._clear_log).pack(anchor="e", padx=4, pady=(0, 4))

    def _build_account_tab(self):
        f = self.tab_account
        frame = ttk.LabelFrame(f, text="离线模式（无需正版，可玩单人）", padding=10)
        frame.pack(fill="x", padx=12, pady=10)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="昵称：").pack(side="left")
        self.var_offline_name = tk.StringVar(value="Steve")
        ttk.Entry(row, textvariable=self.var_offline_name, width=20).pack(side="left", padx=4)
        ttk.Button(row, text="使用此昵称登录", command=self._use_offline).pack(side="left")

        skin_row = ttk.Frame(frame)
        skin_row.pack(fill="x", pady=(8, 0))
        ttk.Label(skin_row, text="皮肤：").pack(side="left")
        self.skin_preview = ttk.Label(skin_row, text="（未设置，显示默认 Steve）")
        self.skin_preview.pack(side="left", padx=6)
        ttk.Button(skin_row, text="选择皮肤图片", command=self._pick_skin).pack(side="left")
        ttk.Button(skin_row, text="移除皮肤", command=self._remove_skin).pack(side="left", padx=4)
        ttk.Label(frame, text="皮肤只对离线账号生效（PNG 图片，64x64 标准格式，可到 namemc 或皮肤站下载）。"
                               "正版账号使用 Mojang 官方皮肤。", foreground="#555", padding=(0, 4)).pack(anchor="w")

        frame2 = ttk.LabelFrame(f, text="微软正版账号", padding=10)
        frame2.pack(fill="x", padx=12, pady=10)
        ttk.Button(frame2, text="微软账号登录（浏览器验证码）", command=self._ms_login).pack(anchor="w")
        self.var_acc_info = tk.StringVar(value="当前无正版账号")
        ttk.Label(frame2, textvariable=self.var_acc_info, padding=(0, 6)).pack(anchor="w")
        ttk.Button(frame2, text="注销账号", command=self._logout).pack(anchor="w")

        note = ttk.Label(f, text="说明：离线模式使用本地昵称生成的 UUID，可正常游玩单人世界；"
                                "正版账号可进入服务器、多人游戏并同步皮肤。", wraplength=700, justify="left")
        note.pack(fill="x", padx=12, pady=4)

    def _build_settings_tab(self):
        f = self.tab_settings
        pad = {"padx": 12, "pady": 6}

        def row(parent, text, widget):
            r = ttk.Frame(parent)
            r.pack(fill="x", **pad)
            ttk.Label(r, text=text, width=16, anchor="e").pack(side="left", padx=(0, 6))
            widget.pack(side="left", fill="x", expand=True)
            return r

        g1 = ttk.LabelFrame(f, text="游戏")
        g1.pack(fill="x", padx=10, pady=(10, 4))
        r = ttk.Frame(g1); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="游戏目录", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_gamedir = tk.StringVar(value=self.cfg.get("game_dir", core.DEFAULT_GAME_DIR))
        ttk.Entry(r, textvariable=self.var_gamedir).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="浏览", command=self._browse_gamedir).pack(side="left", padx=4)
        r = ttk.Frame(g1); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="内存上限(MB)", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_mem_max = tk.IntVar(value=int(self.cfg.get("mem_max", 4096)))
        ttk.Spinbox(r, from_=512, to=65536, increment=512, textvariable=self.var_mem_max, width=10).pack(side="left")
        ttk.Label(r, text="  内存下限(MB)").pack(side="left")
        self.var_mem_min = tk.IntVar(value=int(self.cfg.get("mem_min", 1024)))
        ttk.Spinbox(r, from_=256, to=65536, increment=256, textvariable=self.var_mem_min, width=10).pack(side="left", padx=4)
        r = ttk.Frame(g1); r.pack(fill="x", padx=12, pady=6)
        self.var_close = tk.BooleanVar(value=bool(self.cfg.get("close_after_launch", False)))
        ttk.Checkbutton(r, text="游戏退出后自动关闭启动器", variable=self.var_close).pack(side="left")

        g2 = ttk.LabelFrame(f, text="Java")
        g2.pack(fill="x", padx=10, pady=4)
        r = ttk.Frame(g2); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="选择方式", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_java_mode = tk.StringVar(value=self.cfg.get("java_mode", "auto"))
        ttk.Combobox(r, textvariable=self.var_java_mode, state="readonly", width=14,
                     values=["auto", "manual"]).pack(side="left")
        ttk.Button(r, text="自动检测", command=self._detect_java).pack(side="left", padx=6)
        self.lbl_java_detect = ttk.Label(r, text="")
        self.lbl_java_detect.pack(side="left")
        r = ttk.Frame(g2); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="手动 Java 路径", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_java_path = tk.StringVar(value=self.cfg.get("java_path", ""))
        ttk.Entry(r, textvariable=self.var_java_path).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="浏览", command=self._browse_java).pack(side="left", padx=4)
        r = ttk.Frame(g2); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="下载 Java 运行时", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_jre_major = tk.StringVar(value="8")
        ttk.Combobox(r, textvariable=self.var_jre_major, state="readonly", width=6,
                     values=["8", "17", "21", "25"]).pack(side="left")
        ttk.Label(r, text="  (1.12.1 及旧版用 8；1.18+ 用 17；1.20.5+ 用 21；26.x 用 25)").pack(side="left", padx=4)
        ttk.Button(r, text="开始下载", command=self._download_jre).pack(side="left", padx=6)

        g3 = ttk.LabelFrame(f, text="下载与高级")
        g3.pack(fill="x", padx=10, pady=4)
        r = ttk.Frame(g3); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="下载源", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_source = tk.StringVar(value=self.cfg.get("download_source", "auto"))
        ttk.Combobox(r, textvariable=self.var_source, state="readonly", width=16,
                     values=["auto", "official", "mirror"]).pack(side="left")
        ttk.Label(r, text="  (auto=官方优先、失败自动切镜像)").pack(side="left", padx=4)
        r = ttk.Frame(g3); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="下载线程数", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_threads = tk.IntVar(value=int(self.cfg.get("max_threads", 24)))
        ttk.Spinbox(r, from_=1, to=64, textvariable=self.var_threads, width=8).pack(side="left")
        r = ttk.Frame(g3); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="自定义分辨率", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_custom_res = tk.BooleanVar(value=bool(self.cfg.get("custom_resolution", False)))
        ttk.Checkbutton(r, variable=self.var_custom_res).pack(side="left")
        self.var_res_w = tk.IntVar(value=int(self.cfg.get("res_w", 854)))
        self.var_res_h = tk.IntVar(value=int(self.cfg.get("res_h", 480)))
        ttk.Spinbox(r, from_=320, to=7680, textvariable=self.var_res_w, width=7).pack(side="left", padx=4)
        ttk.Label(r, text="x").pack(side="left")
        ttk.Spinbox(r, from_=240, to=4320, textvariable=self.var_res_h, width=7).pack(side="left", padx=4)
        r = ttk.Frame(g3); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="额外 JVM 参数", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_jvm_extra = tk.StringVar(value=self.cfg.get("jvm_extra", ""))
        ttk.Entry(r, textvariable=self.var_jvm_extra).pack(side="left", fill="x", expand=True)

        g4 = ttk.LabelFrame(f, text="语言与联机")
        g4.pack(fill="x", padx=10, pady=4)
        r = ttk.Frame(g4); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="游戏语言", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_game_lang = tk.StringVar(value=self.cfg.get("game_language", "zh_cn"))
        lang_box = ttk.Combobox(r, textvariable=self.var_game_lang, state="readonly", width=12,
                                values=["zh_cn", "en_us"])
        lang_box.pack(side="left")
        ttk.Label(r, text="  (启动后游戏内显示对应语言)").pack(side="left", padx=4)
        r = ttk.Frame(g4); r.pack(fill="x", padx=12, pady=6)
        ttk.Label(r, text="联机服务器", width=16, anchor="e").pack(side="left", padx=(0, 6))
        self.var_server = tk.StringVar(value=self.cfg.get("server", ""))
        ttk.Entry(r, textvariable=self.var_server).pack(side="left", fill="x", expand=True)
        ttk.Label(r, text=" 端口").pack(side="left", padx=4)
        self.var_server_port = tk.IntVar(value=int(self.cfg.get("server_port", 25565)))
        ttk.Spinbox(r, from_=1, to=65535, textvariable=self.var_server_port, width=7).pack(side="left", padx=4)
        tip = ttk.Label(g4, justify="left", text="填了服务器地址后，点「启动」会直接进入该服务器；留空则进游戏主菜单。\n"
                                               "好友联机（同一网络）：一人进单人世界后按 ESC →「对局域网开放」，"
                                               "其他人在多人游戏列表里就能看到。跨网络联机需服务器或内网穿透工具。")
        tip.pack(fill="x", padx=12, pady=(0, 6))

        ttk.Button(f, text="保存设置", command=self._save_settings).pack(anchor="e", padx=16, pady=8)

    # ---------------- 队列与线程 ----------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                getattr(self, "_on_" + kind)(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _run_bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _push(self, kind, payload=None):
        self.q.put((kind, payload))

    def log(self, text):
        self._push("log", str(text))

    def set_status(self, text):
        self._push("status", str(text))

    def set_progress(self, frac, text=""):
        self._push("progress", (max(0.0, min(1.0, frac)), text))

    def _on_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_status(self, text):
        self.var_status.set(text)

    def _on_progress(self, data):
        frac, text = data
        self.progress["value"] = int(frac * 1000)
        if text:
            self.var_status.set(text)

    # ---------------- 版本列表 ----------------
    def _refresh_versions(self, force=False, background=True):
        def work():
            self.set_status("正在获取版本列表…")
            try:
                self.manifest = core.load_manifest(force=force, source=self.cfg.get("download_source", "auto"))
                vs = self.manifest.get("versions", [])
                # 按发布时间倒序
                vs = sorted(vs, key=lambda x: x.get("releaseTime", ""), reverse=True)
                self._push("versions", vs)
            except Exception as e:
                self._push("error", f"获取版本列表失败：{e}")
        if background:
            self._run_bg(work)
        else:
            work()

    def _on_versions(self, vs):
        self.versions = vs
        self._apply_filter()
        # 默认选中 1.12.1
        target = DEFAULT_VERSION
        for item in self.tree.get_children():
            if self.tree.set(item, "version") == target:
                self.tree.selection_set(item)
                self.tree.focus(item)
                self.tree.see(item)
                break
        self.set_status(f"共 {len(vs)} 个版本")

    def _apply_filter(self):
        self.tree.delete(*self.tree.get_children())
        flt = self.var_filter.get()
        key = self.var_search.get().strip().lower()
        for v in self.versions:
            vid = v.get("id", "")
            vtype = v.get("type", "")
            if flt == "正式版" and vtype != "release":
                continue
            if flt == "快照" and vtype != "snapshot":
                continue
            if key and key not in vid.lower():
                continue
            status = "已安装" if core.is_installed(vid, self.manifest, self.cfg) else ""
            self.tree.insert("", "end", values=(vid, vtype, status))
        self._on_select_version()

    def _selected_id(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.set(sel[0], "version")

    def _on_select_version(self, *_):
        vid = self._selected_id()
        if not vid:
            self.var_info.set("请选择版本")
            return
        entry = core.find_in_manifest(self.manifest, vid)
        installed = core.is_installed(vid, self.manifest, self.cfg)
        try:
            v = core.version_json(vid, self.manifest)
            java_req = (v.get("javaVersion") or {}).get("majorVersion", 8)
        except Exception:
            java_req = "?"
        text = (f"版本：{vid}\n类型：{entry.get('type') if entry else '?'}\n"
                f"发布时间：{(entry.get('releaseTime') or '?')[:10] if entry else '?'}\n"
                f"需要 Java：{java_req}+\n状态：{'✅ 已安装' if installed else '未安装'}")
        self.var_info.set(text)

    # ---------------- 安装 / 启动 ----------------
    def _make_installer(self):
        return core.Installer(self.cfg, progress={
            "step": lambda t: self.set_status(t),
            "bar": lambda d, t, txt: self.set_progress(1 if t <= 0 else d / t, txt),
        }, log=self.log)

    def _install_selected(self):
        vid = self._selected_id()
        if not vid:
            return
        if self.busy:
            return
        self.busy = True
        self._set_buttons(False)
        self.set_progress(0, f"开始安装 {vid}")

        def work():
            try:
                self._make_installer().install(vid, self.manifest)
                self.set_progress(1, "安装完成")
                self.log(f"✅ {vid} 安装完成，可以启动了")
                self._push("refresh_status", vid)
            except Exception as e:
                self._push("error", f"安装失败：{e}")
            finally:
                self._push("done", None)

        self._run_bg(work)

    def _launch_selected(self):
        vid = self._selected_id()
        if not vid:
            messagebox.showinfo(APP_TITLE, "请先选择一个版本")
            return
        if self.busy:
            return
        acc = self._active_account()
        if acc is None:
            acc = core.offline_account("Steve")
            self.cfg["accounts"].append(acc)
            self.cfg["active_account"] = len(self.cfg["accounts"]) - 1
            core.save_config(self.cfg)
            self.log("已自动创建离线账号 Steve")
            self._refresh_account_label()
        self.busy = True
        self._set_buttons(False)
        self.set_progress(0, f"准备启动 {vid}")
        self._run_bg(self._launch_worker, vid, acc)

    def _launch_worker(self, vid, acc):
        try:
            v = core.resolve_version(vid, self.manifest, source=self.cfg.get("download_source", "auto"))
            required = (v.get("javaVersion") or {}).get("majorVersion", 8)
            java_path, major = core.pick_java_for_version(required, self.cfg)
            if not java_path:
                self.log(f"⛔ {major}")
                self.log(f"正在自动下载 Java {required} 运行时，请稍候…")
                self.set_status(f"下载 Java {required} 运行时…")

                def prog(d, t):
                    self.set_progress(1 if t <= 0 else d / t, f"下载 Java {required}…")

                java_path = core.download_jre(required, source=self.cfg.get("download_source", "auto"),
                                              progress_cb=prog, log=self.log)
                self.log(f"✅ Java {required} 运行时已就绪：{java_path}")
            if not core.is_installed(vid, self.manifest, self.cfg):
                self.log(f"{vid} 尚未安装，自动安装…")
                self._make_installer().install(vid, self.manifest)
            # 微软账号先刷新令牌
            if acc.get("type") == "msa":
                try:
                    acc = core.MicrosoftAuth(log=self.log).refresh(acc, log=self.log)
                    for i, a in enumerate(self.cfg["accounts"]):
                        if a.get("uuid") == acc.get("uuid"):
                            self.cfg["accounts"][i] = acc
                    core.save_config(self.cfg)
                except Exception as e:
                    self.log(f"[警告] 刷新正版登录失败：{e}（将尝试使用旧令牌）")
            # 设置游戏内语言
            lang = self.cfg.get("game_language", "zh_cn")
            try:
                core.set_game_language(self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR, lang)
                self.log(f"游戏语言已设置为：{lang}")
            except Exception as e:
                self.log(f"[警告] 设置游戏语言失败：{e}")
            # 离线账号自定义皮肤：确保注入器可用
            skin_port = 0
            if acc.get("type") == "offline":
                try:
                    core.ensure_authlib_injector(self.cfg.get("download_source", "auto"), log=self.log)
                except Exception as e:
                    self.log(f"[警告] 皮肤注入器下载失败，本次无自定义皮肤：{e}")
                skin_port = self.skin_server.port if self.skin_server else 0
            self.set_status("构建启动命令…")
            java, major, cmd = core.build_command(vid, acc, self.cfg, self.manifest, log=self.log,
                                                  skin_server_port=skin_port)
            self.log("启动命令：")
            self.log("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
            self._push("game_start", (cmd, vid, acc))
        except Exception as e:
            self._push("error", f"启动失败：{e}")
            self._push("done", None)

    def _on_game_start(self, data):
        cmd, vid, acc = data
        self.log(f"▶ 正在启动 {vid}（{acc.get('name')}）…")
        self.btn_stop.configure(state="normal")
        self.var_status.set(f"游戏中：{vid}")

        def exit_cb(code):
            self._push("game_exit", code)

        self.game = core.GameProcess(cmd, self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR,
                                     line_cb=lambda line: self.log(line), exit_cb=exit_cb)
        try:
            self.game.start()
        except Exception as e:
            self._push("error", f"启动进程失败：{e}")
            self._push("done", None)

    def _on_game_exit(self, code):
        self.log(f"游戏已退出（退出码 {code}）")
        self.btn_stop.configure(state="disabled")
        self.set_status("就绪")
        self.busy = False
        self._set_buttons(True)
        if self.cfg.get("close_after_launch"):
            self.root.after(1500, self.root.destroy)

    def _stop_game(self):
        if self.game:
            self.game.stop()
            self.log("已请求停止游戏")

    def _open_game_dir(self):
        gd = self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR
        os.makedirs(gd, exist_ok=True)
        if os.name == "nt":
            os.startfile(gd)  # noqa
        else:
            subprocess.Popen(["xdg-open", gd])

    def _terracotta_exe(self):
        d = os.path.join(core.BASE_DIR, "tools", "terracotta")
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.lower().endswith(".exe"):
                    return os.path.join(d, name)
        return ""

    def _start_terracotta(self):
        exe = self._terracotta_exe()
        if not exe:
            messagebox.showerror(APP_TITLE, "未找到陶瓦联机程序（tools\\terracotta），请重新下载启动器完整包。")
            return
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
            self.log("已启动陶瓦联机。用法：房主在陶瓦里创建房间拿到房间码 → 好友启动陶瓦输入房间码加入 → "
                     "游戏内「多人游戏」双击「陶瓦联机大厅」即可联机。")
            self.log("陶瓦联机由开源项目 Terracotta 提供（AGPL-3.0，github.com/burningtnt/Terracotta）")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"启动陶瓦联机失败：{e}")

    def _delete_selected(self):
        vid = self._selected_id()
        if not vid:
            return
        if not messagebox.askyesno(APP_TITLE, f"确定删除版本 {vid} 的本地文件吗？"):
            return
        for p in (os.path.join(self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR, "versions", vid),
                  os.path.join(core.NATIVES_ROOT, vid)):
            shutil_rm(p)
        self._apply_filter()
        self.log(f"已删除 {vid} 本地文件")

    def _on_refresh_status(self, vid):
        self._apply_filter()
        sel = self.tree.selection()
        if sel:
            self._on_select_version()

    def _on_error(self, msg):
        self.log(f"❌ {msg}")
        self.set_status(msg)
        self.busy = False
        self._set_buttons(True)

    def _on_done(self, *_):
        self.busy = False
        self._set_buttons(True)

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        self.btn_install.configure(state=state)
        self.btn_launch.configure(state=state)
        if not enabled and self.game and self.game.running:
            pass
        else:
            self.btn_stop.configure(state=state if self.game and self.game.running else "disabled")

    # ---------------- 账号 ----------------
    def _active_account(self):
        if not self.cfg.get("accounts"):
            return None
        idx = self.cfg.get("active_account", 0)
        if 0 <= idx < len(self.cfg["accounts"]):
            return self.cfg["accounts"][idx]
        return None

    def _use_offline(self):
        name = self.var_offline_name.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "昵称不能为空")
            return
        acc = core.offline_account(name)
        self.cfg["accounts"].append(acc)
        self.cfg["active_account"] = len(self.cfg["accounts"]) - 1
        core.save_config(self.cfg)
        self._refresh_account_label()
        self.log(f"已使用离线账号：{name}")
        self._preview_skin()

    # ---------------- 皮肤 ----------------
    def _skins_dir(self):
        return os.path.join(self.cfg.get("game_dir") or core.DEFAULT_GAME_DIR, "skins")

    def _skin_path_for(self, acc):
        if not acc or acc.get("type") != "offline":
            return None
        return os.path.join(self._skins_dir(), acc.get("uuid", "") + ".png")

    def _preview_skin(self):
        acc = self._active_account()
        p = self._skin_path_for(acc)
        self.skin_img = None
        if p and os.path.isfile(p):
            try:
                self.skin_img = tk.PhotoImage(file=p)
                self.skin_preview.configure(image=self.skin_img, text="")
            except Exception:
                self.skin_preview.configure(image="", text="（皮肤文件无效）")
        else:
            self.skin_preview.configure(image="", text="（未设置，显示默认 Steve）")

    def _pick_skin(self):
        p = filedialog.askopenfilename(title="选择皮肤图片（PNG，推荐 64x64）",
                                       filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
        if not p:
            return
        acc = self._active_account()
        if not acc or acc.get("type") != "offline":
            acc = core.offline_account(self.var_offline_name.get().strip() or "Steve")
            self.cfg["accounts"].append(acc)
            self.cfg["active_account"] = len(self.cfg["accounts"]) - 1
            core.save_config(self.cfg)
            self._refresh_account_label()
            self.log(f"已自动创建离线账号 {acc['name']}（自定义皮肤需要离线账号）")
        # 校验是否为有效 PNG
        try:
            with open(p, "rb") as f:
                head = f.read(8)
            if head != b"\x89PNG\r\n\x1a\n":
                raise ValueError("不是 PNG 文件")
            if os.path.getsize(p) > 2 * 1024 * 1024:
                raise ValueError("文件超过 2MB")
            tk.PhotoImage(file=p)  # 能被 Tk 解析才接受
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"皮肤图片无效：{e}")
            return
        dest = self._skin_path_for(acc)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            shutil.copyfile(p, dest)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"保存皮肤失败：{e}")
            return
        self.log(f"皮肤已设置：{dest}")
        self._preview_skin()

    def _remove_skin(self):
        acc = self._active_account()
        p = self._skin_path_for(acc)
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                self.log("皮肤已移除")
            except OSError as e:
                self.log(f"[警告] 移除皮肤失败：{e}")
        self._preview_skin()

    def _ms_login(self):
        self.auth_flow = threading.Thread(target=self._ms_login_worker, daemon=True)
        self.auth_flow.start()

    def _ms_login_worker(self):
        auth = core.MicrosoftAuth(log=self.log)
        try:
            self.set_status("正在发起微软登录…")
            dev, code, uri, expires, interval, _msg = auth.begin()
            self._push("ms_code", (code, uri, expires))
            self.log(f"请在浏览器打开 {uri} 并输入代码 {code}（{expires // 60} 分钟内有效）")

            def status_cb(t):
                self._push("ms_status", t)

            self.log("等待授权…")
            msa, refresh = auth.wait(dev, interval, status_cb=status_cb)
            self.log("授权成功，正在获取 Minecraft 档案…")
            acc = auth.complete(msa, refresh, log=self.log)
            self.cfg["accounts"].append(acc)
            self.cfg["active_account"] = len(self.cfg["accounts"]) - 1
            core.save_config(self.cfg)
            self._push("ms_done", acc["name"])
            self.log(f"✅ 正版登录成功：{acc['name']}")
        except Exception as e:
            self._push("ms_fail", str(e))

    def _on_ms_code(self, data):
        code, uri, _expires = data
        dlg = tk.Toplevel(self.root)
        dlg.title("微软登录")
        dlg.geometry("380x200")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg, text="请在浏览器中打开以下链接并输入代码：", padding=(10, 8)).pack(anchor="w")
        ttk.Label(dlg, text=uri, foreground="#0066cc", cursor="hand2").pack(anchor="w", padx=10)
        ttk.Label(dlg, text=code, style="Big.TLabel", padding=6).pack()
        row = ttk.Frame(dlg)
        row.pack(pady=4)
        ttk.Button(row, text="复制代码", command=lambda: self._copy_text(code)).pack(side="left", padx=4)
        ttk.Button(row, text="打开链接", command=lambda: webbrowser.open(uri)).pack(side="left", padx=4)
        self.ms_dlg = dlg
        self.ms_status_lbl = ttk.Label(dlg, text="等待授权…", padding=6)
        self.ms_status_lbl.pack()

    def _on_ms_status(self, text):
        if getattr(self, "ms_status_lbl", None):
            try:
                self.ms_status_lbl.configure(text=text)
            except tk.TclError:
                pass

    def _on_ms_done(self, name):
        if getattr(self, "ms_dlg", None):
            try:
                self.ms_dlg.destroy()
            except tk.TclError:
                pass
        self._refresh_account_label()
        self.set_status(f"已登录：{name}")

    def _on_ms_fail(self, msg):
        if getattr(self, "ms_dlg", None):
            try:
                self.ms_dlg.destroy()
            except tk.TclError:
                pass
        messagebox.showerror(APP_TITLE, f"微软登录失败：\n{msg}")
        self.set_status("登录失败")

    def _logout(self):
        idx = self.cfg.get("active_account", 0)
        if idx < len(self.cfg.get("accounts", [])):
            acc = self.cfg["accounts"][idx]
            if messagebox.askyesno(APP_TITLE, f"注销账号 {acc.get('name')}？"):
                self.cfg["accounts"].pop(idx)
                self.cfg["active_account"] = 0
                core.save_config(self.cfg)
        self._refresh_account_label()

    def _refresh_account_label(self):
        acc = self._active_account()
        if not acc:
            self.lbl_account.configure(text="账号：未设置")
            self.var_acc_info.set("当前无正版账号")
            return
        tag = "正版" if acc.get("type") == "msa" else "离线"
        self.lbl_account.configure(text=f"账号：{acc.get('name')}（{tag}）")
        if acc.get("type") == "msa":
            self.var_acc_info.set(f"当前正版账号：{acc.get('name')}（UUID {acc.get('uuid')}）")
        else:
            self.var_acc_info.set("当前未登录正版账号")

    def _copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("已复制验证码")

    # ---------------- 设置 ----------------
    def _detect_java(self):
        self._run_bg(self._detect_java_worker)

    def _detect_java_worker(self):
        javas = core.list_javas()
        self._push("java_list", javas)

    def _on_java_list(self, javas):
        if not javas:
            self.lbl_java_detect.configure(text="未检测到 Java")
            return
        top = javas[0]
        self.lbl_java_detect.configure(text=f"Java {top['major']}（共 {len(javas)} 个）")
        self._refresh_java_label()

    def _refresh_java_label(self):
        self._run_bg(self._refresh_java_label_worker)

    def _refresh_java_label_worker(self):
        javas = core.list_javas()
        self._push("java_badge", javas)

    def _on_java_badge(self, javas):
        if javas:
            top = javas[0]
            self.lbl_java.configure(text=f"Java：Java {top['major']} 可用（{top['path'][:60]}…）" if len(top["path"]) > 60 else f"Java：Java {top['major']}（{top['path']}）")
            self.lbl_java_detect.configure(text=f"Java {top['major']}（共 {len(javas)} 个）")
        else:
            self.lbl_java.configure(text="Java：未检测到（可在设置页自动下载）")
            self.lbl_java_detect.configure(text="未检测到 Java")

    def _download_jre(self):
        if self.busy:
            return
        self.busy = True
        self._set_buttons(False)
        major = int(self.var_jre_major.get())
        self.set_progress(0, f"下载 Java {major} 运行时…")
        self.log(f"开始下载 Java {major} 运行时（Adoptium）…")

        def work():
            try:
                def prog(d, t):
                    self.set_progress(1 if t <= 0 else d / t, f"下载 Java {major} 运行时…")

                path = core.download_jre(major, source=self.cfg.get("download_source", "auto"),
                                         progress_cb=prog, log=self.log)
                self.log(f"✅ Java {major} 运行时已就绪：{path}")
                self._push("java_list", core.list_javas())
                self._push("done", None)
                self.set_status("下载完成")
            except Exception as e:
                self._push("error", f"Java 运行时下载失败：{e}")

        self._run_bg(work)

    def _browse_gamedir(self):
        d = filedialog.askdirectory(title="选择游戏目录", initialdir=self.var_gamedir.get())
        if d:
            self.var_gamedir.set(os.path.normpath(d))

    def _browse_java(self):
        p = filedialog.askopenfilename(title="选择 java.exe",
                                       filetypes=[("java.exe", "java.exe"), ("所有文件", "*.*")])
        if p:
            self.var_java_path.set(os.path.normpath(p))

    def _save_settings(self):
        try:
            self.cfg["game_dir"] = os.path.normpath(self.var_gamedir.get())
            self.cfg["java_mode"] = self.var_java_mode.get()
            self.cfg["java_path"] = os.path.normpath(self.var_java_path.get())
            self.cfg["mem_min"] = int(self.var_mem_min.get())
            self.cfg["mem_max"] = int(self.var_mem_max.get())
            if self.cfg["mem_max"] < self.cfg["mem_min"]:
                self.cfg["mem_max"] = self.cfg["mem_min"]
            self.cfg["custom_resolution"] = bool(self.var_custom_res.get())
            self.cfg["res_w"] = int(self.var_res_w.get())
            self.cfg["res_h"] = int(self.var_res_h.get())
            self.cfg["download_source"] = self.var_source.get()
            self.cfg["max_threads"] = max(1, min(64, int(self.var_threads.get())))
            self.cfg["close_after_launch"] = bool(self.var_close.get())
            self.cfg["jvm_extra"] = self.var_jvm_extra.get().strip()
            self.cfg["game_language"] = self.var_game_lang.get()
            self.cfg["server"] = self.var_server.get().strip()
            self.cfg["server_port"] = max(1, min(65535, int(self.var_server_port.get())))
            core.save_config(self.cfg)
            self.lbl_gamedir.configure(text=f"游戏目录：{self.cfg['game_dir']}")
            self.set_status("设置已保存")
            self.log("设置已保存")
            self._refresh_java_label()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存设置失败：{e}")

    # ---------------- 其他 ----------------
    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _on_close(self):
        if self.game and self.game.running:
            if not messagebox.askyesno(APP_TITLE, "游戏仍在运行，确定退出启动器吗？（游戏也会被关闭）"):
                return
            self.game.stop()
        self.root.destroy()


def shutil_rm(path):
    import shutil
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    try:
        root = tk.Tk()
        App(root)
        if "--smoke" in sys.argv:
            root.after(3000, root.destroy)
        root.mainloop()
        if "--smoke" in sys.argv:
            print("GUI 正常退出（冒烟测试通过）")
    except Exception:
        # 任何启动失败都写入日志文件，方便排查"打不开"的问题
        import datetime
        import traceback
        logp = os.path.join(core.BASE_DIR, "launcher_error.log")
        try:
            with open(logp, "a", encoding="utf-8") as f:
                f.write(f"\n===== [{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] 启动失败 =====\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
