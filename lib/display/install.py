"""display --install chrome: a headless Chrome on Linux without sudo when this machine has what it needs.

Playwright's headless shell goes in ~/.cache/ms-playwright (where tools.chrome_bin looks), fetched by the Node driver
inside Playwright's wheel from PyPI (checked against PyPI's sha256): no venv, no pip, no python3-venv. Only when the
shell is missing system libraries (ldd says which) is root needed. Run as root, it says so and installs them in the
open: Playwright's install-deps on Debian/Ubuntu, the system's Chromium (which brings the same libraries) elsewhere.
Otherwise the line to run with sudo is said first: on Debian/Ubuntu the apt command Playwright would run, caught
with a stand-in sudo that only writes it down; elsewhere the Chromium one. From a terminal it asks whether to run
it now ([y/N]); without one the exit status is 1, for the person to run it.
"""
import hashlib
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import zipfile

from . import tools
from .tools import Fail

PLAYWRIGHT = "1.64.1"
WHEEL = {"x86_64": "manylinux1_x86_64", "aarch64": "manylinux_2_17_aarch64"}
TARGET = "chromium-headless-shell"
# where the libraries come from when they are missing, on the systems Playwright's install-deps does not know
OTHER = {"fedora": "sudo dnf install -y chromium", "arch": "sudo pacman -S --needed --noconfirm chromium"}


def os_like():
    """debian, fedora, arch or "" (as the engine's plugin_steps.os_name says it)."""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            info = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return ""
    words = ("%s %s" % (info.get("ID", ""), info.get("ID_LIKE", ""))).replace('"', "").split()
    for name, like in (("debian", ("debian", "ubuntu")), ("fedora", ("fedora", "rhel")), ("arch", ("arch",))):
        if any(w in like for w in words):
            return name
    return ""


def missing_libs(path):
    """The shared libraries `path` needs and this machine lacks (ldd's "not found"); [] when ldd is not there."""
    try:
        r = subprocess.run(["ldd", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({l.split("=>")[0].strip() for l in r.stdout.splitlines() if "not found" in l})


def named(libs, most=6):
    """The libraries by name, the first few when there are many."""
    return "、".join(libs) if len(libs) <= most else "%s 等" % "、".join(libs[:most])


def fetch(url, timeout=300):
    try:
        return tools.download(url, timeout)
    except (OSError, ValueError) as e:  # URLError is an OSError
        raise Fail("抓不到 PyPI：%s。連得上網路後再跑一次 %s --install chrome" % (getattr(e, "reason", e), tools.BIN))


def driver(tmp):
    """Playwright's Node driver from its wheel, unpacked in tmp: (node, cli.js)."""
    machine = platform.machine()
    tag = WHEEL.get(machine)
    if not tag:
        raise Fail("Playwright 沒有給 %s 這種 CPU 的 Linux 版：自己裝 Chromium 放在 PATH 上，或設 DISPLAY_CHROME" % machine)
    info = json.loads(fetch("https://pypi.org/pypi/playwright/%s/json" % PLAYWRIGHT, 60))
    url = next((u for u in info["urls"] if "-py3-none-%s" % tag in u["filename"]), None)
    if not url:
        raise Fail("PyPI 上找不到 playwright %s 的 %s 版" % (PLAYWRIGHT, tag))
    tools.say("下載 Playwright %s（約 %d MB）…" % (PLAYWRIGHT, url["size"] // 1000000))
    blob = fetch(url["url"])
    if hashlib.sha256(blob).hexdigest() != url["digests"]["sha256"]:
        raise Fail("下載的 Playwright 跟 PyPI 記的 sha256 不合：沒有用它")
    whl = os.path.join(tmp, "playwright.whl")
    with open(whl, "wb") as f:
        f.write(blob)
    out = os.path.join(tmp, "w")
    with zipfile.ZipFile(whl) as z:
        for n in z.namelist():
            if n.startswith("playwright/driver/") and ".." not in n.split("/") and not n.startswith("/"):
                z.extract(n, out)
    node = os.path.join(out, "playwright", "driver", "node")
    os.chmod(node, 0o755)
    return node, os.path.join(out, "playwright", "driver", "package", "cli.js")


def apt_command(node, cli, tmp):
    """The command Playwright's install-deps would run as root, caught by a sudo that only writes it down."""
    stub = os.path.join(tmp, "stub")
    os.makedirs(stub)
    said = os.path.join(tmp, "said")
    with open(os.path.join(stub, "sudo"), "w") as f:
        f.write('#!/bin/sh\nprintf "%%s\\n" "$@" > %s\nexit 1\n' % shlex.quote(said))
    os.chmod(os.path.join(stub, "sudo"), 0o755)
    env = dict(os.environ, PATH=stub + os.pathsep + os.environ.get("PATH", ""))
    subprocess.run([node, cli, "install-deps", TARGET], env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    try:
        with open(said, encoding="utf-8") as f:
            args = f.read().splitlines()
    except OSError:
        return None
    # sudo -- sh -c "apt-get update&& apt-get install -y --no-install-recommends …"
    if len(args) == 4 and args[:3] == ["--", "sh", "-c"] and args[3].startswith("apt-get update"):
        return args[3].replace("&& ", " && ")
    return None


def chrome():
    """0 when this machine has a headless Chrome that has its libraries; 1 when the person has to run a sudo line."""
    if platform.system() != "Linux":
        raise Fail("--install chrome 只管 Linux；Mac：brew install --cask google-chrome")
    have = tools.chrome_bin()
    if have and not missing_libs(have):
        print("chrome: 已經有了（%s）" % have)
        return 0
    with tempfile.TemporaryDirectory(prefix="display-install-") as tmp:  # 0700, ours only
        node, cli = driver(tmp)
        if not have:
            tools.say("下載 Chrome 的 headless shell 到 ~/.cache/ms-playwright（不用 sudo）…")
            r = subprocess.run([node, cli, "install", TARGET], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=1200)
            have = tools.chrome_bin()
            if r.returncode != 0 or not have:
                raise Fail("Chrome 下載失敗：%s" % r.stdout.strip()[-400:])
        lacking = missing_libs(have)
        if not lacking:
            print("chrome: 裝好了（%s），沒有用到 sudo" % have)
            return 0
        system = os_like()
        print("chrome: Chrome 下載好了（%s），但這台缺它要的 %d 個系統函式庫：%s" % (have, len(lacking), named(lacking)))
        if os.geteuid() == 0:
            if system == "debian":
                print("這裡是 root：用 Playwright 的 install-deps 裝這些")
                return finish([node, cli, "install-deps", TARGET], have)
            if system in OTHER:
                print("這裡是 root：用 %s 裝這些" % OTHER[system][len("sudo "):])
                return finish(shlex.split(OTHER[system])[1:], have)
            return unknown()
        cmd = apt_command(node, cli, tmp) if system == "debian" else None
    shown = ("sudo sh -c %s" % shlex.quote(cmd)) if cmd else OTHER.get(system)
    if not shown:
        return unknown()
    print("系統函式庫要用 root 裝，所以這一步要 sudo：\n  %s" % shown)
    again = "%s --check chrome" % tools.BIN
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("這裡沒有終端機可以輸入密碼：請在自己的終端機跑上面那行，跑完再跑一次 %s" % again)
        return 1
    try:
        yes = input("要現在用 sudo 跑嗎？[y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        yes = False
    if not yes:
        print("沒有跑：要裝時自己跑上面那行，跑完再跑一次 %s" % again)
        return 1
    return finish(["sudo", "--"] + (["sh", "-c", cmd] if cmd else shlex.split(shown)[1:]), have)


def unknown():
    print("系統函式庫要用 root 裝。這個系統我不知道套件名稱：照上面缺的函式庫，用系統的套件管理裝上（要 sudo）")
    return 1


def finish(run, have):
    """Run the line that installs the libraries, its output shown as it goes; then whether the shell has them."""
    sys.stdout.flush()  # what was said comes before its output, into a pipe too (#setup)
    if subprocess.run(run).returncode != 0:  # no-timeout: sudo waits for the person to type a password
        print("chrome: 沒裝成（上面是它說的）")
        return 1
    lacking = missing_libs(have)
    if lacking:
        print("chrome: 裝完還是缺：%s" % named(lacking))
        return 1
    print("chrome: 裝好了（%s）" % have)
    return 0
