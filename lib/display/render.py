"""A page in a headless Chrome, as PNGs: the page every browser kind builds, and taking its picture.

Every page puts what it shows in <div id="c"> and calls D.finish() once it is drawn (or D.fail(message)); its
children are the blocks it may be cut between, unless it sets D.blocks ([[top, bottom], …]) first. D.note is said
on stderr (the picture is still made). Chrome runs
twice: --dump-dom lays the page out and reads back the card's size, its blocks and any errors; --screenshot then takes
a window exactly that size. A card taller than `max_height` is cut between its blocks into several pictures, each
one the same page shifted up (#from=…) in a window as tall as its part.
"""
import ctypes
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .tools import Fail, chrome_bin, say

THEMES = {"dark": {"bg": "#2b2d31", "fg": "#dbdee1", "muted": "#949ba4", "line": "#4e5058", "code": "#1e1f22",
                   "accent": "#00a8fc"},
          "light": {"bg": "#ffffff", "fg": "#1a1a1a", "muted": "#5c5e66", "line": "#d4d7dc", "code": "#f2f3f5",
                    "accent": "#006ce7"}}
# text cards (doc) keep the serif look of the math; charts and diagrams are sans
SERIF = ["KaTeX_Main", "Noto Serif CJK TC", "Source Han Serif TC", "Songti TC", "AR PL UMing TW", "serif"]
SANS = ["Helvetica Neue", "Lato", "Liberation Sans", "DejaVu Sans", "PingFang TC", "Noto Sans CJK TC",
        "Source Han Sans TC", "Microsoft JhengHei", "Droid Sans Fallback", "AR PL UMing TW", "sans-serif"]
MONO = '"DejaVu Sans Mono", Menlo, Consolas, "Liberation Mono", "Noto Sans Mono", monospace'


def font_stack(fonts=SANS):
    return ", ".join(f if f in ("serif", "sans-serif") else '"%s"' % f for f in fonts)


PROTOCOL = r"""
var D = {errors: [], params: {}, done: false};
location.hash.slice(1).split("&").forEach(function (kv) {
  var i = kv.indexOf("=");
  if (i > 0) D.params[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
});
var C = document.getElementById("c");
if (D.params.w) { C.style.width = D.params.w + "px"; C.style.maxWidth = "none"; }
D.finish = function () {
  if (D.done) return;
  D.done = true;
  var go = function () {
    if (D.check) D.check();
    var r = C.getBoundingClientRect(), blocks = D.blocks || [];
    for (var i = 0; i < (D.blocks ? 0 : C.children.length); i++) {
      var b = C.children[i].getBoundingClientRect(), top = Math.floor(b.top - r.top);
      // a heading goes with what follows it: never the last thing on a picture
      while (/^H[1-6]$/.test(C.children[i].tagName) && i + 1 < C.children.length) b = C.children[++i].getBoundingClientRect();
      blocks.push([top, Math.ceil(b.bottom - r.top)]);
    }
    // a page of several pictures (display --segments): each one's [top, width, height]
    if (D.segments) document.body.setAttribute("data-segments", JSON.stringify(D.segments().map(function (e) {
      var b = e.getBoundingClientRect();  // what is inside may be wider than the card says: take all of it
      return [Math.floor(b.top - r.top), Math.ceil(Math.max(b.width, e.scrollWidth)), Math.ceil(b.height)];
    })));
    document.body.setAttribute("data-size", Math.ceil(r.width) + "x" + Math.ceil(r.height));
    document.body.setAttribute("data-blocks", JSON.stringify(blocks));
    document.body.setAttribute("data-errors", JSON.stringify(D.errors));
    if (D.note) document.body.setAttribute("data-note", D.note);
    if (D.params.from) C.style.marginTop = -Number(D.params.from) + "px";
  };
  if (document.fonts) document.fonts.ready.then(go); else go();
};
D.fail = function (message) { D.errors.push(String(message)); D.finish(); };
window.addEventListener("error", function (e) { D.fail(e.message || e); });
window.addEventListener("unhandledrejection", function (e) { D.fail(e.reason && e.reason.message || e.reason); });
"""


def page(body, theme, css="", head="", script="", width=None, font=None, mark=""):
    """The whole HTML page: `head` (links and scripts), `css` for this kind, `body` inside #c, then `script`;
    `mark` on <body>, for shoot to make sure it is still our page it takes a picture of."""
    t = THEMES[theme]
    base = """
html, body { margin: 0; background: %(bg)s; color: %(fg)s; }
body { overflow: hidden; }
#c { display: inline-block; box-sizing: border-box; padding: 22px 26px; %(maxw)s
     font: 17px/1.55 %(font)s; overflow-wrap: break-word; }
""" % dict(t, maxw="max-width: %dpx;" % width if width else "", font=font or font_stack())
    return ('<!doctype html><html><head><meta charset="utf-8">%s<style>%s%s</style></head>'
            '<body data-mark="%s"><div id="c">%s</div><script>%s</script><script>%s</script></body></html>'
            % (head, base, css, html.escape(mark, quote=True), body, PROTOCOL, script))


def _die_with_us():
    """In Chrome, before it starts (Linux): killed when display is, so a caller that kills display at its timeout
    (the bot's hook) leaves no Chrome behind. Chrome's own helpers go when it does."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
    except (OSError, AttributeError):
        pass


# the temporary folders display makes (TemporaryDirectory(prefix="display-…"): eight characters after it)
OURS = re.compile(r"^display-(chrome-|pdf-|fetch-)?[a-z0-9_]{8}$")


def _old_temps():
    """Chrome profiles and pages left by a display that was killed (its TemporaryDirectory never cleaned up): gone
    after an hour."""
    cut = time.time() - 3600
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "display-*")):
        try:
            if OURS.match(os.path.basename(d)) and os.path.isdir(d) and not os.path.islink(d) \
                    and os.path.getmtime(d) < cut:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def run_chrome(args, timeout=60):
    _old_temps()
    chrome = chrome_bin()
    if not chrome:
        raise Fail("這台找不到 Chrome／Chromium（display --check）")
    base = [chrome, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--mute-audio",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--virtual-time-budget=8000"]
    with tempfile.TemporaryDirectory(prefix="display-chrome-") as profile:
        cmd = base + ["--user-data-dir=" + profile] + args
        kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                  preexec_fn=_die_with_us if sys.platform.startswith("linux") else None)
        r = subprocess.run(cmd, **kw)
        if r.returncode != 0 and b"sandbox" in r.stderr.lower():  # no usable sandbox here (containers, NFS homes)
            r = subprocess.run(cmd[:1] + ["--no-sandbox"] + cmd[1:], **kw)
    return r


def cuts(blocks, height, max_height):
    """[(from, to)] parts of a card `height` tall, each at most about max_height, cut halfway between two blocks
    (a single block taller than that stays whole)."""
    if not max_height or height <= max_height or len(blocks) < 2:
        return [(0, height)]
    parts, start = [], 0
    for (_, bottom), (top, next_bottom) in zip(blocks, blocks[1:]):
        if next_bottom - start > max_height:
            cut = (bottom + top) // 2
            if cut > start:
                parts.append((start, cut))
                start = cut
    parts.append((start, height))
    return parts


def shoot(html_text, out, scale=2.0, max_height=None, timeout=60, files=None, first_width=2000, mark=None,
          slack=(4, 2)):
    """Draw `html_text` and write it to `out` (several parts: out-1.png, out-2.png …). Returns the paths.
    `files`: {name: bytes} written next to the page (an SVG the page shows, say). `first_width`: the window the
    page is laid out in (a page of its own, not a card, takes the width it is given). `mark`: the page's
    data-mark; with it, both passes must still be on that page (no picture of whatever it went to instead).
    `slack`: CSS pixels added to the window past the card, so text drawn a fraction wider does not wrap."""
    with tempfile.TemporaryDirectory(prefix="display-") as tmp:
        path = os.path.join(tmp, "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_text)
        for name, data in (files or {}).items():
            with open(os.path.join(tmp, name), "wb") as f:
                f.write(data)
        r = run_chrome(["--window-size=%d,3000" % first_width, "--dump-dom", "file://" + path], timeout)
        dom = r.stdout.decode("utf-8", "replace")
        if mark and 'data-mark="%s"' % mark not in dom:
            raise Fail("畫面不是要畫的那一頁（頁面自己跳走了），沒有輸出")
        errors = re.search(r'<body[^>]*data-errors="([^"]*)"', dom)
        errors = json.loads(html.unescape(errors.group(1))) if errors else []
        if errors:
            raise Fail("畫不出來：\n  " + "\n  ".join(str(e) for e in errors[:8]))
        wide_math(dom)
        note = re.search(r'<body[^>]*data-note="([^"]*)"', dom)
        if note:
            say(html.unescape(note.group(1)))
        size = re.search(r'<body[^>]*data-size="(\d+)x(\d+)"', dom)
        if not size:
            raise Fail("Chrome 沒有在時間內畫完：%s" % r.stderr.decode("utf-8", "replace").strip()[-400:])
        blocks = re.search(r'<body[^>]*data-blocks="([^"]*)"', dom)
        blocks = json.loads(html.unescape(blocks.group(1))) if blocks else []
        # a little room: at another scale the text can come out a fraction of a pixel wider and wrap
        w, h = int(size.group(1)) + slack[0], int(size.group(2)) + slack[1]
        parts = cuts(blocks, h, max_height)
        outs = []
        for i, (a, b) in enumerate(parts):
            target = out if len(parts) == 1 else "%s-%d.png" % (out[:-4], i + 1)
            if os.path.exists(target):
                os.remove(target)
            r = run_chrome(["--window-size=%d,%d" % (w, b - a), "--force-device-scale-factor=%s" % scale]
                           + (["--dump-dom"] if mark else []) +
                           ["--screenshot=" + target, "file://%s#w=%d&from=%d" % (path, w, a)], timeout)
            if not os.path.isfile(target):
                raise Fail("Chrome 沒有輸出圖片：%s" % r.stderr.decode("utf-8", "replace").strip()[-400:])
            if mark and 'data-mark="%s"' % mark not in r.stdout.decode("utf-8", "replace"):
                os.remove(target)
                raise Fail("畫面不是要畫的那一頁（頁面自己跳走了），沒有輸出")
            outs.append(target)
        return outs


def wide_math(dom, strict=False):
    """The formulas the page says its card cut (data-wide): said, or with `strict` (for the bot: no picture with
    half a formula) failed."""
    wide = re.search(r'<body[^>]*data-wide="([^"]*)"', dom)
    if not wide:
        return
    said = "第 %s 個公式太寬被裁掉：拆成多行（align）" % "、".join(str(n) for n in json.loads(html.unescape(wide.group(1))))
    if strict:
        raise Fail(said)
    say(said)


def shoot_segments(html_text, out, scale=2.0, timeout=60, strict=False):
    """A page of several cards (D.segments) as one picture each: out-1.png, out-2.png … in order. One pass lays
    them all out and says their sizes; each picture is then a window that size, showing that card alone (#only=).
    `strict`: a formula cut by its card fails it all (wide_math)."""
    with tempfile.TemporaryDirectory(prefix="display-") as tmp:
        path = os.path.join(tmp, "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_text)
        r = run_chrome(["--window-size=2000,3000", "--dump-dom", "file://" + path], timeout)
        dom = r.stdout.decode("utf-8", "replace")
        errors = re.search(r'<body[^>]*data-errors="([^"]*)"', dom)
        errors = json.loads(html.unescape(errors.group(1))) if errors else []
        if errors:
            raise Fail("畫不出來：\n  " + "\n  ".join(str(e) for e in errors[:8]))
        wide_math(dom, strict)
        found = re.search(r'<body[^>]*data-segments="([^"]*)"', dom)
        if not found:
            raise Fail("Chrome 沒有在時間內畫完：%s" % r.stderr.decode("utf-8", "replace").strip()[-400:])
        outs = []
        for i, (_, w, h) in enumerate(json.loads(html.unescape(found.group(1)))):
            target = "%s-%d.png" % (out[:-4], i + 1)
            if os.path.exists(target):
                os.remove(target)
            # the same room as shoot() gives a card: text drawn a fraction wider at this scale does not wrap
            r = run_chrome(["--window-size=%d,%d" % (w + 4, h + 2), "--force-device-scale-factor=%s" % scale,
                            "--screenshot=" + target, "file://%s#w=%d&only=%d" % (path, w + 4, i + 1)], timeout)
            if not os.path.isfile(target):
                raise Fail("Chrome 沒有輸出圖片：%s" % r.stderr.decode("utf-8", "replace").strip()[-400:])
            outs.append(target)
        return outs
