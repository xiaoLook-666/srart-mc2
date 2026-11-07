# -*- coding: utf-8 -*-
"""
Minecraft 启动器 —— 核心模块
功能：版本清单 / 安装下载 / Java 检测与运行时下载 / 账号（离线+微软）/ 启动命令构建
仅依赖 Python 标准库；可独立在命令行运行用于测试：
    python mc_core.py manifest                查看版本清单统计
    python mc_core.py version <id>            解析并展示某个版本
    python mc_core.py javas                   检测系统 Java
    python mc_core.py download-jre <8|17|21|25>  自动下载 Java 运行时
    python mc_core.py install <id> [--skip-assets] [--source auto|official|mirror]
    python mc_core.py dryrun <id>             打印启动命令（不实际启动）
    python mc_core.py offline-login <昵称>     生成离线账号 UUID
"""
import argparse
import base64
import hashlib
import http.client
import http.server
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))  # PyInstaller 打包后：exe 所在目录
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
META_DIR = os.path.join(BASE_DIR, "meta")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
NATIVES_ROOT = os.path.join(BASE_DIR, "natives")
DEFAULT_GAME_DIR = os.path.join(BASE_DIR, ".minecraft")

UA = "MC-Launcher/1.0 (Python)"
MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_MIRROR = "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json"
CLIENT_ID = "4a07b708-b86d-4365-a55f-f4f23ecb85ab"  # 公共设备代码流客户端（社区广泛验证可用）
MSA_SCOPE = "XboxLive.signin offline_access openid profile email"

OS_NAME = "windows" if sys.platform == "win32" else ("osx" if sys.platform == "darwin" else "linux")
_arch = platform.machine().lower()
OS_ARCH = "x86_64" if _arch in ("amd64", "x86_64") else _arch
NATIVES_OS = {"windows": "natives-windows", "osx": "natives-osx", "linux": "natives-linux"}[OS_NAME]

log = print  # 简单日志回调，GUI 会替换


# ============================================================ 通用 HTTP

def _request(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    return urllib.request.urlopen(req, timeout=timeout)


def http_get_bytes(url, timeout=30, tries=2):
    last = None
    for i in range(tries):
        try:
            with _request(url, timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.0 * (i + 1))
    raise last


def http_get_json(url, timeout=30, tries=2):
    return json.loads(http_get_bytes(url, timeout, tries).decode("utf-8"))


def http_post_json(url, data, headers=None, timeout=30):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_form(url, params, timeout=30):
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def download_to(url, dest, sha1=None, sha256=None, mirror=None, source="auto", progress_cb=None, timeout=60):
    """下载单个大文件，支持重试与镜像回退，校验哈希，带进度回调 progress_cb(done, total)。"""
    sources = []
    if source == "official":
        sources = [url]
    elif source == "mirror":
        sources = [mirror, url] if mirror else [url]
    else:
        sources = [url, mirror] if mirror else [url]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    errors = []
    for src in sources:
        if not src:
            continue
        try:
            _download_to(src, tmp, sha1, sha256, progress_cb, timeout)
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(tmp, dest)
            return dest
        except Exception as e:
            errors.append(f"{src}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    raise RuntimeError("下载失败：" + " | ".join(errors[-2:]))


def _download_to(url, tmp, sha1=None, sha256=None, progress_cb=None, timeout=60):
    h1 = hashlib.sha1() if sha1 else None
    h256 = hashlib.sha256() if sha256 else None
    done = 0
    total = 0
    with _request(url, timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                if h1:
                    h1.update(chunk)
                if h256:
                    h256.update(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
    if total and done != total:
        raise IOError(f"传输不完整 {done}/{total}")
    if sha1 and h1.hexdigest() != sha1:
        raise IOError(f"SHA1 校验失败（期望 {sha1[:12]}…，实际 {h1.hexdigest()[:12]}…）")
    if sha256 and h256.hexdigest() != sha256:
        raise IOError("SHA256 校验失败")


def download_many(items, workers=24, progress_cb=None):
    """并发下载大量小文件（资源对象）。item = {url, mirror, dest, sha1, size, name}。
    每线程复用 HTTP 连接。progress_cb(done_bytes, total_bytes)。返回 [(name, error), ...]。
    注意：调用方必须保证各 item 的 dest 互不相同（同一 hash 只提交一次）。"""
    q = queue.Queue()
    for it in items:
        q.put(it)
    state = {"done": 0, "total": sum(it.get("size") or 0 for it in items), "lock": threading.Lock(),
             "last_cb": 0.0}
    errors = []
    elock = threading.Lock()

    def _emit():
        if progress_cb:
            with state["lock"]:
                now = time.time()
                if now - state["last_cb"] < 0.2:
                    return
                state["last_cb"] = now
                progress_cb(state["done"], state["total"])

    def worker():
        tls = threading.local()
        while True:
            try:
                it = q.get_nowait()
            except queue.Empty:
                return
            conns = getattr(tls, "conns", None)
            if conns is None:
                conns = {}
                tls.conns = conns
            srcs = []
            if it.get("source") == "mirror":
                srcs = [it["mirror"], it["url"]]
            elif it.get("source") == "official":
                srcs = [it["url"]]
            else:
                srcs = [it["url"], it["mirror"]]
            srcs = [s for s in srcs if s]
            ok = False
            last_err = None
            for src in srcs:
                for attempt in range(2):
                    try:
                        _conn_download(conns, src, it)
                        ok = True
                        break
                    except Exception as e:
                        last_err = e
                        conn = conns.pop(_key_of(src), None)
                        if conn:
                            try:
                                conn.close()
                            except Exception:
                                pass
                if ok:
                    break
            if not ok:
                with elock:
                    errors.append((it.get("name") or it.get("url"), str(last_err)))
            else:
                with state["lock"]:
                    state["done"] += it.get("size") or 0
            _emit()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, max(1, len(items))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if progress_cb:
        progress_cb(state["done"], state["total"])
    return errors


def _key_of(url):
    u = urllib.parse.urlsplit(url)
    return (u.netloc,)


def _conn_download(conns, url, it):
    u = urllib.parse.urlsplit(url)
    key = u.netloc
    conn = conns.get(key)
    if conn is None:
        conn = http.client.HTTPSConnection(u.netloc, timeout=30)
        conns[key] = conn
    path = u.path + (("?" + u.query) if u.query else "")
    conn.request("GET", path, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    resp = conn.getresponse()
    if resp.status != 200:
        resp.read()
        raise IOError(f"HTTP {resp.status}")
    expected = it.get("size")
    total = int(resp.getheader("Content-Length") or 0)
    h = hashlib.sha1()
    dest = it["dest"]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    done = 0
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
        if total and done != total:
            raise IOError(f"传输不完整 {done}/{total}")
        if (expected is not None and expected and done != expected):
            raise IOError(f"大小不符 {done}/{expected}")
        if it.get("sha1") and h.hexdigest() != it["sha1"]:
            raise IOError("SHA1 校验失败")
        if os.path.exists(dest):
            os.remove(dest)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ============================================================ 配置

DEFAULT_CONFIG = {
    "game_dir": DEFAULT_GAME_DIR,
    "java_mode": "auto",          # auto: 自动选择最佳 Java；manual: 使用 java_path
    "java_path": "",
    "mem_min": 1024,              # MB
    "mem_max": 4096,
    "custom_resolution": False,
    "res_w": 854,
    "res_h": 480,
    "download_source": "auto",    # auto / official / mirror
    "max_threads": 24,
    "close_after_launch": False,
    "jvm_extra": "",
    "game_language": "zh_cn",     # 游戏内语言（写入 options.txt）
    "server": "",                 # 联机：启动后直连的服务器地址（留空不直连）
    "server_port": 25565,
    "accounts": [],
    "active_account": 0,
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update(saved)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(BASE_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ============================================================ Java 检测

def parse_java_major(version_output: str):
    m = re.search(r'version "([^"]+)"', version_output)
    if not m:
        return 0
    v = m.group(1)
    parts = v.split(".")[:2]
    try:
        major = int(parts[0])
        if major == 1:
            major = int(parts[1])
        return major
    except (ValueError, IndexError):
        return 0


def _reg_query(args):
    try:
        r = subprocess.run(["reg", "query", *args], capture_output=True, text=True, timeout=15)
        return r.stdout
    except Exception:
        return ""


def _java_homes_from_registry():
    """从注册表收集 JavaHome。"""
    homes = []
    roots = [("HKLM\\SOFTWARE", ""), ("HKCU\\SOFTWARE", ""), ("HKLM\\SOFTWARE\\WOW6432Node", "")]
    bases = [
        "JavaSoft\\JDK",
        "JavaSoft\\Java Runtime Environment",
        "Eclipse Adoptium\\JDK",
        "Eclipse Foundation\\JDK",
        "Microsoft\\JDK",
        "Amazon Corretto\\JDK",
        "Zulu\\zulu",
    ]
    for root, _ in roots:
        for base in bases:
            out = _reg_query([f"{root}\\{base}", "/s"])
            for line in out.splitlines():
                if "JavaHome" in line and "REG_SZ" in line:
                    parts = line.split("REG_SZ")
                    if len(parts) == 2:
                        p = parts[1].strip()
                        if p and p not in homes:
                            homes.append(p)
    return homes


def _scan_dir_for_java(base):
    found = []
    if not os.path.isdir(base):
        return found
    for dirpath, dirnames, _ in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".cache",)]
        if os.path.isfile(os.path.join(dirpath, "bin", "java.exe")):
            found.append(os.path.join(dirpath, "bin", "java.exe"))
        if len(found) > 8:
            break
    return found


def list_javas():
    """返回 [{major, path, source}]，按 major 降序。"""
    candidates = {}
    seen = set()

    def add(path, source):
        p = os.path.normpath(path)
        if p in seen or not os.path.isfile(p):
            return
        seen.add(p)
        try:
            r = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=15)
            out = (r.stdout or "") + (r.stderr or "")
            major = parse_java_major(out)
        except Exception:
            return
        if major:
            candidates.setdefault(major, {"major": major, "path": p, "source": source})

    for home in _java_homes_from_registry():
        add(os.path.join(home, "bin", "java.exe"), "注册表")
    env_home = os.environ.get("JAVA_HOME")
    if env_home:
        add(os.path.join(env_home, "bin", "java.exe"), "JAVA_HOME")
    path_var = os.environ.get("PATH", "")
    for d in path_var.split(os.pathsep):
        if d:
            add(os.path.join(d, "java.exe"), "PATH")
    common = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for c in common:
        if not c:
            continue
        for sub in ("Java", "Eclipse Adoptium", "Eclipse Foundation", "Microsoft", "Amazon Corretto", "Zulu", "BellSoft"):
            for exe in _scan_dir_for_java(os.path.join(c, sub)):
                add(exe, "常见目录")
    # 启动器自带运行时
    for exe in _scan_dir_for_java(RUNTIME_DIR):
        add(exe, "启动器运行时")

    result = sorted(candidates.values(), key=lambda x: -x["major"])
    return result


def best_java(required_major=None, config=None):
    """选择最佳 Java：优先满足版本要求且 major 最高；否则任意已安装；返回 (path, major) 或 None。"""
    javas = list_javas()
    if required_major is not None:
        for j in javas:
            if j["major"] >= required_major:
                return j["path"], j["major"]
    if javas:
        return javas[0]["path"], javas[0]["major"]
    return None, None


def pick_java_for_version(required_major, config):
    """按配置选择 Java。手动路径无效时自动回退到自动检测。返回 (path, major) 或 (None, 原因)。"""
    if config.get("java_mode") == "manual" and config.get("java_path"):
        p = config["java_path"]
        if os.path.isfile(p):
            try:
                r = subprocess.run([p, "-version"], capture_output=True, text=True, timeout=15)
                major = parse_java_major((r.stdout or "") + (r.stderr or ""))
            except Exception:
                major = 0
            if major > 0:
                if major < required_major:
                    return None, f"手动指定的 Java 为 {major}，但该版本需要 Java {required_major}+（请到设置页更改）"
                return p, major
        # 手动路径无效/不存在 → 回退自动检测
    path, major = best_java(required_major, config)
    if path:
        return path, major
    javas = list_javas()
    if javas:
        return None, f"未找到 Java {required_major}+（检测到最高为 Java {javas[0]['major']}）"
    return None, "未检测到任何 Java"


# ============================================================ Java 运行时下载（Adoptium）

def download_jre(major, source="auto", progress_cb=None, log=log):
    """下载并解压 Adoptium JRE（major: 8/17/21/25…）。返回 java.exe 绝对路径。"""
    for image in ("jre", "jdk"):
        api = (f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
               f"?architecture=x64&image_type={image}&os=windows&vendor=eclipse")
        try:
            data = http_get_json(api, timeout=30)
        except Exception as e:
            log(f"  Adoptium API 查询失败（{image}）：{e}")
            continue
        if not isinstance(data, list) or not data:
            continue
        for asset in data:
            pkg = (asset.get("binary") or {}).get("package") or {}
            name = pkg.get("name")
            link = pkg.get("link")
            if not name or not link:
                continue
            checksum = pkg.get("checksum") or ""
            dest = os.path.join(RUNTIME_DIR, name)
            if os.path.exists(dest) and checksum:
                try:
                    h = hashlib.sha256()
                    with open(dest, "rb") as f:
                        for c in iter(lambda: f.read(1 << 20), b""):
                            h.update(c)
                    if h.hexdigest() == checksum:
                        log(f"  {name} 已存在且校验通过")
                        break
                except OSError:
                    pass
            mirror = f"https://mirrors.tuna.tsinghua.edu.cn/Adoptium/{major}/{image}/x64/windows/{name}"
            log(f"  开始下载 Java {major} 运行时：{name}（约 {round((pkg.get('size') or 0) / 1048576)} MB）")
            download_to(link, dest, sha256=checksum or None, mirror=mirror, source=source,
                        progress_cb=progress_cb, timeout=120)
            break
        # 解压
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        zip_path = os.path.join(RUNTIME_DIR, name)
        if not os.path.exists(zip_path):
            continue
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(RUNTIME_DIR)
        for dirpath, dirnames, _ in os.walk(RUNTIME_DIR):
            dirnames[:] = [d for d in dirnames if d != "cache"]
            exe = os.path.join(dirpath, "bin", "java.exe")
            if os.path.isfile(exe):
                return exe
    raise RuntimeError(f"Java {major} 运行时下载失败")


# ============================================================ 版本清单

def load_manifest(force=False, source="auto"):
    os.makedirs(META_DIR, exist_ok=True)
    cache = os.path.join(META_DIR, "version_manifest.json")
    if not force and os.path.exists(cache):
        try:
            age = time.time() - os.path.getmtime(cache)
            if age < 6 * 3600:
                with open(cache, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
    srcs = []
    if source == "mirror":
        srcs = [MANIFEST_MIRROR, MANIFEST_URL]
    elif source == "official":
        srcs = [MANIFEST_URL]
    else:
        srcs = [MANIFEST_URL, MANIFEST_MIRROR]
    last = None
    for s in srcs:
        try:
            data = http_get_json(s, timeout=30, tries=2)
            with open(cache, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return data
        except Exception as e:
            last = e
    raise RuntimeError(f"获取版本清单失败：{last}")


def find_in_manifest(manifest, version_id):
    for v in manifest.get("versions", []):
        if v.get("id") == version_id:
            return v
    return None


def version_json(version_id, manifest=None, force=False, source="auto"):
    """获取版本 JSON（优先本地缓存）。"""
    d = os.path.join(BASE_DIR, "versions", version_id)
    cache = os.path.join(d, f"{version_id}.json")
    if not force and os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    entry = find_in_manifest(manifest, version_id) if manifest else None
    if not entry:
        manifest = load_manifest(source=source)
        entry = find_in_manifest(manifest, version_id)
    if not entry:
        raise RuntimeError(f"清单中找不到版本 {version_id}")
    url = entry["url"]
    mirror = f"https://bmclapi2.bangbang93.com/version/{urllib.parse.quote(version_id)}/json"
    os.makedirs(d, exist_ok=True)
    data = http_get_json(url, timeout=30, tries=2)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def resolve_version(version_id, manifest=None, source="auto", _seen=None):
    """处理 inheritsFrom 继承链，返回合并后的版本 JSON。"""
    _seen = _seen or set()
    v = version_json(version_id, manifest, source=source)
    if v.get("inheritsFrom"):
        parent_id = v["inheritsFrom"]
        if parent_id in _seen:
            raise RuntimeError("inheritsFrom 循环引用")
        _seen.add(parent_id)
        parent = resolve_version(parent_id, manifest, source, _seen)
        merged = json.loads(json.dumps(parent))
        for k, val in v.items():
            merged[k] = val
        merged["libraries"] = (parent.get("libraries") or []) + (v.get("libraries") or [])
        if "arguments" in v:
            merged["arguments"] = v["arguments"]
        elif "arguments" in parent:
            merged["arguments"] = parent["arguments"]
        for k in ("mainClass", "assetIndex", "javaVersion", "logging", "downloads"):
            if k not in v and k in parent:
                merged[k] = parent[k]
        return merged
    return v


# ============================================================ 规则匹配

def rule_matches(rule, features):
    osr = rule.get("os")
    if osr:
        name = (osr.get("name") or "").lower()
        if name and name != OS_NAME:
            return False
        arch = osr.get("arch")
        if arch and arch != OS_ARCH:
            return False
        ver = osr.get("version")
        if ver:
            try:
                if not re.fullmatch(ver, platform.release() or ""):
                    return False
            except re.error:
                return False
    feats = rule.get("features")
    if feats:
        for k, want in feats.items():
            if bool(features.get(k)) != bool(want):
                return False
    return True


def rules_allow(rules, features=None):
    features = features or {}
    if not rules:
        return True
    for r in rules:
        if rule_matches(r, features):
            return r.get("action") == "allow"
    return True


# ============================================================ 安装器

class Installer:
    """下载并安装一个版本：客户端 jar、库、原生库、日志配置、资源。"""

    def __init__(self, config, game_dir=None, progress=None, log=log):
        self.cfg = config
        self.game_dir = game_dir or config.get("game_dir") or DEFAULT_GAME_DIR
        self.source = config.get("download_source", "auto")
        self.progress = progress  # 可选：{"step": fn(text), "bar": fn(done,total,text)}
        self.log = log
        self.assets_dir = os.path.join(self.game_dir, "assets")
        self.libs_dir = os.path.join(self.game_dir, "libraries")
        self.versions_dir = os.path.join(self.game_dir, "versions")

    def _step(self, text):
        self.log(f"[安装] {text}")
        if self.progress:
            self.progress["step"](text)

    def _bar(self, done, total, text=""):
        if self.progress:
            self.progress["bar"](done, total, text)

    # ---------- 库
    def _natives_classifier(self):
        key = NATIVES_OS
        if OS_ARCH not in ("x86_64", "amd64"):
            key += "-" + OS_ARCH
        return key

    def _lib_downloads(self, v):
        """返回 (classpath_jar_paths, natives_zips) —— 均经规则过滤并去重。"""
        cp = []
        natives = []
        for lib in v.get("libraries", []):
            if not rules_allow(lib.get("rules")):
                continue
            dl = lib.get("downloads") or {}
            art = dl.get("artifact")
            if art and art["path"] not in cp:
                cp.append(art["path"])
            cls = dl.get("classifiers") or {}
            if lib.get("natives"):
                key = self._natives_classifier()
                nz = cls.get(key) or cls.get(NATIVES_OS)
                if nz and nz["path"] not in [n["path"] for n in natives]:
                    natives.append(nz)
        return cp, natives

    def _download_libs(self, v):
        cp_paths, natives = self._lib_downloads(v)
        for p in cp_paths:
            art = None
            for lib in v.get("libraries", []):
                if rules_allow(lib.get("rules")) and (lib.get("downloads") or {}).get("artifact", {}).get("path") == p:
                    art = lib["downloads"]["artifact"]
                    break
            dest = os.path.join(self.libs_dir, p)
            if os.path.exists(dest) and os.path.getsize(dest) == (art.get("size") or 0):
                continue
            url = art.get("url") or f"https://libraries.minecraft.net/{p}"
            mirror = f"https://bmclapi2.bangbang93.com/maven/{p}"
            download_to(url, dest, sha1=art.get("sha1"), mirror=mirror, source=self.source)
        for nz in natives:
            dest = os.path.join(self.libs_dir, nz["path"])
            if not (os.path.exists(dest) and os.path.getsize(dest) == (nz.get("size") or 0)):
                url = nz.get("url") or f"https://libraries.minecraft.net/{nz['path']}"
                mirror = f"https://bmclapi2.bangbang93.com/maven/{nz['path']}"
                download_to(url, dest, sha1=nz.get("sha1"), mirror=mirror, source=self.source)
        return cp_paths, natives

    def _extract_natives(self, version_id, natives):
        out = os.path.join(NATIVES_ROOT, version_id)
        needed = False
        for nz in natives:
            zip_path = os.path.join(self.libs_dir, nz["path"])
            marker = os.path.join(out, ".ok-" + os.path.basename(zip_path))
            if os.path.exists(marker):
                continue
            needed = True
            os.makedirs(out, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path) as z:
                    for info in z.infolist():
                        name = info.filename.replace("\\", "/")
                        if name.startswith("META-INF") or info.is_dir():
                            continue
                        target = os.path.join(out, os.path.basename(name))
                        with z.open(info) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("ok")
            except Exception as e:
                self.log(f"  [警告] 解压原生库失败 {os.path.basename(zip_path)}: {e}")
        if needed:
            self.log(f"  原生库已解压到 {out}")
        return out

    # ---------- 资源
    def _asset_index(self, v):
        ai = v.get("assetIndex") or {}
        index_id = ai.get("id", "legacy")
        dest = os.path.join(self.assets_dir, "indexes", f"{index_id}.json")
        if not os.path.exists(dest):
            url = ai.get("url")
            mirror = f"https://bmclapi2.bangbang93.com/assets/indexes/{urllib.parse.quote(index_id)}.json"
            download_to(url, dest, sha1=ai.get("sha1"), mirror=mirror, source=self.source)
        with open(dest, "r", encoding="utf-8") as f:
            return json.load(f), index_id

    def _download_assets(self, index, index_id, done_cb=None):
        objects = index.get("objects") or {}
        virtual = bool(index.get("virtual"))
        map_res = bool(index.get("map_to_resources"))
        items = []
        seen_hash = set()
        for name, obj in objects.items():
            h = obj["hash"]
            dest = os.path.join(self.assets_dir, "objects", h[:2], h)
            size = obj.get("size") or 0
            if os.path.exists(dest) and (size == 0 or os.path.getsize(dest) == size):
                continue
            if h in seen_hash:  # 同一哈希被多个条目引用，只下载一次
                continue
            seen_hash.add(h)
            url = f"https://resources.download.minecraft.net/{h[:2]}/{h}"
            mirror = f"https://bmclapi2.bangbang93.com/assets/{h[:2]}/{h}"
            items.append({"url": url, "mirror": mirror, "dest": dest, "sha1": h, "size": size,
                          "name": name, "source": self.source})
        if items:
            self.log(f"  资源对象缺失 {len(items)}/{len(objects)} 个，开始下载…")

            def prog(done, total):
                self._bar(done, total, "下载资源文件…")

            errs = download_many(items, workers=max(1, self.cfg.get("max_threads", 24)), progress_cb=prog)
            if errs:
                self.log(f"  [警告] {len(errs)} 个资源下载失败：{errs[:5]}…")
            else:
                self.log("  资源对象下载完成")
        # 旧版资源映射
        if virtual:
            vdir = os.path.join(self.assets_dir, "virtual", str(index_id))
            n = 0
            for name, obj in objects.items():
                src = os.path.join(self.assets_dir, "objects", obj["hash"][:2], obj["hash"])
                dst = os.path.join(vdir, name.replace("/", os.sep))
                if not os.path.exists(dst) and os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
                    n += 1
            self.log(f"  旧版虚拟资源已复制 {n} 个到 {vdir}")
        if map_res:
            rdir = os.path.join(self.game_dir, "resources")
            n = 0
            for name, obj in objects.items():
                src = os.path.join(self.assets_dir, "objects", obj["hash"][:2], obj["hash"])
                dst = os.path.join(rdir, name.replace("/", os.sep))
                if not os.path.exists(dst) and os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(src, dst)
                    n += 1
            self.log(f"  远古版本资源已复制 {n} 个到 {rdir}")

    # ---------- 主流程
    def install(self, version_id, manifest=None, skip_assets=False):
        v = resolve_version(version_id, manifest, source=self.source)
        os.makedirs(self.versions_dir, exist_ok=True)

        self._step(f"下载 {version_id} 客户端文件")
        client = (v.get("downloads") or {}).get("client") or {}
        jar = os.path.join(self.versions_dir, version_id, f"{version_id}.jar")
        if not (os.path.exists(jar) and os.path.getsize(jar) == (client.get("size") or 0)):
            url = client.get("url")
            mirror = f"https://bmclapi2.bangbang93.com/version/{urllib.parse.quote(version_id)}/client"
            download_to(url, jar, sha1=client.get("sha1"), mirror=mirror, source=self.source,
                        progress_cb=lambda d, t: self._bar(d, t, "下载客户端…"))
        else:
            self.log(f"  客户端 {version_id}.jar 已存在")

        self._step(f"下载 {version_id} 依赖库")
        cp_paths, natives = self._download_libs(v)
        self.log(f"  类路径库 {len(cp_paths)} 个，原生库 {len(natives)} 个")

        self._step("解压原生库")
        natives_dir = self._extract_natives(version_id, natives)

        logging_cfg = ((v.get("logging") or {}).get("client") or {}).get("file") or {}
        log_cfg_path = ""
        if logging_cfg.get("url"):
            self._step("下载日志配置")
            dest = os.path.join(self.assets_dir, "log_configs", f"{version_id}.xml")
            if not (os.path.exists(dest) and os.path.getsize(dest) == (logging_cfg.get("size") or 0)):
                download_to(logging_cfg["url"], dest, sha1=logging_cfg.get("sha1"), source=self.source)
            log_cfg_path = dest

        if not skip_assets:
            self._step("处理资源索引与文件")
            index, index_id = self._asset_index(v)
            self._download_assets(index, index_id)
        else:
            index_id = (v.get("assetIndex") or {}).get("id", "legacy")

        self._step("安装完成")
        return {"cp": cp_paths, "natives_dir": natives_dir, "log_cfg": log_cfg_path, "index_id": index_id}


def is_installed(version_id, manifest=None, config=None, game_dir=None, source="auto"):
    """检查版本是否已完整安装（只查本地文件，绝不触发网络下载）。"""
    cfg = config or {}
    gd = game_dir or cfg.get("game_dir") or DEFAULT_GAME_DIR
    jf = os.path.join(BASE_DIR, "versions", version_id, f"{version_id}.json")
    if not os.path.exists(jf):
        return False
    try:
        with open(jf, "r", encoding="utf-8") as f:
            v = json.load(f)
    except Exception:
        return False
    jar = os.path.join(gd, "versions", version_id, f"{version_id}.jar")
    if not os.path.exists(jar):
        return False
    inst = Installer(cfg, game_dir=gd, log=lambda *a: None)
    cp_paths, _natives = inst._lib_downloads(v)
    for p in cp_paths:
        if not os.path.exists(os.path.join(gd, "libraries", p)):
            return False
    ai = v.get("assetIndex") or {}
    idx = os.path.join(gd, "assets", "indexes", f"{ai.get('id', 'legacy')}.json")
    if not os.path.exists(idx):
        return False
    return True


# ============================================================ 账号

def offline_account(name):
    """离线账号：UUID 按 Java 标准由昵称生成。"""
    return {
        "type": "offline",
        "name": name,
        "uuid": str(uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name)),
        "access_token": "0",
        "refresh_token": "",
        "xuid": "0",
    }


def parse_version_id(version_id):
    """'1.8.9' -> (1,8,9)；无法解析返回 None。"""
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", version_id)
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


class MicrosoftAuth:
    """微软账号登录（设备代码流）：Mojang 官方启动器同款客户端 ID。"""

    def __init__(self, log=log):
        self.log = log

    def begin(self):
        """开始设备代码登录。返回 (device_code, user_code, verification_uri, expires_in, interval, message)。"""
        r = http_post_form(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
            {"client_id": CLIENT_ID, "scope": MSA_SCOPE})
        return (r["device_code"], r["user_code"], r.get("verification_uri") or "https://www.microsoft.com/link",
                int(r.get("expires_in", 900)), max(1, int(r.get("interval", 5))), r.get("message", ""))

    def wait(self, device_code, interval, status_cb=None):
        """轮询直到用户完成授权。返回 (access_token, refresh_token)。status_cb(text)。"""
        url = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
        while True:
            try:
                r = http_post_form(url, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID, "device_code": device_code})
                return r["access_token"], r.get("refresh_token", "")
            except urllib.error.HTTPError as e:
                try:
                    err = json.loads(e.read().decode("utf-8"))
                except Exception:
                    err = {}
                code = err.get("error", "")
                if code == "authorization_pending":
                    if status_cb:
                        status_cb("等待授权… 请在浏览器中打开链接并输入代码")
                    time.sleep(interval)
                    continue
                if code == "slow_down":
                    interval += 5
                    time.sleep(interval)
                    continue
                if code in ("authorization_declined", "expired_token"):
                    raise RuntimeError("授权已取消或已过期")
                raise RuntimeError(f"微软登录失败：{code} {err.get('error_description', '')}")
            except Exception as e:
                if status_cb:
                    status_cb(f"网络错误：{e}，重试中…")
                time.sleep(interval)

    def _xbl(self, msa_token):
        r = http_post_json("https://user.auth.xboxlive.com/user/authenticate", {
            "properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": "d=" + msa_token,
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        })
        xui = (r.get("DisplayClaims") or {}).get("xui") or [{}]
        return r["Token"], xui[0].get("uhs", "")

    def _xsts(self, xbl_token):
        r = http_post_json("https://xsts.auth.xboxlive.com/xsts/authorize", {
            "properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbl_token],
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        })
        if r.get("XErr"):
            xerr = r["XErr"]
            if xerr == 2148916233:
                raise RuntimeError("该微软账号没有 Xbox 档案，请先到 xbox.com 创建玩家代号")
            if xerr == 2148916238:
                raise RuntimeError("儿童账号需要家长授权才能登录")
            raise RuntimeError(f"Xbox 验证失败（XErr {xerr}）")
        xui = (r.get("DisplayClaims") or {}).get("xui") or [{}]
        return r["Token"], xui[0].get("uhs", "")

    def _mc_login(self, uhs, xsts_token):
        r = http_post_json("https://api.minecraftservices.com/authentication/login_with_xbox",
                           {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"})
        return r["access_token"]

    def _profile(self, mc_token):
        req = urllib.request.Request("https://api.minecraftservices.com/minecraft/profile",
                                     headers={"Authorization": "Bearer " + mc_token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        return r.get("id", ""), r.get("name", "")

    def _xuid(self, xbl_token):
        try:
            req = urllib.request.Request(
                "https://profile.xboxlive.com/users/me/profile/settings?settings=Gamertag",
                headers={"Authorization": "XBL3.0 x=" + xbl_token, "x-xbl-contract-version": "2",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            users = r.get("profileUsers") or []
            if users:
                return str(users[0].get("id", "0"))
        except Exception:
            pass
        return "0"

    def ownership(self, mc_token):
        """检查是否拥有 Minecraft。返回 True/False/None(无法确定)。"""
        try:
            req = urllib.request.Request("https://api.minecraftservices.com/entitlements/mcstore",
                                         headers={"Authorization": "Bearer " + mc_token})
            with urllib.request.urlopen(req, timeout=30) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            names = [i.get("name", "") for i in r.get("items", [])]
            return any(n in ("product_minecraft", "game_minecraft") for n in names)
        except Exception:
            return None

    def chain(self, msa_token, log=None):
        """MSA token -> 完整的 MC 登录链。返回 {name, uuid, access_token, xuid}。"""
        lf = log or self.log
        lf("  正在通过 Xbox Live 验证…")
        xbl, uhs = self._xbl(msa_token)
        lf("  正在通过 XSTS 验证…")
        xsts, uhs2 = self._xsts(xbl)
        lf("  正在登录 Minecraft 服务…")
        mc = self._mc_login(uhs2 or uhs, xsts)
        uuid_, name = self._profile(mc)
        xuid = self._xuid(xbl)
        return {"name": name, "uuid": uuid_, "access_token": mc, "xuid": xuid}

    def complete(self, msa_token, refresh_token, log=None):
        info = self.chain(msa_token, log)
        own = self.ownership(info["access_token"])
        if own is False:
            log("  [提示] 该账号未检测到 Minecraft 正版授权，可能只能以离线方式游玩单人")
        return {
            "type": "msa",
            "name": info["name"],
            "uuid": info["uuid"],
            "access_token": info["access_token"],
            "refresh_token": refresh_token,
            "xuid": info["xuid"],
        }

    def refresh(self, account, log=None):
        """用 refresh_token 刷新并重新走登录链，更新 account 的 access_token。"""
        lf = log or self.log
        try:
            r = http_post_form("https://login.microsoftonline.com/consumers/oauth2/v2.0/token", {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": account.get("refresh_token", ""),
                "scope": MSA_SCOPE})
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"刷新登录失败（{e.code}），请重新登录")
        msa = r.get("access_token")
        if not msa:
            raise RuntimeError("刷新登录失败，请重新登录")
        info = self.chain(msa, lf)
        account["access_token"] = info["access_token"]
        account["xuid"] = info["xuid"]
        account["uuid"] = info["uuid"]
        account["name"] = info["name"]
        if r.get("refresh_token"):
            account["refresh_token"] = r["refresh_token"]
        return account


# ============================================================ 自定义皮肤（离线账号）

AIL_JAR = os.path.join(BASE_DIR, "injector", "authlib-injector.jar")
AIL_URL = "https://github.com/yushijinhun/authlib-injector/releases/download/v1.2.8/authlib-injector-1.2.8.jar"


def ensure_authlib_injector(source="auto", log=log):
    """确保 authlib-injector 存在，返回 jar 路径。"""
    os.makedirs(os.path.dirname(AIL_JAR), exist_ok=True)
    if os.path.isfile(AIL_JAR) and os.path.getsize(AIL_JAR) > 100 * 1024:
        return AIL_JAR
    log("  正在下载皮肤注入器 authlib-injector…")
    download_to(AIL_URL, AIL_JAR, source=source, timeout=120)
    return AIL_JAR


class SkinServer:
    """内置皮肤服务器：为离线账号提供 Yggdrasil 皮肤 API 与皮肤文件。

    用法：srv = SkinServer(accounts, skins_dir); port = srv.start()
    皮肤文件按账号 UUID 存放：skins_dir/{uuid}.png
    """

    def __init__(self, accounts, skins_dir, log=log):
        self.accounts = accounts      # 引用外部账号列表（原地增删可见）
        self.skins_dir = skins_dir
        self.log = log
        self.httpd = None
        self.port = 0

    def start(self):
        os.makedirs(self.skins_dir, exist_ok=True)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                outer.log("  [皮肤服务] " + (fmt % args))

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                m = re.match(r"^/sessionserver/session/minecraft/profile/([0-9a-fA-F]{32})$", path)
                if m:
                    nodash = m.group(1).lower()
                    for acc in outer.accounts:
                        if (acc.get("uuid") or "").replace("-", "").lower() == nodash:
                            outer.log(f"  皮肤档案请求：{acc.get('name')}")
                            self._send(200, json.dumps(outer._profile(acc)).encode("utf-8"))
                            return
                    self._send(404, b'{"error":"unknown profile"}')
                    return
                m = re.match(r"^/skin/([0-9a-fA-F-]{36})\.png$", path)
                if m:
                    p = os.path.join(outer.skins_dir, m.group(1).lower() + ".png")
                    if os.path.isfile(p) and os.path.getsize(p) <= 2 * 1024 * 1024:
                        with open(p, "rb") as f:
                            self._send(200, f.read(), "image/png")
                    else:
                        self._send(404, b"")
                    return
                if path.startswith("/sessionserver/session/minecraft/hasJoined"):
                    self._send(204)
                    return
                if path in ("/", "/index.html"):
                    meta = {
                        "meta": {
                            "serverName": "MC-Launcher Skin Server",
                            "implementationName": "Yggdrasil",
                            "implementationVersion": "1.0",
                            "links": {"homepage": "https://minecraft.net",
                                      "register": "https://account.mojang.com/register"},
                            "feature.non_email_login": True,
                        }
                    }
                    self._send(200, json.dumps(meta).encode("utf-8"))
                    return
                self._send(404, b"")

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/sessionserver/session/minecraft/join"):
                    self._send(204)
                    return
                self._send(404, b"")

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def _profile(self, acc):
        uuid_ = acc.get("uuid") or ""
        nodash = uuid_.replace("-", "")
        textures = {}
        skin_file = os.path.join(self.skins_dir, uuid_ + ".png")
        if os.path.isfile(skin_file):
            ts = int(os.path.getmtime(skin_file))
            textures["SKIN"] = {"url": f"http://127.0.0.1:{self.port}/skin/{uuid_}.png?t={ts}"}
        value = json.dumps({
            "timestamp": int(time.time() * 1000),
            "profileId": nodash,
            "profileName": acc.get("name", ""),
            "isPublic": True,
            "textures": textures,
        }, ensure_ascii=False)
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return {
            "id": nodash,
            "name": acc.get("name", ""),
            "properties": [{"name": "textures", "value": b64, "signature": ""}],
        }

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
            self.httpd = None


# ============================================================ 启动

def build_command(version_id, account, config, manifest=None, log=log, install_if_missing=True,
                  skin_server_port=0):
    """构建启动命令。返回 (java_path, java_major, cmd)。"""
    cfg = config
    game_dir = cfg.get("game_dir") or DEFAULT_GAME_DIR
    v = resolve_version(version_id, manifest, source=cfg.get("download_source", "auto"))

    required = (v.get("javaVersion") or {}).get("majorVersion", 8)
    java_path, major = pick_java_for_version(required, cfg)
    if not java_path:
        raise RuntimeError(f"无法启动：{major}")
    log(f"  使用 Java {major}：{java_path}")

    inst = Installer(cfg, game_dir=game_dir, log=log)
    cp_paths, _ = inst._lib_downloads(v)
    libs = [os.path.join(game_dir, "libraries", p) for p in cp_paths]
    version_jar = os.path.join(game_dir, "versions", version_id, f"{version_id}.jar")
    classpath = os.pathsep.join([version_jar] + libs)
    natives_dir = os.path.join(NATIVES_ROOT, version_id)
    assets_dir = os.path.join(game_dir, "assets")
    ai = v.get("assetIndex") or {}
    index_id = ai.get("id", "legacy")
    log_cfg = os.path.join(assets_dir, "log_configs", f"{version_id}.xml")
    if not os.path.exists(log_cfg):
        log_cfg = ""

    vt = (find_in_manifest(manifest, version_id) or {}).get("type", "release") if manifest else "release"

    replacements = {
        "${auth_player_name}": account.get("name", ""),
        "${version_name}": version_id,
        "${game_directory}": game_dir,
        "${assets_root}": assets_dir,
        "${assets_index_name}": index_id,
        "${auth_uuid}": account.get("uuid", ""),
        "${auth_access_token}": account.get("access_token", "0"),
        "${user_type}": "msa" if account.get("type") == "msa" else "legacy",
        "${version_type}": vt,
        "${clientid}": "",
        "${auth_xuid}": account.get("xuid", "0"),
        "${user_properties}": "{}",
        "${game_assets}": assets_dir,
        "${auth_session}": f"token:0:{account.get('name', '')}",
        "${launcher_name}": "MC-Launcher",
        "${launcher_version}": "1.0",
        "${classpath}": classpath,
        "${natives_directory}": natives_dir,
        "${library_directory}": os.path.join(game_dir, "libraries"),
    }
    if cfg.get("custom_resolution"):
        replacements["${resolution_width}"] = str(int(cfg.get("res_w", 854)))
        replacements["${resolution_height}"] = str(int(cfg.get("res_h", 480)))

    def sub(s):
        for k, val in replacements.items():
            s = s.replace(k, val)
        return s

    features = {
        "is_demo_user": False,
        "has_custom_resolution": bool(cfg.get("custom_resolution")),
        "has_quick_plays_support": False,
    }

    jvm_args = []
    # 离线账号自定义皮肤：authlib-injector 注入本地皮肤服务
    if (skin_server_port and account.get("type") == "offline" and os.path.isfile(AIL_JAR)
            and os.path.getsize(AIL_JAR) > 100 * 1024):
        jvm_args.append(f"-javaagent:{AIL_JAR}=http://127.0.0.1:{skin_server_port}")
        log(f"  已注入皮肤服务（端口 {skin_server_port}）")
    if "arguments" in v:
        for entry in v["arguments"].get("jvm", []):
            if isinstance(entry, str):
                jvm_args.append(entry)
            elif isinstance(entry, dict) and rules_allow(entry.get("rules"), features):
                for x in entry.get("value", []):
                    if isinstance(x, str):
                        jvm_args.append(x)
    else:
        jvm_args.append(f"-Djava.library.path={natives_dir}")

    # 移除 JSON 自带的固定内存参数，改由用户配置
    jvm_args = [a for a in jvm_args if not (a.startswith("-Xmx") or a.startswith("-Xms"))]
    mem = [f"-Xms{int(cfg.get('mem_min', 1024))}M", f"-Xmx{int(cfg.get('mem_max', 4096))}M"]
    brand = ["-Dminecraft.launcher.brand=MC-Launcher", "-Dminecraft.launcher.version=1.0"]
    extra_jvm = []
    if "arguments" in v:
        if any(a == "-Djava.library.path=${natives_directory}" for a in jvm_args):
            jvm_args = [sub(a) for a in jvm_args]
        else:
            jvm_args.append(f"-Djava.library.path={natives_dir}")
        if cfg.get("jvm_extra"):
            extra_jvm = cfg["jvm_extra"].split()
    else:
        jvm_args = [sub(a) for a in jvm_args]
        if cfg.get("jvm_extra"):
            extra_jvm = cfg["jvm_extra"].split()
    # 新旧版本统一注入：日志配置 + log4j 漏洞防护（1.17 前）
    if log_cfg:
        jvm_args.append(f"-Dlog4j.configurationFile={log_cfg}")
    pv = parse_version_id(version_id)
    if pv and pv < (1, 17):
        jvm_args.append("-Dlog4j2.formatMsgNoLookups=true")

    cmd = [java_path] + jvm_args + brand + extra_jvm + mem + ["-cp", classpath, v["mainClass"]]

    if "arguments" in v:
        for entry in v["arguments"].get("game", []):
            if isinstance(entry, str):
                cmd.append(sub(entry))
            elif isinstance(entry, dict) and rules_allow(entry.get("rules"), features):
                for x in entry.get("value", []):
                    if isinstance(x, str):
                        cmd.append(sub(x))
    else:
        cmd += sub(v.get("minecraftArguments", "")).split()

    # 联机直连：--server/--port 参数（1.6+ 客户端支持）
    server = (cfg.get("server") or "").strip()
    if server:
        pv2 = parse_version_id(version_id)
        if pv2 and pv2 >= (1, 6):
            cmd += ["--server", server, "--port", str(int(cfg.get("server_port") or 25565))]
            log(f"  启动后将直连服务器：{server}:{int(cfg.get('server_port') or 25565)}")
        else:
            log("[提示] 该旧版本不支持直连参数，请进游戏后手动加入服务器")

    return java_path, major, cmd


def set_game_language(game_dir, lang):
    """设置游戏内语言（写入 options.txt 的 lang 项）。lang 如 'zh_cn'。返回 options.txt 路径。"""
    opt = os.path.join(game_dir, "options.txt")
    lines = []
    if os.path.exists(opt):
        try:
            with open(opt, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith("lang:"):
            lines[i] = "lang:" + lang
            found = True
            break
    if not found:
        lines.append("lang:" + lang)
    os.makedirs(game_dir, exist_ok=True)
    with open(opt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return opt


class GameProcess:
    """封装游戏子进程，实时回传输出。"""

    def __init__(self, cmd, cwd, line_cb=None, exit_cb=None):
        self.cmd = cmd
        self.cwd = cwd
        self.line_cb = line_cb
        self.exit_cb = exit_cb
        self.proc = None
        self._stop = threading.Event()

    def start(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=flags)
        except FileNotFoundError:
            raise RuntimeError("找不到 Java 可执行文件")
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        t2 = threading.Thread(target=self._waiter, daemon=True)
        t2.start()

    def _reader(self):
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                text = ""
            if text and self.line_cb:
                self.line_cb(text)

    def _waiter(self):
        code = self.proc.wait()
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        if self.exit_cb:
            self.exit_cb(code)

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=4)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None


# ============================================================ 命令行测试入口

def _cmd_manifest(args):
    m = load_manifest(force=args.force, source=args.source)
    vs = m["versions"]
    print(f"清单版本数：{len(vs)}，latest={m['latest']}")
    for t in ("release", "snapshot"):
        top = [x for x in vs if x["type"] == t][:3]
        print(f"  最新 {t}: " + ", ".join(x["id"] for x in top))


def _cmd_version(args):
    m = load_manifest(source=args.source)
    v = resolve_version(args.id, m, source=args.source)
    print(f"版本：{v.get('id')}  类型：{v.get('type')}  主类：{v.get('mainClass')}")
    print(f"Java 要求：{(v.get('javaVersion') or {}).get('majorVersion', 8)}")
    ai = v.get("assetIndex") or {}
    print(f"资源索引：{ai.get('id')}  virtual={ai.get('virtual')}  map_to_resources={ai.get('map_to_resources')}")
    print(f"库数量：{len(v.get('libraries') or [])}")
    inst = Installer(load_config(), log=lambda *a: None)
    cp, nat = inst._lib_downloads(v)
    print(f"  本机类路径库：{len(cp)}，原生库：{len(nat)}")
    if v.get("minecraftArguments"):
        print(f"旧版启动参数：{v['minecraftArguments'][:120]}…")
    else:
        print(f"新版游戏参数：{len(v['arguments']['game'])} 条 / JVM 参数：{len(v['arguments']['jvm'])} 条")
    if args.jvm:
        jvm = v.get("arguments", {}).get("jvm", [])
        for e in jvm:
            print("   ", json.dumps(e, ensure_ascii=False)[:200])


def _cmd_javas(args):
    js = list_javas()
    if not js:
        print("未检测到任何 Java")
        return
    for j in js:
        print(f"Java {j['major']}  [{j['source']}]  {j['path']}")


def _cmd_jre(args):
    path = download_jre(args.major, source=args.source, progress_cb=lambda d, t: print(f"\r  下载中 {d/1048576:.1f}/{t/1048576:.1f} MB", end="", flush=True))
    print()
    print(f"完成：{path}")


def _cmd_install(args):
    cfg = load_config()
    inst = Installer(cfg, progress={
        "step": lambda t: print(f"[步骤] {t}"),
        "bar": lambda d, t, txt: print(f"\r[{txt}] {d/1048576:.2f}/{t/1048576:.2f} MB", end="", flush=True),
    })
    try:
        r = inst.install(args.id, skip_assets=args.skip_assets)
        print()
        print("安装完成：", r["index_id"], "类路径库", len(r["cp"]))
    except Exception as e:
        print(f"\n安装失败：{e}")
        sys.exit(1)


def _cmd_dryrun(args):
    cfg = load_config()
    acc = offline_account(args.name or "Player")
    if cfg.get("accounts"):
        acc = cfg["accounts"][cfg.get("active_account", 0)]
    m = load_manifest(source=args.source)
    try:
        java, major, cmd = build_command(args.id, acc, cfg, m)
    except Exception as e:
        print(f"构建失败：{e}")
        sys.exit(1)
    print(f"Java({major})：{java}")
    print("命令：")
    print("  " + " ".join(f'"{c}"' if " " in c else c for c in cmd))


def _cmd_offline(args):
    a = offline_account(args.name)
    print(f"昵称：{a['name']}\nUUID：{a['uuid']}")


def _cmd_skin_server(args):
    cfg = load_config()
    accs = cfg.get("accounts") or [offline_account("Steve")]
    skins_dir = os.path.join(cfg.get("game_dir") or DEFAULT_GAME_DIR, "skins")
    srv = SkinServer(accs, skins_dir)
    port = srv.start()
    print(f"皮肤服务器已启动：http://127.0.0.1:{port}")
    nodash = (accs[0].get("uuid") or "").replace("-", "")
    data = http_get_json(f"http://127.0.0.1:{port}/sessionserver/session/minecraft/profile/{nodash}")
    print("profile 自测：", json.dumps(data, ensure_ascii=False)[:260])
    print("按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


def main():
    ap = argparse.ArgumentParser(description="MC 启动器核心（命令行测试用）")
    ap.add_argument("--source", default="auto", choices=["auto", "official", "mirror"])
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("manifest"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=_cmd_manifest)
    p = sub.add_parser("version"); p.add_argument("id"); p.add_argument("--jvm", action="store_true"); p.set_defaults(fn=_cmd_version)
    p = sub.add_parser("javas"); p.set_defaults(fn=_cmd_javas)
    p = sub.add_parser("download-jre"); p.add_argument("major", type=int); p.set_defaults(fn=_cmd_jre)
    p = sub.add_parser("install"); p.add_argument("id"); p.add_argument("--skip-assets", action="store_true"); p.set_defaults(fn=_cmd_install)
    p = sub.add_parser("dryrun"); p.add_argument("id"); p.add_argument("--name", default=""); p.set_defaults(fn=_cmd_dryrun)
    p = sub.add_parser("offline-login"); p.add_argument("name"); p.set_defaults(fn=_cmd_offline)
    p = sub.add_parser("skin-server"); p.set_defaults(fn=_cmd_skin_server)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()

# --EOF--
