"""Running Tectonic so that it reads and writes only its own folders: Landlock on Linux, sandbox-exec on macOS.

A picture of a LaTeX document is posted to Discord, so a document that does \\input{~/.config/…} must not get the
file into it. Tectonic's --untrusted turns off shell escape but not reading files by absolute path, ../ or a
symlink; the kernel does that here. The command can still use the network (the first compile downloads TeX
packages) and read what any program needs: system libraries, fonts, certificates, DNS settings. Where neither is
there, `how()` is None and tex is not drawn at all.
"""
import ctypes
import ctypes.util
import os
import platform
import shutil
import subprocess

from .tools import Fail

WHY = ("這台沒辦法把 LaTeX 關在自己的資料夾裡（Linux 要 kernel 5.13 以上開著 Landlock，macOS 要 sandbox-exec），"
       "所以不畫 tex：tex 讀得到這台的檔案，會被排進貼出去的圖裡")

# Landlock (linux/landlock.h): the same syscall numbers on every architecture
CREATE_RULESET, ADD_RULE, RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
TRUNCATE, IOCTL_DEV = 1 << 14, 1 << 15
READ = EXECUTE | READ_FILE | READ_DIR
ON_FILES = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV  # the rights a rule on a file (not a folder) may have

# what any program reads: libraries, fonts and their settings, certificates and DNS for the downloads, and a few
# devices one by one (not all of /dev: /dev/shm holds other programs' files, /dev/pts the terminals)
DEVICES = ["/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"]
SYSTEM_LINUX = ["/usr/lib", "/usr/lib64", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/fonts", "/usr/share/fonts",
                "/usr/share/fontconfig", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts",
                "~/.cache/fontconfig", "/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/usr/share/ca-certificates",
                "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/gai.conf", "/etc/localtime"] + DEVICES
SYSTEM_MAC = ["/System", "/usr/lib", "/usr/share", "/Library/Fonts", "/Library/Preferences", "/private/etc",
              "/private/var/db/timezone", "~/Library/Fonts"] + DEVICES


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


def landlock_abi():
    if platform.system() != "Linux":
        return 0
    try:
        abi = _libc().syscall(CREATE_RULESET, None, ctypes.c_size_t(0), CREATE_RULESET_VERSION)
    except (OSError, AttributeError):
        return 0
    return max(abi, 0)


def how():
    """'landlock', 'sandbox-exec' or None: how this machine keeps a command to its folders."""
    if landlock_abi() >= 1:
        return "landlock"
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec"
    return None


def _all_fs(abi):
    # every file-system right this ABI knows: 13 in v1, then REFER (v2), TRUNCATE (v3), IOCTL_DEV (v5)
    return (1 << {1: 13, 2: 14, 3: 15, 4: 15}.get(abi, 16)) - 1


class _Attr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _landlock(rw, ro):
    """Called in the child before exec: from here on it (and what it runs) can reach only rw and ro. Any failure
    raises, and subprocess then does not run the command."""
    libc, abi = _libc(), landlock_abi()
    every = _all_fs(abi)
    attr = _Attr(every)
    fd = libc.syscall(CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    for paths, rights in ((rw, every), (ro, READ)):
        for path in paths:
            try:
                pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                allowed = rights if os.path.isdir(path) else rights & ON_FILES
                rule = _Beneath(allowed & every, pfd)
                if libc.syscall(ADD_RULE, fd, RULE_PATH_BENEATH, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "landlock_add_rule " + path)
            finally:
                os.close(pfd)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl")
    if libc.syscall(RESTRICT_SELF, fd, 0) != 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    os.close(fd)


def _sbpl_string(path):
    return '"%s"' % path.replace("\\", "\\\\").replace('"', '\\"')


def mac_profile(rw, ro):
    """A sandbox-exec profile: nothing may be read or written but rw and ro (and the names of files, which dyld
    needs to find its way)."""
    lines = ["(version 1)", "(allow default)", "(deny file-read* file-write*)", "(allow file-read-metadata)",
             '(allow file-read* (literal "/"))']
    kind = lambda p: "subpath" if os.path.isdir(p) else "literal"  # noqa: E731
    lines += ["(allow file-read* (%s %s))" % (kind(p), _sbpl_string(p)) for p in ro]
    lines += ["(allow file-read* file-write* (%s %s))" % (kind(p), _sbpl_string(p)) for p in rw]
    lines.append('(allow file-write* (literal "/dev/null"))')
    # a folder is (subpath …), a single file (literal …): neither lets a rule on /dev/null reach the rest of /dev
    return "\n".join(lines) + "\n"


def run(cmd, rw, ro, **kw):
    """subprocess.run(cmd) where cmd can read and write only under `rw` and read only under `ro` (plus what every
    program reads). Fail when this machine cannot do that: the command is never run unconfined."""
    way = how()
    if not way:
        raise Fail(WHY)
    system = SYSTEM_MAC if way == "sandbox-exec" else SYSTEM_LINUX
    # rules are on the real paths: a symlink is followed by the kernel before the rule is checked
    real = lambda paths: [os.path.realpath(os.path.expanduser(p)) for p in paths]  # noqa: E731
    rw, ro = real(rw), real(list(ro) + system)
    if way == "sandbox-exec":
        return subprocess.run(["sandbox-exec", "-p", mac_profile(rw, ro)] + list(cmd), **kw)
    return subprocess.run(cmd, preexec_fn=lambda: _landlock(rw, ro), **kw)
