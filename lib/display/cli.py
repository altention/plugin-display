"""display: what Discord cannot show (math, tables, charts, diagrams, diffs, terminal colours, SVG/HTML/PDF) as PNGs,
posted to a channel when asked.

  display note.md --post <channel> --caption "…"     render, then altention dc post <channel> --file (each picture);
                                                       --post alone (or . or self) is $DCBOT_CHANNEL, this channel
  display answer.md --segments --post <channel>        an answer as text messages, with only its display math,
                                                       tables and mermaid fences as pictures, posted in order
  display - --for-bot -o <dir>/reply.png                the bot's hook: a whole reply in, the messages to post out
                                                       as JSON ([{"text", "images"}]); $x$ left as it is
  display -e '$e^{i\\pi}+1=0$'                         just the PNG; prints its path (one per line)
  some-command | display - --kind term                 from stdin; --kind when the input does not say what it is
  display --setup [browser|tectonic|plot|all]          get the tools into ~/.cache/altention-display (default: browser)
  display --check [chrome|poppler|cjk-font|venv]       what this machine has, and the kinds it can draw; with a
                                                       name, exit 0 when it has that one (plugin.json's needs)

Kinds (--kind; guessed from the file name or the input when left out):
  doc      Markdown: text, $math$ and $$display math$$, \\begin{align}, tables, ```mermaid fences (aliases: math);
           a .csv/.tsv file, or --kind table for CSV on stdin, is a table. Long ones are cut into several pictures.
  chart    a Vega-Lite spec (JSON) with its data in data.values
  diagram  Mermaid: flowchart, sequenceDiagram, gantt, timeline, mindmap, classDiagram, erDiagram, stateDiagram…
  graph    Graphviz DOT (digraph { a -> b })
  diff     a unified diff (git diff): red and green like GitHub; --side-by-side for two columns
  term     terminal output with ANSI colours
  image    an SVG or an HTML file (the HTML runs sandboxed: no files of this machine, no network, no going elsewhere)
  pdf      pages of a PDF (--pages 2-4; default the first three)
  tex      a LaTeX document body typeset by Tectonic: tikz, tabular, any package (--preamble '\\usepackage{tikz}')
  plot     a Python script drawing with matplotlib: the figures it leaves open (needs --setup plot)

Pictures are a dark card (Discord's dark theme), readable in the light theme too; --theme light for a white one.
"""
import argparse
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time

from . import confine, kinds, render, sources, tools
from .tools import Fail, say

MAX_HEIGHT = 1400  # CSS pixels: a long doc is cut into pictures about this tall
# the most pictures one post carries: the bot takes at most 10 files, 8 MB in all, in one message
FILES_MAX = 10
BYTES_MAX = 8 * 1024 * 1024


def altention():
    return shutil.which("altention") or os.path.expanduser("~/altention/engine/bin/altention")


def tidy():
    """Pictures older than a day in the default output folder go, with their sources: they were posted or
    forgotten."""
    cut = time.time() - 86400
    for p in glob.glob(os.path.join(tools.OUT, "display-*.png")) + glob.glob(os.path.join(tools.OUT, "display-*.png.*")):
        try:
            if os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass


# what --post takes for "this channel": the bot's conversations have its ID in $DCBOT_CHANNEL
SELF = ("", ".", "self")


def channel_of(value):
    """The channel --post names: `value`, or this conversation's channel by ID. An ID spares the bot looking the
    name up in the server's list of channels, which Discord allows only a few times a minute: past that every post
    waits up to a minute."""
    if value == "-" or os.path.isfile(value):  # --post took the input meant for after it: display --post a.md
        raise Fail("--post 後面是 %s，看起來是要畫的檔案，不是頻道：把檔案寫在 --post 前面（display a.md --post）" % value)
    if value.strip() not in SELF:
        return value
    if not os.environ.get("DCBOT_CHANNEL"):
        raise Fail("--post 沒寫頻道，而這裡沒有 $DCBOT_CHANNEL（不是機器人開的對話）：寫明頻道的 ID 或名稱")
    return os.environ["DCBOT_CHANNEL"]


def batches(pictures):
    """The pictures cut into runs one post can carry (FILES_MAX, BYTES_MAX), in order. A picture too big on its own
    still goes alone: the bot says why it was not posted."""
    out = []
    for p in pictures:
        size = os.path.getsize(p)
        if not out or len(out[-1]) >= FILES_MAX or sum(map(os.path.getsize, out[-1])) + size > BYTES_MAX:
            out.append([])
        out[-1].append(p)
    return out


def messages(plan):
    """[(caption, [pictures])] as the Discord messages to post, in order: a caption's pictures past what one
    message carries go on in the next ones, with no text."""
    return [(caption if i == 0 else "", run) for caption, pictures in plan
            for i, run in enumerate(batches(pictures) or [[]])]


def post(channel, caption, pictures=()):
    """The caption with the pictures under it: one message, or more when the pictures are too many for one (the
    caption goes with the first). The bot puts one sender's posts in a channel in the order sent. The exit status
    of altention dc post."""
    for text, run in messages([(caption, pictures)]):
        cmd = [altention(), "dc", "post", channel] + [x for p in run for x in ("--file", p)] + ([text] if text else [])
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=120)  # with no text it would read ours
        if r.returncode != 0:
            return r.returncode
    return 0


def segments(text, out, a):
    """display --segments: [(caption, [picture paths])], the pictures drawn in one go."""
    plan = kinds.posts(kinds.split_segments(text))
    found = [m for caption, _ in plan for m in kinds.inline_math(caption) or []]
    # Discord would show them as they are: nothing is drawn or posted until they are gone; but not for the bot,
    # whose reply is written already (stopping would post nothing at all)
    if found and not a.for_bot:
        raise Fail("文字裡有 Discord 不會排的公式：%s。簡單的改成 Unicode 寫在文字裡（x²、xᵢ、α、∇、≤），"
                   "複雜的寫成獨立一行的 $$…$$（會畫成圖），再跑一次" % "、".join(found[:5]))
    pictures = [p for _, ps in plan for p in ps]
    if not pictures:
        if not a.for_bot:  # said to Claude; the bot's log needs nothing here
            say("這份回答沒有要畫的（公式、表格、流程圖）：直接寫成文字回覆就好")
        return plan
    paths = render.shoot_segments(kinds.segments_page(pictures, a.theme, a.width), out, a.scale)
    for i, path in enumerate(paths):
        h = png_height(path) / a.scale
        if h > MAX_HEIGHT and a.for_bot:
            say("第 %d 張圖高 %d px，超過 %d" % (i + 1, h, MAX_HEIGHT))
        elif h > MAX_HEIGHT:
            say("第 %d 張圖高 %d px，手機上會縮成看不清的細條：大表改附 CSV 檔（altention dc post … --file），"
                "圖只放前幾列" % (i + 1, h))
    for path, chunk in zip(paths, pictures):
        sources.write(path, sources.of_markdown(chunk))
    drawn = iter(paths)
    return [(c, [next(drawn) for _ in ps]) for c, ps in plan]


def said(pictures):
    """{"sources": {picture: its source}} for the pictures that have one, for the bot; {} when none has."""
    found = {p: sources.read(p) for p in pictures}
    return {"sources": {p: s for p, s in found.items() if s}} if any(found.values()) else {}


def png_height(path):
    with open(path, "rb") as f:
        return struct.unpack(">I", f.read(24)[20:24])[0]


def read_input(a, ap):
    """(text or None for a binary file, file path or None, name used to guess the kind)."""
    if a.expr is not None:
        return a.expr, None, None
    if a.file in (None, "-"):
        if a.file is None and sys.stdin.isatty():
            ap.error("給一個檔案、- 或 -e '…'")
        return sys.stdin.read(), None, None
    if not os.path.isfile(a.file):
        raise Fail("找不到檔案：%s" % a.file)
    with open(a.file, "rb") as f:
        data = f.read()
    if data.startswith(b"%PDF"):
        return None, a.file, a.file
    return data.decode("utf-8", "replace"), a.file, a.file


def draw(kind, text, path, name, out, a):
    if kind == "pdf":
        return kinds.pdf_pages(path, out, a.scale, a.pages)
    if kind == "tex":
        return kinds.tex(text, out, a.theme, a.width, a.scale, a.preamble)
    if kind == "plot":
        if not path:
            raise Fail("plot 要一個 .py 檔")
        return kinds.plot(path, out, a.theme, a.scale)
    if kind == "diff":
        built = kinds.diff(text, a.theme, a.width, name, side_by_side=a.side_by_side)
    elif kind == "doc":
        built = kinds.doc(text, a.theme, a.width, name, table=a.kind in ("table", "csv", "tsv"))
    else:
        built = getattr(kinds, kind)(text, a.theme, a.width, name)
    if isinstance(built, dict):  # an HTML page in its frame: checked to still be our page; no room past it
        return render.shoot(built["page"], out, a.scale, max_height=MAX_HEIGHT, mark=built["mark"], slack=(0, 0))
    return render.shoot(built, out, a.scale, max_height=MAX_HEIGHT if kind in ("doc", "term", "diff") else None)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="display", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[1:]))
    ap.add_argument("file", nargs="?", help="input file, or - for stdin")
    ap.add_argument("-e", "--expr", help="the input as an argument instead of a file")
    ap.add_argument("-k", "--kind", help="|".join(kinds.KINDS) + " (and math, table, mermaid, dot…)")
    ap.add_argument("-o", "--out", help="where to write the PNG (default: ~/.cache/altention-display/out/…)")
    ap.add_argument("--theme", choices=sorted(render.THEMES), default="dark")
    ap.add_argument("--width", type=int, default=720, help="widest a text card gets, in CSS pixels (default 720)")
    ap.add_argument("--scale", type=float, default=2.0, help="pixels per CSS pixel (default 2)")
    ap.add_argument("--side-by-side", action="store_true", help="diff: two columns")
    ap.add_argument("--pages", help="pdf: which pages, e.g. 2-4 (default 1-3)")
    ap.add_argument("--preamble", default="", help="tex: extra preamble, e.g. \\usepackage{tikz}")
    ap.add_argument("--post", metavar="CHANNEL", nargs="?", const=".",
                    help="post the pictures there with altention dc post --file; alone, . or self: $DCBOT_CHANNEL")
    ap.add_argument("--caption", default="", help="the text posted with the (first) picture")
    ap.add_argument("--segments", action="store_true",
                    help="Markdown: text as messages, only display math, tables and mermaid as pictures, in order")
    ap.add_argument("--json", action="store_true",
                    help='print the messages to post as JSON, [{"text": …, "images": [paths]}], and nothing else')
    ap.add_argument("--for-bot", action="store_true",
                    help="for the bot, before it posts a reply: --segments --json, $x$ left in the text as it is")
    ap.add_argument("--setup", nargs="?", const="browser", choices=["browser", "tectonic", "plot", "all"])
    ap.add_argument("--check", nargs="?", const="", choices=[""] + sorted(tools.SYSTEM),
                    help="alone: what this machine has; with a name: exit 0 when it has that (plugin.json's needs)")
    a = ap.parse_args(argv)
    try:
        if a.setup:
            tools.setup(["browser", "tectonic", "plot"] if a.setup == "all" else [a.setup])
            tools.check()
            return 0
        if a.check:
            return tools.check_one(a.check)
        if a.check == "":
            tools.check()
            return 0
        if a.for_bot:
            if a.post is not None:
                raise Fail("--for-bot 只交回 JSON，由機器人貼：不能加 --post")
            if a.kind not in (None, "doc"):
                raise Fail("--for-bot 只收整份回覆（doc），不能加 --kind %s" % a.kind)
            a.segments, a.json, a.kind = True, True, "doc"
        text, path, name = read_input(a, ap)
        if text is not None and not text.strip():
            raise Fail("沒有內容")
        if a.post is not None:  # before anything is drawn: a channel it cannot name stops it here
            a.post = channel_of(a.post)
        kind = kinds.ALIASES.get(a.kind, a.kind) if a.kind else kinds.detect(text, name)
        if kind not in kinds.KINDS:
            raise Fail("沒有這種 --kind：%s（%s）" % (a.kind, "、".join(kinds.KINDS)))
        if text is None and kind != "pdf":
            raise Fail("這個檔案不是文字（只有 pdf 可以是二進位檔）")
        if kind == "pdf" and path is None:
            raise Fail("pdf 要給檔案（display a.pdf），不能用 -e 或 stdin")
        if a.segments and kind != "doc":
            raise Fail("--segments 只用在 Markdown 的回答（doc），這是 %s" % kind)
        need = tools.missing(kind)
        if "sandbox" in need:
            raise Fail(confine.WHY)
        if need:
            raise Fail(tools.missing_said(kind, need))
        if a.out:
            out = os.path.abspath(a.out if a.out.endswith(".png") else a.out + ".png")
            os.makedirs(os.path.dirname(out), exist_ok=True)  # Chrome cannot write into a folder not there
        else:
            os.makedirs(tools.OUT, exist_ok=True)
            tidy()
            out = os.path.join(tools.OUT, "display-%s-%d.png" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        if a.segments:
            plan = segments(text, out, a)
            if a.json and not any(ps for _, ps in plan):
                plan = [(text, [])]  # nothing drawn: the text as it came, for the bot to post as it would have
            for caption, ps in [] if a.json else plan:
                print("（文字）" + (caption.split("\n")[0][:60] or "（沒有）") + "".join("  " + p for p in ps))
        else:
            outs = draw(kind, text, path, name, out, a)
            info = sources.of_input(kind, text, name, table=a.kind in ("table", "csv", "tsv"), preamble=a.preamble)
            for p in outs:  # a doc cut into several pictures: each has the whole source
                sources.write(p, info)
            for p in [] if a.json else outs:
                print(p)
            plan = []
            for i, p in enumerate(outs):
                caption = a.caption if i == 0 else ""
                if len(outs) > 1:
                    caption = ("%s（%d/%d）" % (caption, i + 1, len(outs))).strip()
                plan.append((caption, [p]))
        if a.json:
            print(json.dumps([dict({"text": t, "images": run}, **said(run)) for t, run in messages(plan)],
                             ensure_ascii=False))
        if a.post is not None:
            for caption, ps in plan:
                code = post(a.post, caption, ps)
                if code != 0:
                    return code
        return 0
    except Fail as e:
        say(str(e))
        return 2
    except subprocess.TimeoutExpired as e:
        say("逾時：%s" % " ".join(map(str, e.cmd[:1])))
        return 3
