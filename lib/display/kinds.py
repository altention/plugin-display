"""Each kind of picture: what its input looks like, and how it is drawn.

  doc      Markdown with math ($…$, $$…$$, \\[…\\], \\begin{align}…), tables and ```mermaid fences (also: math, table)
  chart    a Vega-Lite spec (JSON)
  diagram  Mermaid (flowchart, sequenceDiagram, gantt, timeline, mindmap, classDiagram, erDiagram, stateDiagram…)
  graph    Graphviz DOT
  diff     a unified diff (git diff), drawn the way GitHub does
  term     terminal output with ANSI colours
  image    an SVG or HTML file as a PNG
  pdf      pages of a PDF
  tex      a LaTeX document body, typeset by Tectonic (tikz, tabular, any package)
  plot     a Python script drawing with matplotlib (the figures it leaves open)
"""
import base64
import csv
import glob
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import confine, render
from .render import MONO, SANS, SERIF, THEMES, font_stack, page
from .tools import TOOLS, Fail, asset, say, tectonic_bin, tectonic_cache, venv_python

KINDS = ["doc", "chart", "diagram", "graph", "diff", "term", "image", "pdf", "tex", "plot"]
ALIASES = {"math": "doc", "table": "doc", "csv": "doc", "tsv": "doc", "md": "doc", "markdown": "doc", "text": "doc", "mermaid": "diagram",
           "dot": "graph", "graphviz": "graph", "vega": "chart", "vega-lite": "chart", "ansi": "term",
           "svg": "image", "html": "image", "latex": "tex", "matplotlib": "plot"}
EXT = {".md": "doc", ".markdown": "doc", ".txt": "doc", ".csv": "doc", ".tsv": "doc", ".mmd": "diagram",
       ".mermaid": "diagram", ".dot": "graph", ".gv": "graph", ".diff": "diff", ".patch": "diff", ".ansi": "term",
       ".log": "term", ".svg": "image", ".html": "image", ".htm": "image", ".pdf": "pdf", ".tex": "tex",
       ".py": "plot", ".vl.json": "chart", ".json": "chart"}
MERMAID_START = re.compile(
    r"^\s*(%%.*\n\s*)*(---[\s\S]*?---\s*)?(flowchart|graph\s+(TD|TB|BT|RL|LR)|sequenceDiagram|classDiagram|"
    r"stateDiagram(-v2)?|erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|C4\w+|mindmap|"
    r"timeline|sankey(-beta)?|xychart(-beta)?|block(-beta)?|packet(-beta)?|kanban|architecture(-beta)?|radar"
    r"(-beta)?|treemap(-beta)?)\b")
DOT_START = re.compile(r"^\s*(//.*\n\s*|/\*[\s\S]*?\*/\s*|#.*\n\s*)*(strict\s+)?(di)?graph\s*(\w+|\"[^\"]*\")?\s*\{",
                       re.I)


def detect(text, name=None):
    """The kind of an input nobody named: by its file name, else by what it starts with; doc when nothing fits."""
    if name:
        low = name.lower()
        for ext in sorted(EXT, key=len, reverse=True):
            if low.endswith(ext):
                if ext == ".json" and not looks_vega(text):
                    break
                return EXT[ext]
    if text is None:
        return "doc"
    head = text.lstrip()[:2000]
    if head.startswith("%PDF"):
        return "pdf"
    if looks_vega(text):
        return "chart"
    if DOT_START.match(head):
        return "graph"
    if MERMAID_START.match(head):
        return "diagram"
    if re.match(r"(diff --git |--- \S.*\n\+\+\+ \S)", head) or re.search(r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", head, re.M):
        return "diff"
    if "\x1b[" in head:
        return "term"
    if re.match(r"<\?xml[^>]*>\s*<svg|<svg[\s>]", head, re.I):
        return "image"
    if re.match(r"<!doctype html|<html[\s>]", head, re.I):
        return "image"
    return "doc"


def looks_vega(text):
    try:
        spec = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(spec, dict) and ("vega" in str(spec.get("$schema", "")) or
                                       any(k in spec for k in ("mark", "layer", "facet", "hconcat", "vconcat",
                                                               "concat", "repeat")))


def script_json(value):
    """A value as JavaScript, safe inside <script> (no </script> or <!-- can end it)."""
    return json.dumps(value).replace("</", "<\\/").replace("<!--", "<\\!--")


def link(name, path):
    return '<link rel="stylesheet" href="%s">' % asset(name, path)


def src(name, path):
    return '<script src="%s"></script>' % asset(name, path)


# ---------- doc: Markdown, math, tables, mermaid fences ----------

def csv_to_markdown(text, name=None, table=False):
    """A CSV/TSV input (its name ends .csv/.tsv, or `table`: --kind table) as a Markdown table; else the text.
    Nothing else is guessed: prose with a comma on every line is still prose."""
    first = text.strip().split("\n", 1)[0]
    is_csv = (name or "").lower().endswith((".csv", ".tsv")) or (table and "|" not in first)
    if not is_csv:
        return text
    delim = "\t" if "\t" in first or (name or "").lower().endswith(".tsv") else ","
    rows = [r for r in csv.reader(io.StringIO(text.strip()), delimiter=delim)]
    if not rows:
        return text
    cell = lambda c: c.replace("|", "\\|").replace("\n", " ")
    width = max(len(r) for r in rows)
    out = ["| " + " | ".join(cell(c) for c in rows[0] + [""] * (width - len(rows[0]))) + " |",
           "|" + "|".join("---:" if all(re.match(r"^-?[\d.,%]+$", (r[i] if i < len(r) else "") or "0")
                                         for r in rows[1:]) else "---" for i in range(width)) + "|"]
    out += ["| " + " | ".join(cell(c) for c in r + [""] * (width - len(r))) + " |" for r in rows[1:]]
    return "\n".join(out)


DOC_JS = r"""
var md = markdownit({html: false, linkify: false, breaks: true, typographer: false});
var MATH = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\\begin\{(equation|align|alignat|gather|multline|CD)(\*?)\}[\s\S]+?\\end\{\1\2\}|\$(?=[^\s$])[^$\n]*?[^\s\\$]\$(?!\d)|\$[^\s$\\]\$(?!\d)/g;
function render(src) {
// code (fenced or inline) is left alone; elsewhere \$ is a dollar sign and math is set aside before Markdown sees it
var math = [], parts = src.split(/(^```[\s\S]*?(?:^```|(?![\s\S]))|^~~~[\s\S]*?(?:^~~~|(?![\s\S]))|`[^`\n]+`)/m);
for (var i = 0; i < parts.length; i += 2) {
  parts[i] = parts[i].replace(/\\\$/g, "\uE000").replace(MATH, function (m) {
    math.push(m);
    return "\uE001" + (math.length - 1) + "\uE002";
  });
}
var out = md.render(parts.join(""));
out = out.replace(/\uE001(\d+)\uE002/g, function (_, n) {
  var tex = math[+n], display = true, body = tex;
  if (/^\$\$/.test(tex)) body = tex.slice(2, -2);
  else if (/^\\\[/.test(tex)) body = tex.slice(2, -2);
  else if (/^\\\(/.test(tex)) { body = tex.slice(2, -2); display = false; }
  else if (/^\$/.test(tex)) { body = tex.slice(1, -1); display = false; }
  try {
    return katex.renderToString(body, {displayMode: display, throwOnError: true, strict: "ignore", trust: false});
  } catch (e) {
    D.errors.push("公式錯誤：" + (e.message || e) + "（在 " + tex.slice(0, 60) + "）");
    return "";
  }
}).replace(/\uE000/g, "$");
return out;
}
// a card that is a single formula and nothing else: less room around it
function solo(el) {
  if (el.children.length === 1 && el.children[0].tagName === "P" && el.children[0].children.length === 1 &&
      el.children[0].children[0].classList.contains("katex-display") && !el.children[0].textContent.trim().replace(
        el.children[0].children[0].textContent, "").trim()) el.classList.add("solo");
}
if (typeof SRCS !== "undefined") {  // display --segments: each a card of its own, drawn one picture each
  C.innerHTML = SRCS.map(function (s) { return '<div class="seg">' + render(s) + "</div>"; }).join("");
  Array.prototype.forEach.call(C.children, solo);
  D.segments = function () { return Array.prototype.slice.call(C.children); };
  // the picture of one of them: the others out of the page, so nothing above it can move it
  if (D.params.only) Array.prototype.forEach.call(C.children, function (el, i) {
    if (i !== +D.params.only - 1) el.style.display = "none";
  });
} else {
  C.innerHTML = render(SRC);
  solo(C);
}
// what looks like math left as text: a $ next to \ ^ _ { or a \( \[ with no partner
var walk = document.createTreeWalker(C, NodeFilter.SHOW_TEXT), node;
while ((node = walk.nextNode())) {
  if (node.parentElement.closest(".katex, code, pre")) continue;
  var m = node.nodeValue.match(/\$\s*\\[a-zA-Z]|\$[^$\n]*[\^_{]|\\[(\[]/);
  if (m) D.errors.push("看起來是沒配對的公式（錢號本身寫成 \\$）：…" + node.nodeValue.substr(Math.max(0, m.index - 20), 60) + "…");
}
// whole words in tables, unless a word longer than a column can be (a hash, a URL, a path) would make the table
// wider than TABLE_MAX: then that table breaks anywhere, as it must to fit
C.querySelectorAll("table").forEach(function (t) {
  if (t.getBoundingClientRect().width <= TABLE_MAX + 1) return;
  // first only the cells with such a long word in them, then (still too wide) the whole table
  t.querySelectorAll("th, td").forEach(function (c) {
    if (/\S{25,}/.test(c.textContent)) c.classList.add("squeeze");
  });
  if (t.getBoundingClientRect().width > TABLE_MAX + 1) t.classList.add("squeeze");
});
var fences = C.querySelectorAll("pre > code.language-mermaid");
if (fences.length && window.mermaid) {
  fences.forEach(function (code) {
    var div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = MERMAID_SRC(code.textContent);
    code.parentNode.replaceWith(div);
  });
  MERMAID_INIT();
  mermaid.run({querySelector: ".mermaid"}).then(function () { D.finish(); }, D.fail);
} else {
  D.finish();
}
"""

# the widest a table gets, in CSS pixels: no wider than the text. On a phone a picture is shown about 330 points
# wide, and a 1100-wide table is unreadable there (720 is about the limit); a table with more than that needs
# goes out as a CSV file as well
TABLE_MAX = 720

DOC_CSS = """
#c > :not(table) { max-width: %(width)dpx; } table { max-width: %(table)dpx; }
table.squeeze, td.squeeze, th.squeeze { overflow-wrap: anywhere; }
#c > :first-child { margin-top: 0; } #c > :last-child { margin-bottom: 0; }
p, ul, ol, table, pre, blockquote, .katex-display, .mermaid { margin: 0 0 12px; }
h1, h2, h3, h4 { margin: 18px 0 10px; line-height: 1.3; } h1 { font-size: 1.5em; } h2 { font-size: 1.3em; }
h3 { font-size: 1.12em; } h1, h2 { border-bottom: 1px solid %(line)s; padding-bottom: 4px; }
ul, ol { padding-left: 1.6em; } li + li { margin-top: 3px; }
a { color: %(accent)s; text-decoration: none; }
code { font-family: %(mono)s; font-size: .88em; background: %(code)s; padding: 1px 5px; border-radius: 4px; }
pre { background: %(code)s; padding: 10px 14px; border-radius: 6px; white-space: pre-wrap; }
pre code { background: none; padding: 0; font-size: .85em; }
blockquote { border-left: 4px solid %(line)s; padding-left: 12px; color: %(muted)s; }
table { border-collapse: collapse; border-top: 2px solid %(fg)s; border-bottom: 2px solid %(fg)s; }
th, td { padding: 5px 14px; } th { border-bottom: 1px solid %(fg)s; font-weight: bold; }
tr:nth-child(even) td { background: rgba(127,127,127,.08); }
hr { border: 0; border-top: 1px solid %(line)s; margin: 16px 0; }
.katex { font-size: 1.12em; } .katex-display { overflow: hidden; }
#c.solo, .seg.solo { padding: 10px 18px; } .solo .katex-display { margin: 0; } .solo > p { margin: 0; }
.mermaid { text-align: center; overflow-wrap: normal; line-height: 1.2; font-family: %(sans)s; }
.mermaid svg { max-width: 100%%; height: auto; }
"""

MERMAID_JS = r"""
function MERMAID_INIT() {
  mermaid.initialize({startOnLoad: false, securityLevel: "strict", theme: THEME === "dark" ? "dark" : "default",
                      fontFamily: FONT, themeVariables: {fontFamily: FONT, fontSize: "16px", background: BG},
                      // a gantt is as wide as its container: give it room and short dates so the axis stays readable
                      gantt: {useWidth: 900, axisFormat: "%m-%d", barHeight: 26, fontSize: 14, sectionFontSize: 15}});
}
function MERMAID_SRC(text) {  // a gantt's red "today" line says nothing about the plan drawn: off unless asked for
  var lines = text.split("\n"), i = 0;
  while (i < lines.length && !lines[i].trim()) i++;
  if (i < lines.length && lines[i].trim() === "---") {  // front matter
    for (i++; i < lines.length && lines[i].trim() !== "---"; i++);
    i++;
  }
  while (i < lines.length && (!lines[i].trim() || /^\s*%%/.test(lines[i]))) i++;  // %%{init}%%, comments
  if (i >= lines.length || lines[i].trim() !== "gantt" || /^\s*todayMarker\b/m.test(text)) return text;
  lines.splice(i + 1, 0, "  todayMarker off");
  return lines.join("\n");
}
function MERMAID_SIZE() {  // the natural size, not squeezed into the card's width
  document.querySelectorAll(".mermaid svg").forEach(function (svg) {
    var vb = svg.viewBox && svg.viewBox.baseVal;
    if (vb && vb.width) { svg.setAttribute("width", vb.width); svg.setAttribute("height", vb.height); }
    svg.style.maxWidth = "none";
  });
}
"""


def theme_js(theme):
    t = THEMES[theme]
    return "var THEME = %s, FONT = %s, BG = %s, FG = %s;\n" % (
        script_json(theme), script_json(font_stack()), script_json(t["bg"]), script_json(t["fg"]))


def doc(text, theme, width, name=None, table=False):
    text = csv_to_markdown(text, name, table)
    t = THEMES[theme]
    head = (link("katex", "dist/katex.min.css") + src("katex", "dist/katex.min.js") +
            src("katex", "dist/contrib/mhchem.min.js") + src("markdown-it", "dist/markdown-it.min.js"))
    if re.search(r"^(```|~~~)\s*mermaid", text, re.M):
        head += src("mermaid", "dist/mermaid.min.js")
    css = DOC_CSS % dict(t, mono=MONO, sans=font_stack(SANS), width=width, table=max(width, TABLE_MAX))
    script = (theme_js(theme) + MERMAID_JS + "var SRC = %s, TABLE_MAX = %d;\n" % (script_json(text), max(width, TABLE_MAX))
              + DOC_JS)
    return page("", theme, css=css, head=head, script=script, font=font_stack(SERIF))


# ---------- segments: the text of an answer as text, only what Discord cannot show as pictures ----------

SEG_CSS = """
#c { padding: 0; }
.seg { width: fit-content; max-width: %(width)dpx; padding: 18px 22px; }
.seg > :first-child { margin-top: 0; } .seg > :last-child { margin-bottom: 0; }
"""
# the start of a block drawn as a picture: display math, a table, a mermaid fence
MATH_ENVS = r"equation|align|alignat|gather|multline|CD"
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# a caption is posted as a Discord message: at most this many characters each (Discord's limit is 2000)
CAPTION_MAX = 1900


def split_segments(text):
    """An answer in Markdown as [("text", …) | ("picture", …)] in order: display math ($$…$$, \\[…\\],
    \\begin{align}…), tables and ```mermaid fences are pictures (those next to each other one picture); the rest,
    code blocks included, is text."""
    lines, out, i = text.replace("\r\n", "\n").split("\n"), [], 0

    def add(kind, chunk):
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + "\n" + chunk)
        else:
            out.append((kind, chunk))

    while i < len(lines):
        line, s = lines[i], lines[i].strip()
        end = None
        fence = re.match(r"^(```|~~~)\s*(\S*)", s)
        if fence:
            j = next((k for k in range(i + 1, len(lines)) if lines[k].strip().startswith(fence.group(1))), len(lines) - 1)
            kind = "picture" if fence.group(2).lower() == "mermaid" else "text"
            add(kind, "\n".join(lines[i:j + 1]))
            i = j + 1
            continue
        if s.startswith("$$"):
            end = i if s.count("$$") >= 2 and len(s) > 2 else next(
                (k for k in range(i + 1, len(lines)) if "$$" in lines[k]), None)
        elif s.startswith("\\["):
            end = next((k for k in range(i, len(lines)) if "\\]" in lines[k]), None)
        else:
            env = re.match(r"^\\begin\{(%s)(\*?)\}" % MATH_ENVS, s)
            if env:
                close = "\\end{%s%s}" % (env.group(1), env.group(2))
                end = next((k for k in range(i, len(lines)) if close in lines[k]), None)
            elif s.startswith("|") and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
                end = i + 1
                while end + 1 < len(lines) and lines[end + 1].strip().startswith("|"):
                    end += 1
        if end is not None:
            add("picture", "\n".join(lines[i:end + 1]))
            i = end + 1
            continue
        if s or not out or out[-1][0] != "picture":  # blank lines between two pictures keep them one picture
            add("text", line)
        i += 1
    # a blank line after a picture, kept above, starts the next text: no text of blank lines only
    return [(k, c.strip("\n")) for k, c in out if c.strip()]


FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def open_fence(text):
    """(the opening line, its marker) of a ``` block `text` leaves open at its end, else None."""
    fence = None
    for line in text.split("\n"):
        m = FENCE.match(line)
        if fence is None and m:
            fence = (line.strip(), m.group(1))
        elif fence and m and line.strip() == m.group(1) and m.group(1).startswith(fence[1]):
            fence = None
    return fence


def split_caption(text, limit=CAPTION_MAX):
    """Text in pieces of at most `limit` characters, cut between paragraphs, else lines, else anywhere. A cut inside
    a ``` block closes it at the end of the piece and opens it again (with its language) at the start of the next,
    as the bot does with a long reply, so each piece shows as code; the code itself is kept line for line."""
    pieces, rest, reopen = [], text.strip(), ""
    while len(reopen) + len(rest) > limit:
        room = limit - len(reopen) - 5  # and a closing "\n````" if the cut falls in a block
        cut = max(rest.rfind("\n\n", 0, room), rest.rfind("\n", 0, room))
        cut = cut if cut > room // 3 else room
        fence = open_fence(reopen + rest[:cut])
        if fence:  # in a block: cut at a line, every line kept as it is
            cut = rest.rfind("\n", 0, room)
            cut = cut if cut > room // 3 else room
            piece, rest = reopen + rest[:cut], rest[cut + 1 if rest[cut:cut + 1] == "\n" else cut:]
            fence = open_fence(piece)
        else:
            piece, rest = reopen + rest[:cut].rstrip(), rest[cut:].lstrip("\n")
        pieces.append(piece + ("\n" + fence[1] if fence else ""))
        reopen = fence[0] + "\n" if fence else ""
    return pieces + ([reopen + rest] if rest.strip() else [])


def posts(segments):
    """[(caption, [picture sources])] to post in order. Discord shows a message's text above its pictures, so each
    text goes with the pictures right after it; pictures with no text before them go in one post of their own, and
    so does the text after the last picture. Text longer than CAPTION_MAX is cut: the pieces go on their own and the last one
    takes the pictures, so they still come right after the text before them."""
    plan = []
    for kind, chunk in segments:
        if kind == "picture" and plan:
            plan[-1][1].append(chunk)
        elif kind == "picture":
            plan.append(["", [chunk]])
        else:
            plan.append([chunk, []])
    out = []
    for caption, pictures in plan:
        pieces = split_caption(caption) or [""]
        out += [(p, []) for p in pieces[:-1]]
        out.append((pieces[-1], pictures))
    return out


# math left in a caption: $…$ (not a price: no digit or space right after the first $, no space before the last,
# no digit after it), $$…$$ inside a line, \(…\)
INLINE_MATH = re.compile(r"\$\$[^$\n]+?\$\$|(?<![\\$])\$(?=[^\s\d$])(?:[^$\n]*?[^\s$\\])?\$(?![\d$])|\\\(.+?\\\)")


def inline_math(text):
    """The $…$ (or \\(…\\)) in a caption: Discord shows them as they are."""
    bare = re.sub(r"```[\s\S]*?```|`[^`\n]+`", "", text)
    return INLINE_MATH.findall(bare) and [m.group(0) for m in INLINE_MATH.finditer(bare)]


def segments_page(pictures, theme, width):
    """One page with a card for each picture source (Markdown: math, tables, mermaid), drawn one picture each."""
    t = THEMES[theme]
    head = (link("katex", "dist/katex.min.css") + src("katex", "dist/katex.min.js") +
            src("katex", "dist/contrib/mhchem.min.js") + src("markdown-it", "dist/markdown-it.min.js"))
    if any(re.search(r"^(```|~~~)\s*mermaid", p, re.M) for p in pictures):
        head += src("mermaid", "dist/mermaid.min.js")
    css = DOC_CSS % dict(t, mono=MONO, sans=font_stack(SANS), width=width, table=max(width, TABLE_MAX))
    css += SEG_CSS % dict(width=width)
    script = (theme_js(theme) + MERMAID_JS + "var SRCS = %s, TABLE_MAX = %d;\n" % (script_json(pictures),
              max(width, TABLE_MAX)) + DOC_JS)
    return page("", theme, css=css, head=head, script=script, font=font_stack(SERIF))


# ---------- chart: Vega-Lite ----------

CHART_JS = r"""
var STEP = 40, FIT_MIN = 480, FIT_MAX = 720;
var config = {background: BG, font: FONT, padding: 8,
              view: {stroke: null, continuousWidth: 480, continuousHeight: 280},
              axis: {labelColor: FG, titleColor: FG, gridColor: LINE, domainColor: MUTED, tickColor: MUTED,
                     labelFontSize: 13, titleFontSize: 14},
              legend: {labelColor: FG, titleColor: FG, labelFontSize: 13, titleFontSize: 14},
              header: {labelColor: FG, titleColor: FG}, title: {color: FG, subtitleColor: MUTED, fontSize: 16},
              text: {color: FG}};
var spec = SPEC;
// marks are cut at the plot's edge: an axis that does not start at 0 then cuts the bars, not the picture
(function clip(s) {
  if (!s || typeof s !== "object") return;
  if (s.mark) {
    if (typeof s.mark === "string") s.mark = {type: s.mark};
    if (s.mark.clip === undefined) s.mark.clip = true;
  }
  ["layer", "hconcat", "vconcat", "concat"].forEach(function (k) { (s[k] || []).forEach(clip); });
  if (s.spec) clip(s.spec);
})(spec);
// a discrete x is 20px a value by default (three months: a line 60px long). With no width given it is laid out
// by its cells (a step each, so a month of a year of days is one cell), then held between FIT_MIN and FIT_MAX
var enc = spec.encoding || (spec.layer && spec.layer[0] && spec.layer[0].encoding) || {};
var fit = spec.width === undefined && enc.x && /^(nominal|ordinal)$/.test(enc.x.type) &&
          !(spec.hconcat || spec.vconcat || spec.concat || spec.facet || spec.repeat || enc.column);
if (fit) spec.width = {step: STEP};
function draw() { return vegaEmbed("#v", spec, {actions: false, renderer: "svg", config: config}); }
function refit(r) {
  var x = null;
  try { x = fit && r.view.scale("x"); } catch (e) {}
  if (!x || !x.domain) return D.finish();
  var cells = x.domain().length, w = r.view.width(), want = Math.min(Math.max(w, FIT_MIN), FIT_MAX);
  // and with room, its labels upright
  var upright = want / Math.max(1, cells) >= 60 && !(enc.x.axis && "labelAngle" in enc.x.axis);
  if (want === w && !upright) return D.finish();
  r.view.finalize();
  spec.width = want;
  if (upright) enc.x.axis = Object.assign({labelAngle: 0}, enc.x.axis || {});
  fit = false;
  draw().then(function () { D.finish(); }, D.fail);
}
if (spec.data && spec.data.url) D.fail("資料要直接寫在 spec 的 data.values 裡（這裡不能讀檔案或網址）");
else draw().then(refit, D.fail);
"""


def chart(text, theme, width, name=None):
    try:
        spec = json.loads(text)
    except ValueError as e:
        raise Fail("不是合法的 JSON（Vega-Lite spec）：%s" % e)
    t = THEMES[theme]
    head = src("vega", "build/vega.min.js") + src("vega-lite", "build/vega-lite.min.js") + src("vega-embed", "build/vega-embed.min.js")
    script = (theme_js(theme) + "var LINE = %s, MUTED = %s, SPEC = %s;\n" % (
        script_json("#3f4147" if theme == "dark" else "#e3e5e8"), script_json(t["muted"]), script_json(spec)) + CHART_JS)
    return page('<div id="v"></div>', theme, head=head, script=script)


# ---------- diagram: Mermaid ----------

def diagram(text, theme, width, name=None):
    head = src("mermaid", "dist/mermaid.min.js")
    body = '<pre class="mermaid">%s</pre>' % html.escape(text, quote=False)
    script = theme_js(theme) + MERMAID_JS + r"""
MERMAID_INIT();
var pre = document.querySelector(".mermaid");
pre.textContent = MERMAID_SRC(pre.textContent);
mermaid.parse(pre.textContent).then(function () {
  return mermaid.run({querySelector: ".mermaid"});
}).then(function () { MERMAID_SIZE(); D.finish(); }, D.fail);
"""
    return page(body, theme, css=".mermaid { margin: 0; overflow-wrap: normal; line-height: 1.2; }", head=head,
                script=script)


# ---------- graph: Graphviz DOT ----------

def graph(text, theme, width, name=None):
    head = src("@viz-js/viz", "dist/viz-global.js")
    script = theme_js(theme) + "var SRC = %s;\n" % script_json(text) + r"""
Viz.instance().then(function (viz) {
  var f = FONT.replace(/"/g, "");
  var r = viz.render(SRC, {format: "svg",
                           graphAttributes: {bgcolor: "transparent", fontcolor: FG, fontname: f},
                           nodeAttributes: {color: FG, fontcolor: FG, fontname: f},
                           edgeAttributes: {color: FG, fontcolor: FG, fontname: f}});
  if (r.status !== "success") {
    D.fail((r.errors || []).map(function (e) { return e.message; }).join("；") || "DOT 有錯");
    return;
  }
  C.innerHTML = r.output.replace(/^[\s\S]*?(<svg)/, "$1");
  D.finish();
}, D.fail);
"""
    return page("", theme, head=head, script=script)


# ---------- diff: unified diff the GitHub way ----------

def diff(text, theme, width, name=None, side_by_side=False):
    head = link("diff2html", "bundles/css/diff2html.min.css") + src("diff2html", "bundles/js/diff2html.min.js")
    w = width if width != 720 else (1400 if side_by_side else 960)
    css = """
#c { width: %dpx; padding: 16px; }
.d2h-wrapper, .d2h-file-header { font-family: %s; }
.d2h-code-line, .d2h-code-side-line, .d2h-code-linenumber, .d2h-code-side-linenumber { font-family: %s; }
.d2h-file-diff, .d2h-file-side-diff { overflow: visible; }
""" % (w, font_stack(), MONO + ", " + font_stack())
    if side_by_side:  # the two sides are separate tables: a wrapped line would put them out of step, so widen
        css += "#c { width: max-content; } .d2h-files-diff { width: max-content; }"
    else:  # long lines wrap, the + or - kept in front
        css += (".d2h-code-line { display: flex; width: auto; padding-right: 8px; } "
                ".d2h-code-line-ctn { white-space: pre-wrap; word-break: break-all; }")
    script = theme_js(theme) + "var SRC = %s, SIDE = %s;\n" % (script_json(text), script_json(side_by_side)) + r"""
var out = Diff2Html.html(SRC, {drawFileList: false, matching: "lines", outputFormat: SIDE ? "side-by-side" : "line-by-line",
                              colorScheme: THEME === "dark" ? "dark" : "light"});
if (!/d2h-file-wrapper/.test(out)) D.fail("看不懂這份 diff（要 unified diff，例如 git diff 的輸出）");
else { C.innerHTML = out; D.finish(); }
"""
    return page("", theme, css=css, head=head, script=script)


# ---------- term: ANSI colours ----------

ANSI_16 = {"dark": ["#4e5058", "#f23f43", "#23a55a", "#f0b232", "#5865f2", "#eb459e", "#00a8fc", "#dbdee1",
                    "#80848e", "#ff6b6e", "#57f287", "#fee75c", "#8c96ff", "#ff7ac3", "#5bd0ff", "#ffffff"],
           "light": ["#1a1a1a", "#d22d39", "#1a7f37", "#9a6700", "#0550ae", "#8250df", "#1b7c83", "#6e7781",
                     "#57606a", "#cf222e", "#2da44e", "#bf8700", "#218bff", "#a475f9", "#3192aa", "#8c959f"]}


def ansi_256(n, theme):
    if n < 16:
        return ANSI_16[theme][n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def ansi_html(text, theme):
    """Terminal output (SGR colours, bold, dim, italic, underline, 256 and true colour) as HTML spans."""
    text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)  # OSC (titles, links)
    text = re.sub(r"\r(?!\n)", "\n", text)
    out, state = [], {}
    for chunk in re.split(r"(\x1b\[[0-9;]*[A-Za-z])", text):
        m = re.match(r"\x1b\[([0-9;]*)m$", chunk)
        if m:
            codes = [int(c) if c else 0 for c in m.group(1).split(";")] if m.group(1) else [0]
            i = 0
            while i < len(codes):
                c = codes[i]
                if c == 0:
                    state = {}
                elif c in (1, 2, 3, 4):
                    state[{1: "bold", 2: "dim", 3: "italic", 4: "under"}[c]] = True
                elif c in (22, 23, 24):
                    for k in {22: ("bold", "dim"), 23: ("italic",), 24: ("under",)}[c]:
                        state.pop(k, None)
                elif 30 <= c <= 37 or 90 <= c <= 97:
                    state["fg"] = ANSI_16[theme][c - 30 if c < 90 else c - 82]
                elif 40 <= c <= 47 or 100 <= c <= 107:
                    state["bg"] = ANSI_16[theme][c - 40 if c < 100 else c - 92]
                elif c in (39, 49):
                    state.pop("fg" if c == 39 else "bg", None)
                elif c in (38, 48) and i + 1 < len(codes):
                    key = "fg" if c == 38 else "bg"
                    if codes[i + 1] == 5 and i + 2 < len(codes):
                        state[key] = ansi_256(codes[i + 2] % 256, theme)
                        i += 2
                    elif codes[i + 1] == 2 and i + 4 < len(codes):
                        state[key] = "#%02x%02x%02x" % tuple(v % 256 for v in codes[i + 2:i + 5])
                        i += 4
                i += 1
            continue
        if chunk.startswith("\x1b["):
            continue  # cursor moves and the like
        if not chunk:
            continue
        style = []
        if "fg" in state:
            style.append("color:" + state["fg"])
        if "bg" in state:
            style.append("background:" + state["bg"])
        if state.get("bold"):
            style.append("font-weight:bold")
        if state.get("dim"):
            style.append("opacity:.6")
        if state.get("italic"):
            style.append("font-style:italic")
        if state.get("under"):
            style.append("text-decoration:underline")
        body = html.escape(chunk, quote=False)
        out.append('<span style="%s">%s</span>' % (";".join(style), body) if style else body)
    return "".join(out)


def term(text, theme, width, name=None):
    t = THEMES[theme]
    css = """#c { background: %s; border-radius: 8px; padding: 16px 20px; }
pre { margin: 0; font: 14px/1.45 %s; white-space: pre-wrap; }""" % (t["code"], MONO + ", " + font_stack())
    return page("<pre>%s</pre>" % ansi_html(text.rstrip("\n"), theme), theme, css=css,
                script="D.finish();", width=max(width, 960) if width == 720 else width)


# ---------- image: SVG or HTML ----------

# what an HTML page given to us may load: only what is inside it (no files of this machine, no network, no frames)
CSP = ("default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:; "
       "script-src 'unsafe-inline'; frame-src 'none'")
LOCKED = '<meta http-equiv="Content-Security-Policy" content="%s">' % CSP
# the biggest an HTML page is drawn, in CSS pixels: anything past it is left out; a tall one is cut into pictures
# like a long doc
HTML_MAX = (2000, 5000)
# inside the frame: tell our page how big the content is once it has loaded
PROBE = r"""<script>
addEventListener("load", function () {
  setTimeout(function () {
    var e = document.documentElement, w = fit(), h = height();
    var narrow = w > 0 && w < e.clientWidth - 8 && e.scrollWidth <= e.clientWidth;
    if (narrow) {  // laid out as the frame will be; kept only when nothing moved or sticks out (what fit() missed)
      e.style.width = w + "px";
      var out = Array.prototype.some.call(document.body.querySelectorAll("*"), function (el) {
        return el.getBoundingClientRect().right > w + 0.5;
      });
      if (height() !== h || out) {
        e.style.width = "";
        narrow = false;
      }
    }
    parent.postMessage({display: [narrow ? w : e.scrollWidth, height()], narrow: narrow}, "*");
  }, 200);
});
function height() {
  var b = document.body.getBoundingClientRect();
  return Math.ceil(b.bottom + (parseFloat(getComputedStyle(document.body).marginBottom) || 0) + window.scrollY);
}
function fit() {  // as wide as what shows: text, pictures, and boxes that do not span the page
  var left = Infinity, right = 0, vw = document.documentElement.clientWidth;
  function take(r) { if (r.width && r.height) { left = Math.min(left, r.left); right = Math.max(right, r.right); } }
  var walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), n, range = document.createRange();
  while ((n = walk.nextNode())) {
    if (!n.textContent.trim()) continue;
    range.selectNodeContents(n);
    Array.prototype.forEach.call(range.getClientRects(), take);
  }
  document.body.querySelectorAll("*").forEach(function (el) {
    var s = getComputedStyle(el), r = el.getBoundingClientRect();
    var seen = /^(IMG|SVG|CANVAS|VIDEO|TABLE|INPUT|BUTTON|SELECT|TEXTAREA|PRE)$/i.test(el.tagName) ||
               s.backgroundColor !== "rgba(0, 0, 0, 0)" || s.backgroundImage !== "none" ||
               parseFloat(s.borderRightWidth) > 0 || s.boxShadow !== "none";
    if (seen && r.right < vw - 1) take(r);
    else if (seen) right = vw;  // something reaches the edge: the page keeps its width
  });
  return right && left < Infinity ? Math.ceil(right + Math.max(left, 0)) + 4 : 0;  // + room: no wrap at another scale
}
</script>"""


def lock(text):
    """The CSP put where it counts and keeps the page out of quirks mode: in <head>, else right after <html> or
    the doctype."""
    for pattern in (r"<head(\s[^>]*)?>", r"<html(\s[^>]*)?>", r"<!doctype[^>]*>"):
        m = re.search(pattern, text, re.I)
        if m:
            return text[:m.end()] + LOCKED + text[m.end():]
    return LOCKED + text


def image(text, theme, width, name=None):
    head = text.lstrip()[:500]
    if re.match(r"<\?xml[^>]*>\s*<svg|<svg[\s>]", head, re.I) or (name or "").lower().endswith(".svg"):
        data = base64.b64encode(text.encode("utf-8")).decode()
        body = '<img id="svg" src="data:image/svg+xml;base64,%s">' % data
        script = r"""
var img = document.getElementById("svg");
function done() {
  if (!img.naturalWidth) return D.fail("SVG 打不開");
  if (img.naturalWidth < 400) img.style.width = (img.naturalWidth * 2) + "px";
  D.finish();
}
if (img.complete) done(); else { img.onload = done; img.onerror = function () { D.fail("SVG 打不開"); }; }
"""
        css = "#c { padding: 16px; } img { display: block; max-width: %dpx; }" % max(width, 960)
        return page(body, theme, css=css, script=script)
    # an HTML page is never our top page: it runs in a sandboxed frame of ours (its own origin, no way to move our
    # page; it and the frame may load nothing), laid out at `width`, and says its size back. The mark is checked in
    # both passes, so a picture of anything but our page fails.
    width = width if width != 720 else 1000
    mark = os.urandom(12).hex()
    # white under the page, as in a browser (a page that sets no background is not black on our dark card)
    frame = "display: block; border: 0; background: #fff; color-scheme: light"
    body = ('<iframe id="f" sandbox="allow-scripts" style="%s; width: %dpx; height: 100px" srcdoc="%s"></iframe>'
            % (frame, width, html.escape(lock(text) + PROBE, quote=True)))
    script = "var MAX_W = %d, MAX_H = %d;" % HTML_MAX + r"""
var f = document.getElementById("f");
addEventListener("message", function (e) {
  if (e.source !== f.contentWindow || !e.data || !e.data.display || D.done) return;
  // the page says its own size (narrower than the frame: as wide as what it shows): believed only up to what
  // makes a picture worth posting
  f.style.width = Math.min(Math.max(e.data.narrow ? 1 : %d, e.data.display[0]), MAX_W) + "px";
  var h = Math.min(Math.max(1, e.data.display[1]), MAX_H);
  f.style.height = h + "px";
  if (e.data.display[0] > MAX_W || e.data.display[1] > MAX_H)
    D.note = "頁面是 " + e.data.display[0] + "×" + e.data.display[1] + " px，只畫了左上的 " + MAX_W + "×" + MAX_H +
             " px（其餘沒畫：分段、縮窄，或改用 pdf）";
  // what we see of it is one block: let it be cut anywhere, in steps of 100px (a cut can go through a line of
  // text: we cannot see inside the frame to find the gaps)
  D.blocks = [];
  for (var y = 0; y < h; y += 100) D.blocks.push([y, Math.min(y + 100, h)]);
  D.finish();
});
""" % width
    return {"page": page(body, theme, css="#c { padding: 0; }", head=LOCKED, script=script, mark=mark),
            "mark": mark}


# ---------- pdf, tex, plot: not through Chrome ----------

def pdf_pages(path, out, scale, pages=None):
    if not shutil.which("pdftoppm"):
        raise Fail("要 pdftoppm（poppler）才能轉 PDF")
    first, last = 1, 3
    if pages:
        m = re.match(r"^(\d+)(?:-(\d+))?$", pages)
        if not m:
            raise Fail("--pages 要寫成 3 或 2-5")
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if last - first > 19:
        raise Fail("一次最多 20 頁")
    base = out[:-4]
    with tempfile.TemporaryDirectory(prefix="display-pdf-") as tmp:  # only this run's pages, nothing else there
        subprocess.run(["pdftoppm", "-png", "-r", str(int(75 * scale)), "-f", str(first), "-l", str(last), path,
                        os.path.join(tmp, "p")], check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outs = []
        for f in sorted(os.listdir(tmp)):
            m = re.match(r"^p-(\d+)\.png$", f)
            if m:
                target = "%s-%s.png" % (base, m.group(1))
                shutil.move(os.path.join(tmp, f), target)
                outs.append(target)
    if not outs:
        raise Fail("PDF 沒有那幾頁")
    return outs


def cjk_font_fc():
    if not shutil.which("fc-list"):
        return "Songti TC" if os.uname().sysname == "Darwin" else None
    try:
        have = subprocess.run(["fc-list", ":lang=zh", "family"], stdout=subprocess.PIPE, timeout=20).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        return None
    names = {n.strip() for line in have.splitlines() for n in line.split(",")}
    return next((f for f in ["Noto Serif CJK TC", "Source Han Serif TC", "AR PL UMing TW", "Noto Sans CJK TC",
                             "Songti TC", "PingFang TC", "Droid Sans Fallback"] if f in names), None)


# files Tectonic rewrites in its cache on every run: copied into the throwaway cache, not linked
TEX_CACHE_COPIED = (".lock", ".prefetch")
# a name fetched into the real cache: a plain file name (no /, no .., not hidden), as every file of the bundle is
BUNDLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+-]*(\.[A-Za-z0-9_+-]+)*$")
# what one document may have fetched into the real cache: this many files, in this many seconds; past either the
# picture is still made and the rest is left out (a later picture fetches it again, in its throwaway cache)
TEX_FETCH_FILES = 300
TEX_FETCH_SECONDS = 600


def bundle_files():
    """{name: folder it is cached in} for every file of the TeX bundle (its index is in the cache after warming)."""
    files = {}
    for index in glob.glob(os.path.join(tectonic_cache(), "bundles", "data", "*.index")):
        folder = index[:-len(".index")]
        with open(index, encoding="utf-8", errors="replace") as f:
            for line in f:
                name = line.split(" ", 1)[0]
                if name:
                    files[name] = folder
    return files


def throwaway_cache(dest):
    """A cache for one run at `dest`: every file a link to the real one (which the run may only read), so what the
    run downloads, or writes there itself, lands in `dest` and is thrown away with it."""
    real = tectonic_cache()
    for root, _, names in os.walk(real):
        here = os.path.join(dest, os.path.relpath(root, real))
        os.makedirs(here, exist_ok=True)
        for name in names:
            if name.endswith(TEX_CACHE_COPIED):
                shutil.copy2(os.path.join(root, name), os.path.join(here, name))
            else:
                os.symlink(os.path.join(root, name), os.path.join(here, name))


def downloaded(dest):
    """Names of the bundle files the run fetched into its throwaway cache (not what it may have written there)."""
    files, new = bundle_files(), set()
    data = os.path.join(dest, "bundles", "data")
    for root, _, names in os.walk(data):
        if os.path.dirname(root) != data:  # only right under bundles/data/<digest>/, where Tectonic puts them
            continue
        for name in names:
            path = os.path.join(root, name)
            if (BUNDLE_NAME.match(name) and not os.path.islink(path) and name in files
                    and os.path.basename(root) == os.path.basename(files[name])
                    and not os.path.exists(os.path.join(files[name], name))):
                new.add(name)
    return sorted(new)


def tex_doc(text, theme, width, preamble=""):
    t = THEMES[theme]
    font = cjk_font_fc()
    cjk = "\\usepackage{xeCJK}\\setCJKmainfont{%s}\n" % font if font else ""
    # 12pt stands for the 20px of the browser kinds, so `width` CSS pixels make the same line length
    return ("\\documentclass[12pt,border=14pt,varwidth=%.1fcm]{standalone}\n"
            "\\usepackage{amsmath,amssymb,booktabs,xcolor}\\linespread{1.3}\n%s%s\n"
            "\\definecolor{displaybg}{HTML}{%s}\\definecolor{displayfg}{HTML}{%s}\n"
            "\\begin{document}\\pagecolor{displaybg}\\color{displayfg}\n%s\n\\end{document}\n"
            % (width * 0.6 / 96 * 2.54, cjk, preamble or "", t["bg"][1:], t["fg"][1:], text))


def tectonic(args, tmp, cache=None, timeout=600):
    """Tectonic in the sandbox: it reads and writes only `tmp`, and reads the TeX cache. Only runs that run no TeX
    of the caller's (the warm-up of our own document, fetching a file by name) get the real cache (`cache=True`) to
    write to; a document could otherwise put a package of its own there (\\openout) for every later picture to
    load. Pass a throwaway cache inside `tmp` as `cache` for the caller's document."""
    tec, real = tectonic_bin(), tectonic_cache()
    os.makedirs(real, exist_ok=True)
    rw, ro = [tmp] + ([real] if cache is True else []), [os.path.dirname(tec)] + ([] if cache is True else [real])
    env = dict(os.environ, TECTONIC_CACHE_DIR=real if cache in (None, True) else cache)
    return confine.run([tec] + args, rw, ro, cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)


def tex_warm():
    """Our own document typeset once with the cache writable: Tectonic builds its format there and downloads what
    every picture uses."""
    if glob.glob(os.path.join(tectonic_cache(), "formats", "*-latex-*.fmt")):
        return
    say("第一次在這台編 LaTeX：下載 TeX 套件（約 40MB，幾分鐘）…")
    with tempfile.TemporaryDirectory(prefix="display-") as tmp:
        path = os.path.join(tmp, "warm.tex")
        with open(path, "w", encoding="utf-8") as f:
            f.write(tex_doc("$x$ 中文", "dark", 720))
        r = tectonic(["--untrusted", "--outdir", tmp, path], tmp, cache=True)
    if r.returncode != 0:
        raise Fail("Tectonic 準備不起來：\n  " + "\n  ".join(r.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]))


def tex(text, out, theme, width, scale, preamble=""):
    tex_warm()
    doc = tex_doc(text, theme, width, preamble)
    dpi = int(120 * scale)
    with tempfile.TemporaryDirectory(prefix="display-") as tmp:
        path = os.path.join(tmp, "display.tex")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        pdf = os.path.join(tmp, "display.pdf")
        # the document runs against a throwaway copy of the cache; the files it downloaded are then fetched again
        # by name into the real cache, by a run that has no TeX of the caller's in it
        cache = os.path.join(tmp, "cache")
        throwaway_cache(cache)
        r = tectonic(["--untrusted", "--keep-logs", "--outdir", tmp, path], tmp, cache=cache)
        new, start = downloaded(cache), time.time()
        for i, name in enumerate(new):
            left = TEX_FETCH_SECONDS - (time.time() - start)
            if i >= TEX_FETCH_FILES or left <= 0:
                say("TeX 套件只存了 %d 個（共 %d 個），其他的下次用到時再抓" % (i, len(new)))
                break
            # in an empty folder of its own: the document could have left a Tectonic.toml (other bundle) in tmp
            with tempfile.TemporaryDirectory(prefix="display-fetch-") as clean:
                try:
                    tectonic(["-X", "bundle", "cat", name], clean, cache=True, timeout=min(120, left))
                except subprocess.TimeoutExpired:
                    pass
        if r.returncode != 0 or not os.path.isfile(pdf):
            log = r.stdout.decode("utf-8", "replace")
            try:
                with open(os.path.join(tmp, "display.log"), encoding="utf-8", errors="replace") as f:
                    log = f.read()
            except OSError:
                pass
            bad = [ln for ln in log.splitlines() if ln.startswith("!") or "error:" in ln]
            raise Fail("LaTeX 編譯失敗：\n  " + "\n  ".join((bad or log.strip().splitlines())[-8:]))
        # the page is not a whole number of pixels: pdftoppm rounds up and leaves a white edge, so cut to the floor
        info = subprocess.run(["pdfinfo", pdf], stdout=subprocess.PIPE, timeout=30).stdout.decode()
        m = re.search(r"Page size:\s*([\d.]+) x ([\d.]+)", info)
        crop = ["-W", str(int(float(m.group(1)) * dpi / 72)), "-H", str(int(float(m.group(2)) * dpi / 72))] if m else []
        subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-singlefile"] + crop + [pdf, out[:-4]], check=True,
                       timeout=60)
    return [out]


PLOT_RUNNER = r"""
import os, sys, runpy
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
bg, fg, line, out, dpi, script = sys.argv[1:7]
names = {f.name for f in font_manager.fontManager.ttflist}
# single-face fonts first: matplotlib reads only the first face of a .ttc (AR PL UMing's comes out as boxes)
cjk = [f for f in ["Noto Sans CJK TC", "Source Han Sans TC", "Droid Sans Fallback", "Arial Unicode MS", "Heiti TC",
                   "PingFang TC", "Noto Serif CJK TC", "AR PL UMing TW", "Songti TC"] if f in names]
plt.rcParams.update({
    "figure.facecolor": bg, "axes.facecolor": bg, "savefig.facecolor": bg, "axes.edgecolor": line,
    "axes.labelcolor": fg, "text.color": fg, "xtick.color": fg, "ytick.color": fg, "grid.color": line,
    "legend.facecolor": bg, "legend.edgecolor": line, "axes.unicode_minus": False,
    "font.family": ["DejaVu Sans"] + cjk, "figure.dpi": 100,  # a list of families: glyph-by-glyph fallback
})
if bg.lower() != "#ffffff":
    plt.rcParams["axes.prop_cycle"] = matplotlib.cycler(color=["#5865f2", "#f0b232", "#23a55a", "#f23f43", "#eb459e",
                                                                "#00a8fc", "#a4a8ff", "#ff9f43"])
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
nums = plt.get_fignums()
if not nums:
    sys.exit("腳本沒有留下任何圖（畫完不要 plt.close()，也不用 savefig／show）")
for i, n in enumerate(nums):
    path = out if len(nums) == 1 else "%s-%d.png" % (out[:-4], i + 1)
    plt.figure(n).savefig(path, dpi=float(dpi), bbox_inches="tight", pad_inches=0.25)
    print(path)
"""


def plot(path, out, theme, scale):
    py = venv_python()
    t = THEMES[theme]
    with tempfile.TemporaryDirectory(prefix="display-") as tmp:
        runner = os.path.join(tmp, "runner.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(PLOT_RUNNER)
        env = dict(os.environ, MPLCONFIGDIR=os.path.join(TOOLS, "mpl"), MPLBACKEND="Agg")  # keeps its font cache
        r = subprocess.run([py, runner, t["bg"], t["fg"], "#4e5058" if theme == "dark" else "#d4d7dc", out,
                            str(100 * scale), os.path.abspath(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=300, env=env)
    if r.returncode != 0:
        raise Fail("matplotlib 腳本失敗：\n" + r.stderr.decode("utf-8", "replace").strip()[-1200:])
    return [p for p in r.stdout.decode().splitlines() if p.endswith(".png") and os.path.isfile(p)]
