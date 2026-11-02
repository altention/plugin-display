"""The tools display renders with, where they live (~/.cache/altention-display) and getting them (`display --setup`).

The browser libraries are npm packages, fetched as their tarballs and checked against the registry's sha512; Tectonic
is a single binary from its GitHub release, checked against a sha256 written here; matplotlib goes in a venv of our
own. Nothing needs sudo, node or npm.
"""
import base64
import glob
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

TOOLS = os.environ.get("DISPLAY_TOOLS") or os.path.expanduser("~/.cache/altention-display")
OUT = os.path.join(TOOLS, "out")

# name: (version, sha512 of the npm tarball, the files to keep from package/)
NPM = {
    "katex": ("0.19.0", "v6Tznz3zJ7u3niRCoDTsumM2+HA2XXcCu+WAacCeHD2z3p9A9Ks987o5FzfTGBN0e8A0vjEgIDvLbICrXpdw/Q==",
              ["dist/"]),
    # 12.x sizes every flowchart label at 120px here (a circle around "z" comes out huge); 11.17 measures them
    "mermaid": ("11.17.2", "V6K3C8EBdEsPFZXSKMJe6ppQOENxuHARr9GvHX4hh47lAbhMRD9qf4oEK7LoaRQxULMa80/qt5gHO73aCleBBg==",
                ["dist/mermaid.min.js"]),
    "@viz-js/viz": ("3.31.0", "r7zlQdRvcwvIpjHGgs+KNWHeE/P5/Dq7k8ZAKaMCbZqomxCBJV78gVuQYaHzFVca+kB0mX0fMW9UFevCOBG50A==",
                    ["dist/viz-global.js"]),
    "vega": ("6.4.0", "rHXCq2LpEWCLMjXBEj7hocY9G0/i5TGYRayGGSj06VTF4+l2IG2Z7oeZKCjWbUh/7hTE/mVhbaOPB72wCElh6g==",
             ["build/vega.min.js"]),
    "vega-lite": ("6.4.3", "d/7hPjfz560UERaQuTmGgIVfXAe3g2hJWeC+igDeaGohUdEoNrHLXgR/yTOBT8vV/lIuuKnw+0/xWWblkDwkMQ==",
                  ["build/vega-lite.min.js"]),
    "vega-embed": ("7.3.0", "wQ8s5GaSzqoKgmRfbRolgBrjE2CP7ZKHXqH7NijxMqPjpdWxSs4Dn8tvp9g0giQvh2cK+3p56sxjWWM0wNngGw==",
                   ["build/vega-embed.min.js"]),
    "markdown-it": ("14.3.2", "sHHjZ5fJKlgrG4qns2YwVcdNep35h5fERrfkD2YNsb9UFk0UIHarbiTaHKVMlPuWAoiilyK8Fv/jAm11slsY7Q==",
                    ["dist/markdown-it.min.js"]),
    "diff2html": ("3.4.56", "u9gfn+BlbHcyO7vItCIC4z49LJDUt31tODzOfAuJ5R1E7IdlRL6KjugcB9zOpejD+XiR+dDZbsnHSQ3g6A/u8A==",
                  ["bundles/js/diff2html.min.js", "bundles/css/diff2html.min.css"]),
}

TECTONIC = {"version": "0.17.0",
            "url": "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/"
                   "tectonic-0.17.0-{target}.tar.gz",
            "sha256": {"aarch64-unknown-linux-musl": "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8",
                       "x86_64-unknown-linux-musl": "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
                       "aarch64-apple-darwin": "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667",
                       "x86_64-apple-darwin": "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80"}}

# what each kind of picture needs; "chrome" is found, never fetched
NEEDS = {"doc": ["chrome", "katex", "markdown-it", "mermaid"], "chart": ["chrome", "vega", "vega-lite", "vega-embed"],
         "diagram": ["chrome", "mermaid"], "graph": ["chrome", "@viz-js/viz"], "diff": ["chrome", "diff2html"],
         "term": ["chrome"], "image": ["chrome"], "pdf": ["pdftoppm"], "tex": ["tectonic", "pdftoppm", "sandbox"],
         "plot": ["matplotlib"]}
SETUP_GROUPS = {"browser": sorted(NPM), "tectonic": ["tectonic"], "plot": ["matplotlib"]}
# what this machine has to have, not fetched here (plugin.json's needs[], each checked by display --check <name>):
# (what it is for, how to get it)
SYSTEM = {
    "chrome": ("公式、表格、圖表、流程圖、差異、終端機、SVG／HTML 這幾種",
               "裝 Chrome 或 Chromium 放在 PATH 上，或把 DISPLAY_CHROME 設成它的路徑（寫在 ~/.zshenv 這種非互動 shell "
               "也讀得到的地方）；各系統的裝法見 README 的「需求」。Ubuntu 用 apt 裝的 chromium 是 snap，不能用"),
    "poppler": ("pdf、tex 兩種",
                "裝 poppler（有 pdftoppm、pdfinfo 這兩個指令）。Debian／Ubuntu：sudo apt install poppler-utils；"
                "Mac：brew install poppler"),
    "cjk-font": ("圖裡的中文",
                 "裝一套中文字型。Debian／Ubuntu：sudo apt install fonts-noto-cjk；"
                 "Mac 本來就有"),
    "venv": ("plot 這種（matplotlib 裝在自己的 venv）",
             "Python 要能建 venv。Debian／Ubuntu：sudo apt install python3-venv；"
             "Mac 本來就有"),
}


class Fail(Exception):
    """Something the caller (a Claude) can act on: said on stderr, exit 2."""


def say(text):
    sys.stderr.write("display: %s\n" % text)


def npm_dir(name):
    version = NPM[name][0]
    d = os.path.join(TOOLS, "npm", "%s-%s" % (name.replace("/", "+"), version))
    return d if os.path.isfile(os.path.join(d, ".complete")) else None


def asset(name, path):
    """file:// URL of one of a package's files (a path under package/)."""
    d = npm_dir(name)
    if not d:
        raise Fail("%s 沒有裝：先跑 display --setup" % name)
    return "file://" + os.path.join(d, path)


def tectonic_target():
    arch = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine().lower())
    system = {"Linux": "unknown-linux-musl", "Darwin": "apple-darwin"}.get(platform.system())
    return "%s-%s" % (arch, system) if arch and system else None


def tectonic_bin():
    if shutil.which("tectonic"):
        return shutil.which("tectonic")
    t = tectonic_target()
    path = os.path.join(TOOLS, "tectonic-%s-%s" % (TECTONIC["version"], t), "tectonic") if t else None
    return path if path and os.access(path, os.X_OK) else None


def tectonic_cache():
    """Where Tectonic keeps the TeX packages it downloads (its own default, passed to it as TECTONIC_CACHE_DIR)."""
    if os.environ.get("TECTONIC_CACHE_DIR"):
        return os.environ["TECTONIC_CACHE_DIR"]
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Caches/Tectonic")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "tectonic")


def venv_python():
    path = os.path.join(TOOLS, "venv", "bin", "python")
    return path if os.access(path, os.X_OK) and os.path.isfile(os.path.join(TOOLS, "venv", ".complete")) else None


def chrome_bin():
    """A headless-capable Chrome: $DISPLAY_CHROME, Playwright's headless shell or Chromium, one on PATH, macOS's."""
    env = os.environ.get("DISPLAY_CHROME")
    if env:
        return env if os.access(env, os.X_OK) else None
    pw = os.path.expanduser("~/.cache/ms-playwright")
    for pattern in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
                    "chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        hits = sorted(glob.glob(os.path.join(pw, pattern)))
        if hits:
            return hits[-1]
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        if shutil.which(name):
            return shutil.which(name)
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    return mac if os.access(mac, os.X_OK) else None


FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts")
# only the names sure to have Chinese characters (IPA's Japanese fonts have the kanji); any other is not guessed
CJK_FILE = re.compile(r"noto\w*cjk|sourcehan|wqy|(^|[-_])ipa(ex)?[gmp]", re.I)


def cjk_font():
    """Whether there is a font with Chinese in it (macOS always has one); without fc-list, by the font files'
    names, only the sure ones: None when there are fonts but none of those (this machine cannot tell)."""
    if platform.system() == "Darwin":
        return True
    try:
        r = subprocess.run(["fc-list", ":lang=zh", "family"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10)
        return bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    files = [f for d in FONT_DIRS for _, _, fs in os.walk(os.path.expanduser(d)) for f in fs
             if f.lower().endswith((".ttf", ".otf", ".ttc", ".otc"))]
    if any(CJK_FILE.search(f) for f in files):
        return True
    return None if files else False


def venv_ok():
    """Whether a venv with pip can be made for plot (Debian leaves ensurepip to python3-venv), or one is there."""
    import importlib.util
    return bool(venv_python() or importlib.util.find_spec("ensurepip"))


def have(need):
    if need == "chrome":
        return bool(chrome_bin())
    if need == "poppler":
        return have("pdftoppm")
    if need == "cjk-font":
        return cjk_font()
    if need == "venv":
        return venv_ok()
    if need == "tectonic":
        return bool(tectonic_bin())
    if need == "pdftoppm":
        return bool(shutil.which("pdftoppm") and shutil.which("pdfinfo"))
    if need == "matplotlib":
        return bool(venv_python())
    if need == "sandbox":
        from . import confine
        return bool(confine.how())
    return bool(npm_dir(need))


def missing(kind):
    return [n for n in NEEDS[kind] if not have(n)]


BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin", "display")
LAZY = ("tectonic", "plot")  # plugin.json's setup items install leaves for the first time they are needed


def how_to_get(need):
    """What to do for a missing need, said so a Claude can do it: the setup item to run, or the system's hint."""
    need = {"pdftoppm": "poppler"}.get(need, need)
    if need in SYSTEM:
        return SYSTEM[need][1]
    group = next(g for g, names in SETUP_GROUPS.items() if need in names)
    return "跑 %s --setup %s（plugin.json 的 setup「%s」%s）" % (
        BIN, group, group, "；安裝時不裝，第一次用到才裝" if group in LAZY else "")


def missing_said(kind, need):
    """The error for a kind this machine cannot draw yet: each remedy once, with what it brings."""
    by = {}
    for n in need:
        by.setdefault(how_to_get(n), []).append(n)
    return "這台畫 %s 還缺（display --check 看全部）：\n%s" % (
        kind, "\n".join("- %s：%s" % ("、".join(ns), how) for how, ns in by.items()))


# ---------- getting them ----------

def download(url, timeout=300):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def unpack(blob, dest, keep=None):
    """Extract a .tar.gz held in memory into dest: plain files and folders only, nothing outside dest; with `keep`
    (paths under package/, a trailing / for a folder), only those, the package/ prefix taken off."""
    tmp = dest + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with tempfile.TemporaryFile() as f:
        f.write(blob)
        f.seek(0)
        with tarfile.open(fileobj=f, mode="r:gz") as tar:
            for m in tar.getmembers():
                name = m.name
                if keep is not None:
                    if not name.startswith("package/"):
                        continue
                    name = name[len("package/"):]
                    if not any(name == k or (k.endswith("/") and name.startswith(k)) for k in keep):
                        continue
                if not name or name.startswith("/") or ".." in name.split("/") or not (m.isfile() or m.isdir()):
                    continue
                target = os.path.join(tmp, name)
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)
    open(os.path.join(tmp, ".complete"), "w").close()
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)


def setup_npm(name):
    version, sha512, keep = NPM[name]
    if npm_dir(name):
        return
    say("下載 %s %s…" % (name, version))
    base = name.split("/")[-1]
    blob = download("https://registry.npmjs.org/%s/-/%s-%s.tgz" % (name, base, version))
    if base64.b64encode(hashlib.sha512(blob).digest()).decode() != sha512:
        raise Fail("%s 的檔案校驗不符，沒有裝" % name)
    unpack(blob, os.path.join(TOOLS, "npm", "%s-%s" % (name.replace("/", "+"), version)), keep)


def setup_tectonic():
    if tectonic_bin():
        return
    t = tectonic_target()
    if t not in TECTONIC["sha256"]:
        raise Fail("Tectonic 沒有這台（%s %s）能用的版本" % (platform.system(), platform.machine()))
    say("下載 Tectonic %s（%s）…" % (TECTONIC["version"], t))
    blob = download(TECTONIC["url"].format(target=t))
    if hashlib.sha256(blob).hexdigest() != TECTONIC["sha256"][t]:
        raise Fail("Tectonic 的檔案校驗不符，沒有裝")
    unpack(blob, os.path.join(TOOLS, "tectonic-%s-%s" % (TECTONIC["version"], t)))


def setup_matplotlib():
    if venv_python():
        return
    say("建 matplotlib 的 venv…")
    venv = os.path.join(TOOLS, "venv")
    shutil.rmtree(venv, ignore_errors=True)
    for cmd in ([sys.executable, "-m", "venv", venv],
                [os.path.join(venv, "bin", "python"), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                 "matplotlib>=3.7,<4", "numpy"]):
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        if r.returncode != 0:
            raise Fail("matplotlib 裝不起來：%s" % r.stdout.decode("utf-8", "replace").strip()[-400:])
    open(os.path.join(venv, ".complete"), "w").close()


def setup(groups):
    os.makedirs(TOOLS, exist_ok=True)
    for g in groups:
        for name in SETUP_GROUPS[g]:
            if name == "tectonic":
                setup_tectonic()
            elif name == "matplotlib":
                setup_matplotlib()
            else:
                setup_npm(name)


CANNOT = 3  # a check's exit status for "this machine cannot tell" (the engine's checks.md); older engines: not there


def check_one(name):
    """display --check <name>: plugin.json's check of one thing this machine has to have. Exit 0 when it is there,
    CANNOT when it cannot be told here (said too, for an engine that takes 3 as not there), else 1."""
    ok = cjk_font() if name == "cjk-font" else have(name)
    if ok is None:
        print("%s: 查不了（這台沒有 fc-list，字型檔的名字也看不出來；%s要用；%s）" % ((name,) + SYSTEM[name]))
        return CANNOT
    print("%s: %s" % (name, "有" if ok else "沒有（%s要用；%s）" % SYSTEM[name]))
    return 0 if ok else 1


def check():
    print("工具資料夾: %s" % TOOLS)
    print("chrome:     %s" % (chrome_bin() or "沒有（裝 Chrome／Chromium，或設 DISPLAY_CHROME）"))
    for name in sorted(NPM):
        print("%-11s %s" % (name + ":", npm_dir(name) or "沒有（display --setup）"))
    print("tectonic:   %s" % (tectonic_bin() or "沒有（display --setup tectonic）"))
    print("matplotlib: %s" % (venv_python() or "沒有（display --setup plot）"))
    from . import confine
    print("sandbox:    %s" % (confine.how() or "沒有（Linux 要 Landlock、macOS 要 sandbox-exec；沒有就不畫 tex）"))
    print("pdftoppm:   %s" % (shutil.which("pdftoppm") or "沒有（poppler-utils／brew install poppler；pdf、tex 要）"))
    cjk = cjk_font()
    print("中文字型:   %s" % ("有" if cjk else "查不了（沒有 fc-list，字型檔的名字看不出來）" if cjk is None else "沒有（%s）" % SYSTEM["cjk-font"][1]))
    print("venv:       %s" % ("有" if venv_ok() else "沒有（%s）" % SYSTEM["venv"][1]))
    print("可用的種類: %s" % ("、".join(k for k in NEEDS if not missing(k)) or "沒有"))
