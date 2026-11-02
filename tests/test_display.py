"""display: guessing the kind, the pages and inputs it builds, and (when this machine has the tools) a real picture of
every kind, errors included."""
import contextlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(HERE, "bin", "display")
sys.path.insert(0, os.path.join(HERE, "lib"))
try:
    from PIL import Image  # noqa: E402
except ImportError:
    Image = None
from display import cli, confine, kinds, render, sources, tools  # noqa: E402


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", head[16:24])


class Detect(unittest.TestCase):
    def test_by_name(self):
        for name, kind in [("a.md", "doc"), ("a.csv", "doc"), ("a.mmd", "diagram"), ("a.dot", "graph"),
                           ("a.patch", "diff"), ("a.svg", "image"), ("a.pdf", "pdf"), ("a.tex", "tex"),
                           ("a.py", "plot"), ("a.log", "term")]:
            self.assertEqual(kinds.detect("x", name), kind, name)

    def test_json_only_when_vega(self):
        self.assertEqual(kinds.detect('{"mark": "bar"}', "a.json"), "chart")
        self.assertEqual(kinds.detect('{"a": 1}', "a.json"), "doc")

    def test_by_content(self):
        self.assertEqual(kinds.detect("flowchart LR\n a --> b"), "diagram")
        self.assertEqual(kinds.detect("graph TD\n a --> b"), "diagram")
        self.assertEqual(kinds.detect("digraph G { a -> b }"), "graph")
        self.assertEqual(kinds.detect("strict graph { a -- b }"), "graph")
        self.assertEqual(kinds.detect("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"), "diff")
        self.assertEqual(kinds.detect("\x1b[31mred\x1b[0m"), "term")
        self.assertEqual(kinds.detect("<svg xmlns='http://www.w3.org/2000/svg'></svg>"), "image")
        self.assertEqual(kinds.detect("$x^2$ 和表格"), "doc")


class Inputs(unittest.TestCase):
    def test_csv_becomes_a_table(self):
        for name, table in (("a.csv", False), (None, True)):
            md = kinds.csv_to_markdown("名稱,數量\n蘋果,3\n香蕉,12\n", name, table)
            self.assertEqual(md.splitlines()[0], "| 名稱 | 數量 |")
            self.assertEqual(md.splitlines()[1], "|---|---:|")

    def test_csv_only_when_asked(self):
        # stdin, .md and .txt are never guessed to be CSV: prose with one comma a line stays prose
        text = "Hello, world\nThanks, bye\n"
        for name in (None, "a.md", "a.txt"):
            self.assertEqual(kinds.csv_to_markdown(text, name), text)

    def test_ansi(self):
        out = kinds.ansi_html("\x1b[1;31mred <b>\x1b[0m plain \x1b[38;5;208mx\x1b[38;2;1;2;3my\x1b[0m", "dark")
        self.assertIn("font-weight:bold", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("color:#ff8700", out)
        self.assertIn("color:#010203", out)
        self.assertNotIn("\x1b", out)

    def test_script_json_cannot_end_the_script(self):
        self.assertNotIn("</script", kinds.script_json("</script><script>alert(1)</script>"))

    def test_html_page_is_locked_in_a_frame(self):
        built = kinds.image("<html><head></head><body>x</body></html>", "dark", 720)
        self.assertIn('sandbox="allow-scripts"', built["page"])
        self.assertNotIn("allow-top-navigation", built["page"])
        self.assertNotIn("allow-same-origin", built["page"])
        self.assertIn("frame-src &#x27;none&#x27;", built["page"].split("<body")[1])  # in the frame too
        self.assertIn("frame-src 'none'", built["page"].split("<body")[0])  # and on our page
        self.assertIn('data-mark="%s"' % built["mark"], built["page"])

    def test_csp_keeps_the_page_out_of_quirks_mode(self):
        for text, before in (("<!DOCTYPE html><html lang=en><body>x", "<html lang=en>"),
                             ("<!doctype html><p>x", "<!doctype html>"),
                             ("<html><head><title>t</title></head>", "<head>"), ("<p>x", "")):
            locked = kinds.lock(text)
            self.assertIn(before + kinds.LOCKED, locked)
            self.assertEqual(locked.lower().startswith("<!doctype"), text.lower().startswith("<!doctype"))

    def test_cuts(self):
        self.assertEqual(render.cuts([[0, 100]], 120, 1000), [(0, 120)])
        parts = render.cuts([[22, 300], [310, 900], [910, 1500], [1510, 2000]], 2022, 1000)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], 2022)
        self.assertTrue(all(b - a <= 1000 for a, b in parts))


class Setup(unittest.TestCase):
    def test_unpack_keeps_only_what_is_asked_and_stays_inside(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("package/dist/a.js", "package/other.js", "package/../evil.js", "/abs.js"):
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "pkg")
            tools.unpack(buf.getvalue(), dest, ["dist/a.js"])
            found = sorted(os.path.relpath(os.path.join(r, f), dest) for r, _, fs in os.walk(dest) for f in fs)
            self.assertEqual(found, [".complete", "dist/a.js"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "evil.js")))


class PluginJson(unittest.TestCase):
    def test_engine_accepts_it(self):
        engine = os.path.expanduser("~/altention/engine")
        if not os.path.isdir(os.path.join(engine, "altention")):
            self.skipTest("no engine here")
        code = ("import json,sys; sys.path.insert(0, %r); from altention import plugin; "
                "plugin.check(json.load(open(%r)))" % (engine, os.path.join(HERE, "plugin.json")))
        r = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(r.returncode, 0, r.stdout.decode())

    def test_skill(self):
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["name"], "display")
        for s in d["skills"]:
            with open(os.path.join(HERE, s["path"], "SKILL.md"), encoding="utf-8") as f:
                self.assertIn("name: %s\n" % s["name"], f.read())

    def test_setup_and_needs(self):
        # the core's install runs each setup item but the lazy ones; needs[] are checked, never installed
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual([s["name"] for s in d["setup"]], list(tools.SETUP_GROUPS))
        self.assertEqual([s["name"] for s in d["setup"] if s.get("lazy")], list(tools.LAZY))
        self.assertNotIn("today", json.dumps(d["setup"]))
        self.assertEqual([n["name"] for n in d["needs"]], list(tools.SYSTEM))
        for s in d["setup"] + d["needs"]:
            cmd = s.get("run") or s["check"]
            self.assertTrue(cmd.startswith('{python} "{plugin}/bin/display" '), cmd)  # not a bare python3
        for n in d["needs"]:
            self.assertIn(tools.SYSTEM[n["name"]][1], n["hint"])
            cmd = n["check"].replace("{python}", sys.executable).replace("{plugin}", HERE)
            r = subprocess.run(["bash", "-c", cmd], cwd=HERE, stdout=subprocess.PIPE, timeout=30)
            ok = tools.have(n["name"])
            self.assertEqual(r.returncode, 0 if ok else tools.CANNOT if ok is None else 1, n["name"])
            self.assertTrue(r.stdout.decode().startswith(n["name"] + ": "))

    def test_needs_said_to_setup(self):
        # #setup says why, how big and how (a sudo command is the person's own); what works without one is optional
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["offer"]["when"], "install")
        for n in d["needs"]:
            self.assertTrue(n["why"] and n["size"] and n["hint"], n["name"])
            self.assertLessEqual(set(n["install"]), {"debian", "fedora", "arch", "macos"})
            for system, cmd in n["install"].items():
                self.assertTrue(cmd.startswith("sudo ") or system == "macos", cmd)
            self.assertEqual(bool(n.get("optional")), bool(n.get("for")), n["name"])
        self.assertEqual([n["name"] for n in d["needs"] if n.get("optional")], ["poppler", "venv"])
        self.assertIn("ask", next(n for n in d["needs"] if n["name"] == "cjk-font"))

    def test_a_font_looked_for_without_fc_list(self):
        # by the files' names; fonts but none of them Chinese by name: 3, the engine's "cannot tell", said in words
        # too for an engine that takes it as not there
        if sys.platform == "darwin":
            self.skipTest("macOS always has one")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(tools, "FONT_DIRS", (tmp,)), mock.patch("subprocess.run", side_effect=OSError):
            self.assertIs(tools.cjk_font(), False)
            # not one of the sure names: asked
            for name in ("DejaVuSans.ttf", "NanumGothic.ttf", "tipa.otf", "Lipari-Regular.ttf", "DroidSansFallback.ttf"):
                open(os.path.join(tmp, name), "w").close()
            self.assertIsNone(tools.cjk_font())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(tools.check_one("cjk-font"), tools.CANNOT)
            self.assertIn("查不了", out.getvalue())
            for name in ("ipag.ttf", "NotoSansCJK-Regular.ttc", "SourceHanSansTC-Regular.otf", "wqy-zenhei.ttc"):
                open(os.path.join(tmp, name), "w").close()
                self.assertIs(tools.cjk_font(), True, name)
                os.remove(os.path.join(tmp, name))

    def test_pdf_needs_a_file(self):
        r = subprocess.run([sys.executable, BIN, "-e", "x", "--kind", "pdf"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("pdf 要給檔案", r.stderr.decode())
        self.assertNotIn("Traceback", r.stderr.decode())

    def test_missing_says_what_to_run(self):
        said = tools.missing_said("tex", ["tectonic", "pdftoppm"])
        self.assertIn(tools.BIN + " --setup tectonic", said)
        self.assertIn("setup「tectonic」", said)
        self.assertIn("brew install poppler", said)
        said = tools.missing_said("doc", ["katex", "mermaid"])
        self.assertEqual(said.count("--setup browser"), 1)  # one setup item for all the browser libraries
        self.assertIn("katex、mermaid：", said)
        self.assertTrue(os.access(tools.BIN, os.X_OK))

    def test_skill_description_survives_yaml(self):
        # an unquoted value with " #" in it is cut there by YAML: the rest becomes a comment
        try:
            import yaml
        except ImportError:
            self.skipTest("no yaml here")
        with open(os.path.join(HERE, "skill", "SKILL.md"), encoding="utf-8") as f:
            front = f.read().split("---\n")[1]
        line = next(l for l in front.splitlines() if l.startswith("description:"))
        raw = line[len("description:"):].strip()
        if raw[:1] in "\"'" and raw[-1:] == raw[:1]:
            raw = raw[1:-1]
        self.assertEqual(yaml.safe_load(front)["description"], raw)


SAMPLES = {
    "doc": ("a.md", "## 標題\n\n公式 $\\frac{a}{b}$，價錢 $5\n\n$$\\int_0^1 x\\,dx$$\n\n| 名稱 | 值 |\n|---|--:|\n| x | 1 |\n"),
    "chart": ("a.json", json.dumps({"data": {"values": [{"x": 1, "y": 2}, {"x": 2, "y": 3}]}, "mark": "line",
                                     "encoding": {"x": {"field": "x", "type": "quantitative"},
                                                  "y": {"field": "y", "type": "quantitative"}}})),
    "diagram": ("a.mmd", "flowchart LR\n  A[開始] --> B((z)) --> C[結束]\n"),
    "graph": ("a.dot", "digraph { 資料 -> 模型 -> 評估 }\n"),
    "diff": ("a.diff", "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a = 1\n+a = 2\n b = 3\n"),
    "term": ("a.log", "\x1b[32mok\x1b[0m 中文\n"),
    "image": ("a.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" '
                       'height="60" fill="#5865f2"/></svg>'),
    "tex": ("a.tex", "$\\int_0^1 x\\,dx$"),
    "plot": ("a.py", "import matplotlib.pyplot as plt\nplt.plot([1, 2, 3], [3, 1, 2])\nplt.title('中文')\n"),
}
ERRORS = {
    "doc": ("b.md", "$\\nosuchmacro{x}$", "nosuchmacro"),
    "diagram": ("b.mmd", "flowchart LR\n A -->\n", "Parse error"),
    "graph": ("b.dot", "digraph { a -> }", "syntax error"),
    "chart": ("b.json", '{"mark": "bar", "data": {"url": "x.csv"}}', "data.values"),
}


class Draw(unittest.TestCase):
    def run_display(self, *args):
        return subprocess.run([sys.executable, BIN] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=600)

    def test_every_kind(self):
        for kind, (name, text) in SAMPLES.items():
            with self.subTest(kind=kind):
                if tools.missing(kind):
                    self.skipTest("this machine lacks %s" % tools.missing(kind))
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, name)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    r = self.run_display(path, "-o", os.path.join(tmp, "out.png"))
                    self.assertEqual(r.returncode, 0, r.stderr.decode())
                    outs = r.stdout.decode().split()
                    self.assertTrue(outs)
                    w, h = png_size(outs[0])
                    self.assertGreater(w, 60)
                    self.assertGreater(h, 30)

    def test_equation_numbers_have_room(self):
        # KaTeX puts (1) at the right edge of a card as wide as the formula, over it: numbered, the picture is wider
        # than unnumbered by the numbers' room, alone, as one of --for-bot's pictures and in --segments alike
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

        def widths(env):
            tex = "\\begin{%s}a&=b\\\\c&=d\\end{%s}" % (env, env)
            r = self.run_display("-e", tex, "--kind", "math", "-o", os.path.join(tmp, env + ".png"))
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            alone = png_size(r.stdout.decode().split()[0])[0]
            r = subprocess.run([sys.executable, BIN, "-", "--for-bot", "-o", os.path.join(tmp, env + "-bot.png")],
                               input=("說明：\n\n" + tex + "\n").encode(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            bot = png_size(json.loads(r.stdout.decode())[0]["images"][0])[0]
            path = os.path.join(tmp, env + ".md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("說明：\n\n" + tex + "\n")
            r = self.run_display(path, "--segments", "-o", os.path.join(tmp, env + "-seg.png"))
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            seg = png_size([p for p in r.stdout.decode().split() if p.endswith(".png")][0])[0]
            return alone, bot, seg

        for numbered, plain in zip(widths("align"), widths("align*")):
            self.assertGreater(numbered, plain * 1.4)

    def test_errors_are_said(self):
        for kind, (name, text, said) in ERRORS.items():
            with self.subTest(kind=kind):
                if tools.missing(kind):
                    self.skipTest("this machine lacks %s" % tools.missing(kind))
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, name)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    r = self.run_display(path, "-o", os.path.join(tmp, "out.png"))
                    self.assertEqual(r.returncode, 2)
                    self.assertIn(said, r.stderr.decode())

    def test_html_cannot_show_local_files(self):
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        with tempfile.TemporaryDirectory() as tmp:
            secret = os.path.join(tmp, "secret.svg")
            with open(secret, "w") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" '
                        'height="300" fill="red"/></svg>')
            page = os.path.join(tmp, "p.html")
            with open(page, "w") as f:
                f.write('<html><head></head><body style="margin:0;background:#fff">'
                        '<img src="file://%s" width="300" height="300"></body></html>' % secret)
            out = os.path.join(tmp, "out.png")
            r = self.run_display(page, "-o", out, "--scale", "1")
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            try:
                from PIL import Image
            except ImportError:
                self.skipTest("no PIL to look at the pixels")
            im = Image.open(out).convert("RGB")
            reds = sum(1 for r, g, b in im.getdata() if r > 200 and g < 60 and b < 60)
            self.assertEqual(reds, 0, "the local file showed up in the picture")

    def test_html_cannot_go_to_local_files(self):
        """A page that moves itself (or our page) to a file of this machine: no picture of that file, ever."""
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("no PIL to look at the pixels")
        with tempfile.TemporaryDirectory() as tmp:
            secret = os.path.join(tmp, "secret.svg")
            with open(secret, "w") as f:
                f.write('<svg xmlns="http://www.w3.org/2000/svg" width="3000" height="3000"><rect width="3000" '
                        'height="3000" fill="red"/></svg>')
            url = "file://" + secret
            moves = {
                "hash": '<script>if (location.hash.indexOf("from") >= 0) location.href = "%s";</script>' % url,
                "location": '<script>setTimeout(function () { location.href = "%s"; }, 50);</script>' % url,
                "refresh": '<meta http-equiv="refresh" content="0;url=%s">' % url,
                "top": '<script>window.top.location = "%s";</script>' % url,
                "parent": '<script>parent.location.href = "%s";</script>' % url,
                "frame": '<iframe src="%s" width="300" height="300"></iframe>' % url,
                "open": '<script>window.open("%s", "_top");</script>' % url,
            }
            for how, move in moves.items():
                with self.subTest(how=how):
                    page = os.path.join(tmp, "p.html")
                    with open(page, "w") as f:
                        f.write('<!doctype html><html><head></head><body style="margin:0;background:#fff">'
                                '<p>hello</p>%s</body></html>' % move)
                    out = os.path.join(tmp, "out.png")
                    if os.path.exists(out):
                        os.remove(out)
                    r = self.run_display(page, "-o", out, "--scale", "1")
                    if r.returncode != 0:  # refused: fine, as long as nothing was written
                        self.assertEqual(r.returncode, 2, r.stderr.decode())
                        self.assertFalse(os.path.exists(out))
                        continue
                    im = Image.open(out).convert("RGB")
                    reds = sum(1 for r, g, b in im.getdata() if r > 200 and g < 60 and b < 60)
                    self.assertEqual(reds, 0, "the local file showed up in the picture")

    def test_html_is_drawn_as_in_a_browser(self):
        """A page that sets no background is white, like in a browser, not black text on our dark card."""
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("no PIL to look at the pixels")
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "plain.html")
            with open(page, "w") as f:
                f.write("<html><head><title>r</title></head><body><h1>Report</h1><p>plain page</p></body></html>")
            out = os.path.join(tmp, "out.png")
            r = self.run_display(page, "-o", out, "--scale", "1")
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            im = Image.open(out).convert("RGB")
            w, h = im.size
            for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                self.assertEqual(im.getpixel(xy), (255, 255, 255), xy)

    def test_html_cannot_say_it_is_huge(self):
        """The size a page reports is believed only up to HTML_MAX, and a tall one is cut like a long doc."""
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "big.html")
            with open(page, "w") as f:
                f.write('<body><p>x</p><script>setInterval(function () { parent.postMessage({display: '
                        '[99999, 99999]}, "*"); }, 10);</script></body>')
            r = self.run_display(page, "-o", os.path.join(tmp, "out.png"), "--scale", "1")
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            outs = r.stdout.decode().split()
            sizes = [png_size(p) for p in outs]
            self.assertEqual(sum(h for _, h in sizes), kinds.HTML_MAX[1])
            for w, h in sizes:
                self.assertEqual(w, kinds.HTML_MAX[0])
                self.assertLessEqual(h, 1400)
            self.assertIn("99999×99999", r.stderr.decode())  # and it says what was left out

    def drawn(self, name, text, *args):
        """The (width, height) in CSS pixels of the one picture drawn from `text` (a file `name`)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            r = self.run_display(path, "-o", os.path.join(tmp, "out.png"), "--scale", "1", *args)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            return png_size(r.stdout.decode().split()[0])

    def test_a_chart_by_category_has_room(self):
        if tools.missing("chart"):
            self.skipTest("this machine lacks %s" % tools.missing("chart"))
        spec = {"data": {"values": [{"m": m, "v": v} for m, v in (("Jan", 3), ("Feb", 5), ("Mar", 4))]},
                "mark": "line", "encoding": {"x": {"field": "m", "type": "ordinal"},
                                             "y": {"field": "v", "type": "quantitative"}}}
        self.assertGreater(self.drawn("c.vl.json", json.dumps(spec))[0], 480)  # it was about 200
        spec["width"] = 150  # a width written is kept
        self.assertLess(self.drawn("c.vl.json", json.dumps(spec))[0], 300)
        # a year of days by month: 12 cells, not 365 values
        days = [{"d": "2025-%02d-%02d" % (m, d), "v": 1} for m in range(1, 13) for d in range(1, 29)]
        year = {"data": {"values": days}, "mark": "bar",
                "encoding": {"x": {"field": "d", "timeUnit": "month", "type": "ordinal"},
                             "y": {"aggregate": "sum", "field": "v", "type": "quantitative"}}}
        self.assertLess(self.drawn("y.vl.json", json.dumps(year))[0], 900)
        # 200 names: held at the card's width, squeezed rather than a strip no phone can read
        names = {"data": {"values": [{"n": "name%03d" % i, "v": i} for i in range(200)]}, "mark": "bar",
                 "encoding": {"x": {"field": "n", "type": "nominal"}, "y": {"field": "v", "type": "quantitative"}}}
        self.assertLess(self.drawn("n.vl.json", json.dumps(names))[0], 900)

    def test_side_by_side_is_as_wide_as_its_sides(self):
        if tools.missing("diff"):
            self.skipTest("this machine lacks %s" % tools.missing("diff"))
        text = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n"
        self.assertLess(self.drawn("x.diff", text, "--side-by-side")[0], 800)  # it was 1400, half of it empty

    def test_a_short_html_page_is_cut_to_what_it_shows(self):
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        w, _ = self.drawn("p.html", "<html><body><h1>Hello</h1><p>short page</p></body></html>")
        self.assertLess(w, 300)
        wide = '<html><body><div style="background:#eee">a bar across the page</div></body></html>'
        self.assertEqual(self.drawn("w.html", wide)[0], 1000)  # something reaches the edge: the whole width
        # what the measure does not see keeps the page its width: text from CSS, a line, an outline
        after = ('<html><head><style>p::after { content: "很長的說明很長的說明很長的說明很長的說明很長的說明" }</style>'
                 '</head><body><p></p></body></html>')
        for page in (after, '<body><div style="width:600px;border-bottom:3px solid red">x</div></body>',
                     '<body><div style="width:500px;outline:3px solid blue">y</div></body>'):
            w, h = self.drawn("x.html", page)
            self.assertEqual(w, 1000, page)
            self.assertLess(h, 100, page)  # not one character a line

    def test_gantt_has_no_today_line(self):
        if tools.missing("diagram"):
            self.skipTest("this machine lacks %s" % tools.missing("diagram"))
        # a node called gantt in a flowchart is not a gantt
        self.assertGreater(self.drawn("f.mmd", "flowchart TD\n  A --> gantt\n  gantt\n  gantt --> B\n")[1], 100)
        for text in ("gantt\n  dateFormat YYYY-MM-DD\n  section A\n  a :2020-01-01, 3d\n",
                     "---\ntitle: t\n---\n%%{init: {}}%%\n%% a note\ngantt\n  dateFormat YYYY-MM-DD\n  a :2020-01-01, 3d\n",
                     "# t\n\n```mermaid\ngantt\n  dateFormat YYYY-MM-DD\n  section A\n  a :2020-01-01, 3d\n```\n"):
            page = kinds.doc(text, "dark", 720) if text.startswith("#") else kinds.diagram(text, "dark", 720)
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "page.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(page)
                dom = render.run_chrome(["--window-size=2000,3000", "--dump-dom", "file://" + path], 60).stdout.decode()
            self.assertIn("<svg", dom)
            self.assertNotIn('class="today"', dom)

    def test_out_folder_is_made(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "not", "there", "tok.png")
            r = subprocess.run([sys.executable, BIN, "-", "--for-bot", "-o", out], input=b"$$x$$\n",
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertTrue(os.path.isfile(json.loads(r.stdout.decode())[0]["images"][0]))

    def test_a_picture_of_another_page_fails(self):
        """The second guard on its own: our page itself moving away between the passes is caught by the mark."""
        if tools.missing("image"):
            self.skipTest("no Chrome here")
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "other.html")
            with open(other, "w") as f:
                f.write("<p>other</p>")
            moving = render.page("<p>x</p>", "dark", mark="m0", script='D.finish(); if (location.hash.indexOf('
                                 '"from") >= 0) location.href = "file://%s";' % other)
            with self.assertRaises(tools.Fail):
                render.shoot(moving, os.path.join(tmp, "a.png"), 1, mark="m0")
            self.assertFalse(os.path.exists(os.path.join(tmp, "a.png")))
            render.shoot(render.page("<p>x</p>", "dark", mark="m1", script="D.finish();"),
                         os.path.join(tmp, "b.png"), 1, mark="m1")

    def test_pdf_returns_only_its_own_pages(self):
        if tools.missing("pdf"):
            self.skipTest("no pdftoppm here")
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "x.pdf")
            with open(pdf, "wb") as f:
                f.write(tiny_pdf())
            for old in ("rep-final.png", "rep-9.png"):
                open(os.path.join(tmp, old), "w").close()
            r = self.run_display(pdf, "-o", os.path.join(tmp, "rep.png"), "--pages", "1")
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertEqual([os.path.basename(p) for p in r.stdout.decode().split()], ["rep-1.png"])


ANSWER = """Softmax 把分數變成機率：

$$\\sigma(z)_i = \\frac{e^{z_i}}{\\sum_j e^{z_j}}$$

它的 Jacobian：

\\begin{align*}
J &= \\operatorname{diag}(\\sigma) - \\sigma\\sigma^\\top
\\end{align*}

$$a = b$$

比較，價錢 $5 和 `$x^2$` 不算公式：

| 損失 | 梯度 |
|---|---|
| CE | σ − y |

```python
print("$x_1$")
```

```mermaid
flowchart LR
  A --> B
```

最後一段沒有圖。"""


class Segments(unittest.TestCase):
    def test_split(self):
        got = kinds.split_segments(ANSWER)
        self.assertEqual([k for k, _ in got], ["text", "picture", "text", "picture", "text", "picture", "text",
                                               "picture", "text"])
        self.assertIn("\\begin{align*}", got[3][1])
        self.assertIn("$$a = b$$", got[3][1])  # next to each other: one picture
        self.assertIn('print("$x_1$")', got[6][1])  # a code block stays text
        self.assertTrue(got[7][1].startswith("```mermaid"))
        for kind, chunk in got:
            self.assertEqual(chunk, chunk.strip("\n"))
            self.assertTrue(chunk.strip())

    def test_posts(self):
        # Discord shows a message's text above its picture (seen on an iPad, 10-20): a text goes with the picture
        # after it, so each message reads text, then the picture it talks about
        plan = kinds.posts(kinds.split_segments(ANSWER))
        self.assertEqual([len(ps) for _, ps in plan], [1, 1, 1, 1, 0])
        self.assertEqual(plan[0][0], "Softmax 把分數變成機率：")
        self.assertIn("\\sigma(z)_i", plan[0][1][0])
        self.assertEqual(plan[1][0], "它的 Jacobian：")
        self.assertIn("\\operatorname{diag}", plan[1][1][0])
        self.assertTrue(plan[2][0].startswith("比較"))
        self.assertIn("| CE |", plan[2][1][0])
        self.assertTrue(plan[3][0].startswith("```python"))  # the diagram's text is the code before it
        self.assertTrue(plan[3][1][0].startswith("```mermaid"))
        self.assertEqual(plan[-1], ("最後一段沒有圖。", []))
        # pictures in a row go in one post with the text before them; nothing is ever an empty post
        plan = kinds.posts([("picture", "$$w$$"), ("picture", "| a |"), ("text", "a"), ("picture", "$$x$$"),
                            ("picture", "| b |"), ("text", "b")])
        self.assertEqual(plan, [("", ["$$w$$", "| a |"]), ("a", ["$$x$$", "| b |"]), ("b", [])])
        plan = kinds.posts([("text", "a"), ("text", "b"), ("picture", "$$x$$")])
        self.assertEqual(plan, [("a", []), ("b", ["$$x$$"])])

    def test_long_captions_are_cut(self):
        para = "字" * 700
        plan = kinds.posts([("text", "\n\n".join([para] * 5)), ("picture", "$$x$$"), ("picture", "$$y$$")])
        self.assertGreater(len(plan), 1)
        self.assertTrue(all(len(c) <= kinds.CAPTION_MAX and c for c, _ in plan))
        # the picture stays right after the text before it
        self.assertEqual([len(ps) for _, ps in plan], [0] * (len(plan) - 1) + [2])
        self.assertEqual("".join(c.replace("\n", "") for c, _ in plan), para * 5)  # nothing lost
        self.assertTrue(all(len(c) <= 1900 for c in kinds.split_caption("x" * 5000)))

    def test_long_code_stays_code(self):
        # a cut inside a ``` block closes it and opens it again with its language: each piece shows as code
        code = "\n".join("    x_%d = %d" % (i, i) + ("\n" if i % 7 == 0 else "") for i in range(300))
        text = "說明一下：\n\n```python\n" + code + "\n```\n\n後面的話。"
        pieces = kinds.split_caption(text)
        self.assertGreater(len(pieces), 2)
        for p in pieces:
            self.assertLessEqual(len(p), kinds.CAPTION_MAX)
            self.assertEqual(p.count("```") % 2, 0, p[:40])
        self.assertTrue(pieces[1].startswith("```python\n"))
        lines = [ln for p in pieces for ln in p.split("\n") if not ln.startswith("```")]
        self.assertEqual(lines, [ln for ln in text.split("\n") if not ln.startswith("```")])  # the code as it was
        plan = kinds.posts([("text", text), ("picture", "| a |\n|---|\n| 1 |")])  # what review hit: with a table
        self.assertTrue(all(c.count("```") % 2 == 0 for c, _ in plan))

    def test_inline_math_is_said(self):
        self.assertEqual(kinds.inline_math("其中 $\\nabla_z L$ 和 \\(x^2\\)，價錢 $5、`$x^2$` 不算"),
                         ["$\\nabla_z L$", "\\(x^2\\)"])
        self.assertEqual(kinds.inline_math("$x$、$O(n)$、$a+b=c$，行中的 $$x^2$$"),
                         ["$x$", "$O(n)$", "$a+b=c$", "$$x^2$$"])
        self.assertFalse(kinds.inline_math("價錢 $5 到 $10，cost $x and $y"))

    def test_draws_each_picture_on_its_own(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(ANSWER)
            r = subprocess.run([sys.executable, BIN, path, "--segments", "-o", os.path.join(tmp, "s.png")],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            lines = r.stdout.decode().splitlines()
            pictures = [ln.rsplit("  ", 1)[1] for ln in lines if ".png" in ln]
            self.assertEqual(len(pictures), 4)
            # the preview shows each post in the order it reads: its text, then the picture below it
            self.assertEqual(lines[0], "（文字）Softmax 把分數變成機率：  " + pictures[0])
            self.assertEqual(lines[-1], "（文字）最後一段沒有圖。")
            w, h = png_size(pictures[0])
            self.assertLess(w, 400 * 2)  # a single formula: as wide as it is, not a 720 card
            self.assertLess(h, 100 * 2)
            if Image is not None:  # each picture is its own card: no line of the one before at its top
                im = Image.open(pictures[2]).convert("RGB")
                top = [im.getpixel((x, y)) for y in range(0, 30) for x in range(im.width)]
                self.assertFalse([p for p in top if sum(p) > 500])

    def test_inline_math_stops_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("梯度是 $\\nabla_z L$：\n\n$$x$$\n")
            r = subprocess.run([sys.executable, BIN, path, "--segments", "--post", "nowhere", "-o",
                                os.path.join(tmp, "s.png")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(r.returncode, 2)
            self.assertIn("$\\nabla_z L$", r.stderr.decode())
            self.assertEqual(os.listdir(tmp), ["a.md"])  # nothing drawn, nothing posted

    def test_a_long_string_in_a_table_still_fits(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        sha = "ab12" * 39 + "zzQQ"
        table = "| 欄 | value |\n|---|---|\n| sha (commit) | %s |\n" % sha
        with tempfile.TemporaryDirectory() as tmp:
            for args in ([], ["--segments"]):
                with self.subTest(args=args):
                    path = os.path.join(tmp, "t.md")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(("前言：\n\n" if args else "") + table)
                    r = subprocess.run([sys.executable, BIN, path, "-o", os.path.join(tmp, "t.png")] + args,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
                    self.assertEqual(r.returncode, 0, r.stderr.decode())
                    picture = [ln.rsplit("  ", 1)[-1] for ln in r.stdout.decode().splitlines() if ".png" in ln][0]
                    w, h = png_size(picture)
                    self.assertLessEqual(w, (kinds.TABLE_MAX + 2 * 26 + 8) * 2)  # no wider than a table may be
                    if Image is not None:  # and nothing cut off on the right: the card's own edge is there
                        im = Image.open(picture).convert("RGB")
                        right = [im.getpixel((im.width - 1 - x, y)) for x in range(6) for y in range(im.height)]
                        self.assertFalse([p for p in right if sum(p) > 500])

    def test_table_words_are_not_cut(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        table = ("| 項目 | SGD (+momentum) | Adam | AdamW | Lion |\n|---|---|---|---|---|\n"
                 "| 記憶體（參數倍數，不含梯度） | 1×（無 momentum）／2× | 3× | 3× | 2× |\n"
                 "| 適用情境 | ResNet 等 CNN、記憶體吃緊 | 快速原型、RL、GAN | Transformer、LLM、ViT 預設 | 大模型、大 batch |\n")
        probe = r"""<script>setTimeout(function () {
  var cut = [];
  document.querySelectorAll("th, td").forEach(function (c) {
    var cs = getComputedStyle(c), room = c.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    c.textContent.split(/[\s、（）／]+/).forEach(function (w) {
      if (!/^[\x21-\x7e]+$/.test(w)) return;
      var s = document.createElement("span");
      s.style.whiteSpace = "nowrap"; s.textContent = w; c.appendChild(s);
      if (s.getBoundingClientRect().width > room + 0.5) cut.push(w);
      s.remove();
    });
  });
  document.body.setAttribute("data-cut", JSON.stringify(cut));
}, 1500);</script></body>"""
        page = kinds.doc(table, "dark", 720).replace("</body>", probe)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(page)
            dom = render.run_chrome(["--window-size=2000,3000", "--dump-dom", "file://" + path]).stdout.decode()
        import html as _html, re as _re
        cut = json.loads(_html.unescape(_re.search(r'data-cut="([^"]*)"', dom).group(1)))
        self.assertEqual(cut, [])

    def test_a_heading_is_never_last_on_a_picture(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        text = "\n\n".join("## 第 %d 段\n\n%s" % (i, "一段話。" * 60) for i in range(8))
        page = kinds.doc(text, "dark", 720)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(page)
            dom = render.run_chrome(["--window-size=2000,3000", "--dump-dom", "file://" + path]).stdout.decode()
        import html as _html, re as _re
        blocks = json.loads(_html.unescape(_re.search(r'data-blocks="([^"]*)"', dom).group(1)))
        self.assertEqual(len(blocks), 8)  # each heading glued to its paragraph: a cut falls only between pairs


class PostInOrder(unittest.TestCase):
    """One dc post per message, its pictures as files after the text; the bot keeps one sender's posts in order."""

    def run_posts(self, plan):
        calls = []

        def fake(cmd, **kw):
            self.assertIs(kw.get("stdin"), subprocess.DEVNULL)  # a post of only pictures must not read our stdin
            calls.append(cmd[3:])
            return subprocess.CompletedProcess(cmd, 0)
        run = cli.subprocess.run
        cli.subprocess.run = fake
        try:
            for caption, pictures in plan:
                cli.post("ch", caption, pictures)
        finally:
            cli.subprocess.run = run
        return calls

    def test_one_post_each_with_its_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a.png"), os.path.join(tmp, "b.png")
            for p in (a, b):
                with open(p, "wb") as f:
                    f.write(b"x")
            calls = self.run_posts([("第一段", [a, b]), ("第二段", []), ("", [a])])
        self.assertEqual(calls, [["ch", "--file", a, "--file", b, "第一段"], ["ch", "第二段"], ["ch", "--file", a]])

    def test_too_many_for_one_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            small = []
            for i in range(cli.FILES_MAX + 2):
                small.append(os.path.join(tmp, "s%d.png" % i))
                with open(small[-1], "wb") as f:
                    f.write(b"x")
            self.assertEqual([len(r) for r in cli.batches(small)], [cli.FILES_MAX, 2])
            big = []
            for i in range(3):
                big.append(os.path.join(tmp, "b%d.png" % i))
                with open(big[-1], "wb") as f:
                    f.truncate(cli.BYTES_MAX // 2 - 10)
            self.assertEqual([len(r) for r in cli.batches(big)], [2, 1])
            self.assertEqual(sum(cli.batches(small + big), []), small + big)  # in order, none lost
            calls = self.run_posts([("說明", small)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "說明")  # the text goes with the first
        self.assertNotIn("說明", calls[1])

    def test_this_channel_by_id(self):
        old = os.environ.get("DCBOT_CHANNEL")
        try:
            os.environ["DCBOT_CHANNEL"] = "123"
            for value in ("", ".", "self", " . "):
                self.assertEqual(cli.channel_of(value), "123")
            self.assertEqual(cli.channel_of("456"), "456")
            del os.environ["DCBOT_CHANNEL"]
            with self.assertRaises(tools.Fail):
                cli.channel_of(".")
            self.assertEqual(cli.channel_of("display-dev"), "display-dev")
        finally:
            if old is not None:
                os.environ["DCBOT_CHANNEL"] = old
        with self.assertRaises(tools.Fail):  # what was meant as the input, put after --post
            cli.channel_of("-")
        with self.assertRaises(tools.Fail):
            cli.channel_of(BIN)
        # --post with nothing after it, and before anything is drawn when there is no channel to name
        env = {k: v for k, v in os.environ.items() if k != "DCBOT_CHANNEL"}
        r = subprocess.run([sys.executable, BIN, "-e", "$$x$$", "--post"], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("DCBOT_CHANNEL", r.stderr.decode())
        self.assertEqual(r.stdout, b"")

    def test_skill_posts_back_by_id(self):
        with open(os.path.join(HERE, "skill", "SKILL.md"), encoding="utf-8") as f:
            skill = f.read()
        self.assertNotIn("dc post <頻道>", skill)
        self.assertNotIn("--post <頻道>", skill)



class ForBot(unittest.TestCase):
    """The bot hands a whole reply to `display - --for-bot -o {out}` and posts the JSON it gets back."""

    def run_bot(self, text, *extra, env=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = subprocess.run([sys.executable, BIN, "-", "--for-bot", "-o", os.path.join(tmp, "tok.png")] + list(extra),
                           input=text.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, env=env)
        return r, tmp

    def test_messages_as_json(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        r, tmp = self.run_bot(ANSWER + "\n\n其中 $x$ 照原樣留著。")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        posts = json.loads(r.stdout.decode())  # all of stdout is the JSON
        self.assertEqual([len(p["images"]) for p in posts], [1, 1, 1, 1, 0])
        self.assertEqual(posts[0]["text"], "Softmax 把分數變成機率：")
        self.assertIn("$x$", posts[-1]["text"])  # inline math is left as it is, not refused
        for p in posts:
            self.assertEqual(sorted(p), ["images", "sources", "text"] if p["images"] else ["images", "text"])
            self.assertIsInstance(p["text"], str)
            self.assertTrue(p["text"] or p["images"])
            for image in p["images"]:  # the bot takes only <token>-*.png in its folder
                self.assertEqual(os.path.dirname(image), tmp)
                self.assertRegex(os.path.basename(image), r"^tok-\d+\.png$")
                self.assertTrue(os.path.isfile(image))

    def test_nothing_to_draw_is_the_text_as_it_came(self):
        text = "一段話，價錢 $5，還有 $x$。\n\n```python\nprint(1)\n```\n"
        r, tmp = self.run_bot(text)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(json.loads(r.stdout.decode()), [{"text": text, "images": []}])
        self.assertEqual(os.listdir(tmp), [])

    def test_only_without_for_bot_is_math_refused(self):
        r = subprocess.run([sys.executable, BIN, "-", "--segments"], input="其中 $x$\n\n$$y$$\n".encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("$x$", r.stderr.decode())

    def test_refused_with_post_or_another_kind(self):
        for extra in (["--post", "123"], ["--kind", "diff"]):
            r, _ = self.run_bot("$$x$$", *extra)
            self.assertEqual(r.returncode, 2, extra)
            self.assertEqual(r.stdout, b"")

    def test_when_matches_what_is_drawn(self):
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as f:
            rw = json.load(f)["rewrite"]
        self.assertIn("--for-bot", rw["command"])
        self.assertIn("{out}", rw["command"])
        self.assertLessEqual(rw["timeout"], 60)
        when = re.compile(rw["when"], re.M)
        for text in (ANSWER, "a\n\n$$x$$", "a\n\n\\[x\\]", "a\n\n\\begin{align*}x\\end{align*}",
                     "| a | b |\n|---|---|\n| 1 | 2 |", "```mermaid\nflowchart LR\n  A --> B\n```",
                     "只有文字 $x$", "```python\nx = 1\n```", "a | b"):
            drawn = any(k == "picture" for k, _ in kinds.split_segments(text))
            if drawn:  # whatever display would draw, the bot hands over; a few false calls only cost a run
                self.assertTrue(when.search(text), text)
        for text in ("只有文字 $x$", "```python\nx = 1\n```", "價錢 $5 到 $10"):
            self.assertFalse(when.search(text), text)

    def test_old_temps_go(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        names = ["display-chrome-abc_1234", "display-x1y2z3w4", "display-pdf-abcdefgh", "display-test.u0cE4B",
                 "display-new12345"]
        for n in names:
            os.mkdir(os.path.join(tmp, n))
            if n != "display-new12345":
                os.utime(os.path.join(tmp, n), (time.time() - 7200,) * 2)
        old = tempfile.tempdir
        tempfile.tempdir = tmp
        try:
            render._old_temps()
        finally:
            tempfile.tempdir = old
        self.assertEqual(sorted(os.listdir(tmp)), ["display-new12345", "display-test.u0cE4B"])  # ours, new; not ours

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux: a parent-death signal")
    def test_killed_leaves_no_chrome(self):
        # the bot kills display at its timeout: the Chrome it was waiting for goes too
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake, mark = os.path.join(tmp, "chrome"), "47.%d" % os.getpid()
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexec sleep %s\n" % mark)
        os.chmod(fake, 0o755)
        p = subprocess.Popen([sys.executable, BIN, "-", "--for-bot", "-o", os.path.join(tmp, "t.png")],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, DISPLAY_CHROME=fake))
        p.stdin.write(b"$$x$$\n")
        p.stdin.close()
        sleeping = lambda: subprocess.run(["pgrep", "-f", "^sleep %s$" % mark], stdout=subprocess.PIPE).stdout.split()  # noqa: E731
        deadline = time.time() + 20
        while not sleeping() and time.time() < deadline:
            time.sleep(0.2)
        self.assertTrue(sleeping(), "the fake Chrome never started")
        p.kill()
        p.wait()
        deadline = time.time() + 5
        while sleeping() and time.time() < deadline:
            time.sleep(0.2)
        left = sleeping()
        for pid in left:
            os.kill(int(pid), 9)
        self.assertEqual(left, [])


class TexConfined(unittest.TestCase):
    """A LaTeX document cannot read or write files of this machine, nor put a file of its own into the TeX cache
    for later pictures to load. Run against a copy of this machine's cache, so a failure cannot spoil the real one."""

    @classmethod
    def setUpClass(cls):
        if tools.missing("tex"):
            raise unittest.SkipTest("this machine lacks %s" % tools.missing("tex"))
        cls.home = tempfile.mkdtemp()
        cls.pristine = os.path.join(cls.home, "pristine")
        if os.path.isdir(tools.tectonic_cache()):
            shutil.copytree(tools.tectonic_cache(), cls.pristine, symlinks=True)
        cls.old = os.environ.get("TECTONIC_CACHE_DIR")
        os.environ["TECTONIC_CACHE_DIR"] = cls.pristine
        kinds.tex_warm()
        # read, it stops the run with LEAKED; typeset, it shows SECRETWORD
        cls.secret = os.path.join(cls.home, "secret.tex")
        with open(cls.secret, "w") as f:
            f.write("SECRETWORD\\errmessage{LEAKED}\n")

    @classmethod
    def tearDownClass(cls):
        if cls.old is None:
            os.environ.pop("TECTONIC_CACHE_DIR", None)
        else:
            os.environ["TECTONIC_CACHE_DIR"] = cls.old
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        # each test on its own copy: one that spoils the cache cannot make the next one pass
        self.cache = os.path.join(self.home, "cache-" + self._testMethodName)
        shutil.copytree(self.pristine, self.cache, symlinks=True)
        os.environ["TECTONIC_CACHE_DIR"] = self.cache

    def draw(self, body, preamble=""):
        """(the error said, or None) for a picture of `body`; the picture must not show the secret."""
        out = os.path.join(self.home, "out.png")
        try:
            kinds.tex(body, out, "dark", 720, 1.0, preamble)
        except tools.Fail as e:
            self.assertNotIn("SECRETWORD", str(e))
            return str(e)
        return None

    def test_cannot_read_files(self):
        rel = os.path.relpath(self.secret, tempfile.gettempdir())
        up = "../../../../../../.." + self.secret
        # these find no file and go on: the picture is made
        for body in ["\\InputIfFileExists{%s}{}{}" % p for p in (self.secret, "../" + rel, up)] + [
                "\\newread\\r\\openin\\r=%s \\ifeof\\r\\else\\errmessage{LEAKED}\\fi x" % self.secret,
                "\\IfFileExists{%s}{\\errmessage{LEAKED}}{x}" % self.secret,
                "x\\include{%s}" % self.secret[:-4]]:
            with self.subTest(body=body):
                self.assertIsNone(self.draw(body))
        # these stop on a file not found
        for body in ["\\input{%s}" % self.secret, "\\input{../%s}" % rel]:
            with self.subTest(body=body):
                said = self.draw(body)
                self.assertIsNotNone(said)
                self.assertNotIn("LEAKED", said)
                self.assertRegex(said, "not found|LaTeX Error: File")  # TeX may wrap the line before "not found"

    def test_cannot_read_other_programs_files_in_dev(self):
        # shared memory, where any program of this account may leave files (Linux)
        for shm in [d for d in ("/dev/shm",) if os.path.isdir(d)]:
            with tempfile.NamedTemporaryFile("w", dir=shm, suffix=".tex") as f:
                f.write("SECRETWORD\\errmessage{LEAKED}\n")
                f.flush()
                self.assertIsNone(self.draw("x\\InputIfFileExists{%s}{}{}" % f.name))
                said = self.draw("\\input{%s}" % f.name)
                self.assertIsNotNone(said)
                self.assertNotIn("LEAKED", said)
        # nor a terminal of this account (one made here, typed into before it is read)
        master, slave = os.openpty()
        try:
            os.write(master, b"SECRETWORD\n")
            cat = os.path.realpath(shutil.which("cat"))
            r = confine.run([cat, os.ttyname(slave)], [], [os.path.dirname(cat)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=10)
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn(b"SECRETWORD", r.stdout)
        finally:
            os.close(master)
            os.close(slave)
        # the devices it does get still work
        self.assertIsNone(self.draw("x\\immediate\\openout9=/dev/null \\immediate\\write9{x}\\immediate\\closeout9"))

    def test_cannot_read_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(self.secret, os.path.join(tmp, "link.txt"))
            cat = os.path.realpath(shutil.which("cat"))
            r = confine.run([cat, os.path.join(tmp, "ok.txt")], [tmp], [os.path.dirname(cat)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
            self.assertEqual(r.returncode, 1)  # it runs, and says there is no such file
            r = confine.run([cat, os.path.join(tmp, "link.txt")], [tmp], [os.path.dirname(cat)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(r.returncode, 0)
            self.assertNotIn(b"SECRETWORD", r.stdout)
            r = confine.run([cat, os.path.join(tmp, "..", os.path.basename(self.home), "secret.txt")], [tmp],
                            [os.path.dirname(cat)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotIn(b"SECRETWORD", r.stdout)

    def test_cannot_write_files(self):
        outside = os.path.join(self.home, "written.txt")
        rel = os.path.relpath(outside, tempfile.gettempdir())
        for target in (outside, "../" + rel, "../../../../../../.." + outside):
            with self.subTest(target=target):
                self.draw("x\\immediate\\openout9=%s \\immediate\\write9{EVIL}\\immediate\\closeout9" % target)
                self.assertFalse(os.path.exists(outside))
                self.assertFalse(os.path.exists(outside + ".tex"))

    def test_cannot_put_a_package_in_the_cache(self):
        files = kinds.bundle_files()
        cached = os.path.join(files["amsmath.sty"], "amsmath.sty")
        with open(cached, "rb") as f:
            before = f.read()
        fresh = next(n for n in sorted(files) if n.endswith(".sty") and not os.path.exists(os.path.join(files[n], n)))
        folder = os.path.basename(files[fresh])
        writes = "".join("\\immediate\\openout9=%s \\immediate\\write9{EVIL}\\immediate\\closeout9 " % t for t in (
            cached,                                                         # the real cache, straight
            "cache/bundles/data/%s/amsmath.sty" % folder,                  # through the throwaway cache's link
            "cache/bundles/data/%s/%s" % (folder, fresh),                  # a file the cache does not have yet
            "cache/bundles/data/%s/made-up.sty" % folder,                  # a name the bundle does not have
            os.path.join(files[fresh], fresh)))
        # and a workspace file for the run that fetches by name, pointing at another bundle
        writes += "\\immediate\\openout9=Tectonic.toml \\immediate\\write9{[doc]}\\immediate\\closeout9 "
        self.draw("x" + writes)
        with open(cached, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(os.path.join(files[fresh], "made-up.sty")))
        if os.path.exists(os.path.join(files[fresh], fresh)):  # fetched again by name: the bundle's, not ours
            with open(os.path.join(files[fresh], fresh), "rb") as f:
                self.assertNotIn(b"EVIL", f.read())
        self.assertIsNone(self.draw("$\\int_0^1 x\\,dx$"))

    def test_only_plain_bundle_names_are_fetched(self):
        files = kinds.bundle_files()
        folder = os.path.basename(files["amsmath.sty"])
        fresh, other = [n for n in sorted(files)
                        if n.endswith(".sty") and not os.path.exists(os.path.join(files[n], n))][:2]
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "bundles", "data", folder)
            os.makedirs(os.path.join(data, "sub"))
            deep = os.path.join(tmp, "bundles", "data", "deep", folder)
            os.makedirs(deep)
            open(os.path.join(deep, other), "w").close()  # a bundle name, one folder too deep
            for name in (fresh, ".hidden.sty", "made-up.sty", "sub/" + fresh):
                open(os.path.join(data, name), "w").close()
            os.symlink(os.path.join(files[fresh], "x"), os.path.join(data, "linked.sty"))
            self.assertEqual(kinds.downloaded(tmp), [fresh])
        for bad in ("../x.sty", "a/b.sty", ".x", "x..sty", "..", "-x.sty"):
            self.assertIsNone(kinds.BUNDLE_NAME.match(bad), bad)

    def test_fetching_is_capped(self):
        files = kinds.bundle_files()
        many = [n for n in sorted(files) if n.endswith(".sty") and not os.path.exists(os.path.join(files[n], n))][:5]
        calls = []
        downloaded, tectonic, cap = kinds.downloaded, kinds.tectonic, kinds.TEX_FETCH_FILES
        kinds.downloaded = lambda cache: many
        kinds.tectonic = lambda args, tmp, cache=None, timeout=600: (calls.append(args) if args[0] == "-X" else None) or \
            tectonic(args, tmp, cache, timeout)
        kinds.TEX_FETCH_FILES = 2
        try:
            self.assertIsNone(self.draw("x"))
        finally:
            kinds.downloaded, kinds.tectonic, kinds.TEX_FETCH_FILES = downloaded, tectonic, cap
        self.assertEqual(len(calls), 2)

    def test_still_draws_packages_it_fetches(self):
        self.assertIsNone(self.draw("$\\mathscr{L}$ \\tikz\\draw[-{Stealth}] (0,0)--(1,0);",
                                    "\\usepackage{tikz,mathrsfs}\\usetikzlibrary{arrows.meta}"))

    def test_refused_without_a_sandbox(self):
        how = confine.how
        confine.how = lambda: None
        try:
            with self.assertRaises(tools.Fail) as e:
                kinds.tex("x", os.path.join(self.home, "out.png"), "dark", 720, 1.0)
            self.assertIn("不畫 tex", str(e.exception))
            self.assertIn("sandbox", tools.missing("tex"))
        finally:
            confine.how = how


def tiny_pdf():
    """A one-page PDF, written out by hand."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, o)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


class Sources(unittest.TestCase):
    """Each picture has <picture>.png.source.json, {kind, lang, tool, source}: the bot gives it back on 📝."""

    def valid(self, info):
        self.assertEqual(sorted(info), ["kind", "lang", "source", "tool"])
        self.assertRegex(info["lang"], r"^[A-Za-z0-9+-]{1,20}$")
        for k in ("kind", "tool"):
            self.assertTrue(0 < len(info[k]) <= 40, info)
        self.assertTrue(info["source"].strip())

    def test_every_kind_says_what_it_is(self):
        inputs = {"doc": "# 標題\n\n一段話。", "chart": '{"mark": "bar"}', "diagram": "gantt\n  a :2026-01-01, 3d",
                  "graph": "digraph { a -> b }", "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n", "term": "\x1b[31mred",
                  "image": "<svg xmlns='http://www.w3.org/2000/svg'/>", "tex": "\\section{A}", "plot": "import matplotlib"}
        for kind, text in inputs.items():
            info = sources.of_input(kind, text)
            self.valid(info)
            self.assertEqual(info["source"], text)
        self.assertEqual(sources.of_input("diagram", inputs["diagram"])["kind"], "甘特圖")
        self.assertEqual(sources.of_input("image", "<html><body>x</body></html>")["lang"], "html")
        self.assertEqual(sources.of_input("doc", "a,b\n1,2\n", "t.csv")["lang"], "csv")
        # --kind table, csv or tsv on stdin: the table as it came (as csv_to_markdown reads it)
        self.assertEqual(sources.of_input("doc", "a,b\n1,2\n", table=True)["lang"], "csv")
        self.assertEqual(sources.of_input("doc", "a\tb\n1\t2\n", table=True)["lang"], "tsv")
        self.assertEqual(sources.of_input("doc", "| a |\n|---|\n| 1 |\n", table=True)["lang"], "markdown")
        self.assertIsNone(sources.of_input("pdf", None, "a.pdf"))  # no text to give back

    def test_markdown_pictures_by_their_blocks(self):
        self.assertEqual(sources.of_markdown("$$x$$")["kind"], "公式")
        self.assertEqual(sources.of_markdown("$$x$$")["lang"], "latex")
        both = sources.of_markdown("$$x$$\n| a |\n|---|\n| 1 |")
        self.assertEqual((both["kind"], both["lang"]), ("公式和表格", "markdown"))
        three = sources.of_markdown("| a |\n|---|\n| 1 |\n\n$$x$$\n\n```mermaid\ngraph LR; A-->B\n```")
        self.assertEqual(three["kind"], "表格、公式和流程圖")
        alone = sources.of_markdown("```mermaid\nflowchart TD\n  a --> b\n```")
        self.assertEqual(alone, {"kind": "流程圖", "lang": "mermaid", "tool": "Mermaid",
                                 "source": "flowchart TD\n  a --> b"})

    def test_too_long_has_none(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pic = os.path.join(tmp, "a.png")
        sources.write(pic, sources.of_input("graph", "digraph { a -> b }"))
        self.valid(sources.read(pic))
        sources.write(pic, sources.of_input("graph", "x" * (sources.SOURCE_MAX + 1)))
        self.assertFalse(os.path.exists(pic + ".source.json"))  # and the old one is gone

    def test_for_bot_says_the_source_of_each_picture(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        r = subprocess.run([sys.executable, BIN, "-", "--for-bot", "-o", os.path.join(tmp, "tok.png")],
                           input=ANSWER.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        posts = json.loads(r.stdout.decode())
        for p in posts:
            self.assertTrue(set(p.get("sources", {})) <= set(p["images"]))
            for image in p["images"]:
                self.valid(p["sources"][image])
                self.assertEqual(sources.read(image), p["sources"][image])  # the sidecar says the same
        self.assertEqual(posts[0]["sources"][posts[0]["images"][0]]["source"],
                         "$$\\sigma(z)_i = \\frac{e^{z_i}}{\\sum_j e^{z_j}}$$")

    def test_pictures_next_to_each_other_keep_apart(self):
        # a table, an empty line, $$…$$ and a mermaid fence: one picture, its parts an empty line apart, or
        # markdown-it reads the $$ line as the table's next row
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        table, formula, chart = "| 項目 | 說明 |\n|---|---|\n| 甲 | 第一 |", "$$E=mc^2$$", "```mermaid\ngraph LR; A-->B\n```"
        for text in (table + "\n\n" + formula + "\n" + chart, table + "\n" + formula + "\n" + chart):
            self.assertEqual(kinds.split_segments(text), [("picture", "\n\n".join((table, formula, chart)))])
        r = subprocess.run([sys.executable, BIN, "-", "--for-bot", "-o", os.path.join(tmp, "gap.png")],
                           input=(table + "\n\n" + formula + "\n" + chart).encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        posts = json.loads(r.stdout.decode())
        self.assertEqual(len(posts), 1)
        self.assertEqual(sources.read(posts[0]["images"][0])["source"], "\n\n".join((table, formula, chart)))

    def test_a_cut_doc_has_the_whole_source_on_each(self):
        if tools.missing("doc"):
            self.skipTest("this machine lacks %s" % tools.missing("doc"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        text = "\n\n".join("## 第 %d 段\n\n" % i + "一段夠長的文字。" * 40 for i in range(12))
        r = subprocess.run([sys.executable, BIN, "-", "--kind", "doc", "-o", os.path.join(tmp, "d.png")],
                           input=text.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        outs = r.stdout.decode().split()
        self.assertGreater(len(outs), 1)
        for p in outs:
            self.assertEqual(sources.read(p), {"kind": "文件", "lang": "markdown", "tool": "markdown-it", "source": text})


if __name__ == "__main__":
    unittest.main()
