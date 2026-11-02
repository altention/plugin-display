"""What each picture was drawn from: <picture>.png.source.json next to it, {"kind", "lang", "tool", "source"}.

The bot keeps it with the message and gives the source back when someone taps 📝 on the picture: it says
"<kind>，<tool> 做的" and puts the source in a ```<lang> block (or a file <name>.<lang>). kind and tool are short
Chinese words, lang a code block language. A source past SOURCE_MAX, or a PDF (no text to give back), has none.
"""
import json
import os
import re

from .kinds import MATH_ENVS, MERMAID_START, TABLE_RULE, split_segments

SOURCE_MAX = 64 * 1024  # bytes of source: past this the bot would not keep it
SUFFIX = ".source.json"
LANG = re.compile(r"^[A-Za-z0-9+-]{1,20}$")
MERMAID_KINDS = [("flowchart", "流程圖"), ("graph", "流程圖"), ("sequenceDiagram", "時序圖"), ("gantt", "甘特圖"),
                 ("classDiagram", "類別圖"), ("stateDiagram", "狀態圖"), ("erDiagram", "ER 圖"),
                 ("timeline", "時間軸"), ("mindmap", "心智圖"), ("pie", "圓餅圖"), ("journey", "使用者旅程圖"),
                 ("gitGraph", "git 分支圖"), ("quadrantChart", "象限圖"), ("xychart", "圖表"), ("sankey", "桑基圖")]
MATH_START = re.compile(r"^\s*(\$\$|\\\[|\\begin\{(%s)\*?\})" % MATH_ENVS)


def about(kind, lang, tool, source):
    return {"kind": kind, "lang": lang, "tool": tool, "source": source}


def mermaid_kind(text):
    m = MERMAID_START.match(text)
    word = m.group(3) if m else ""
    return next((k for w, k in MERMAID_KINDS if word.startswith(w)), "Mermaid 圖")


def of_markdown(text):
    """A picture of Markdown blocks (display math, tables, ```mermaid; a --segments picture, or a doc that is
    nothing else): what it is, by its blocks. Anything else in it makes it a document."""
    parts = split_segments(text)
    if any(k == "text" for k, _ in parts):
        return about("文件", "markdown", "markdown-it", text)
    blocks, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        s = lines[i].strip()
        fence = re.match(r"^(```|~~~)\s*mermaid", s, re.I)
        if fence:
            j = next((k for k in range(i + 1, len(lines)) if lines[k].strip().startswith(fence.group(1))), len(lines))
            blocks.append(("mermaid", "\n".join(lines[i + 1:j])))
            i = j + 1
            continue
        if MATH_START.match(s):
            blocks.append(("math", None))
        elif s.startswith("|") and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
            blocks.append(("table", None))
        i += 1
    names = {"math": ("公式", "KaTeX", "latex"), "table": ("表格", "markdown-it", "markdown")}
    kinds, tools = [], []
    for b, body in blocks:
        k, t = (mermaid_kind(body), "Mermaid") if b == "mermaid" else names[b][:2]
        kinds += [k] if k not in kinds else []
        tools += [t] if t not in tools else []
    if not kinds:
        return about("文件", "markdown", "markdown-it", text)
    if len(blocks) == 1 and blocks[0][0] == "mermaid":  # the diagram alone: its source, without the fence
        return about(kinds[0], "mermaid", "Mermaid", blocks[0][1])
    lang = names[blocks[0][0]][2] if len({b for b, _ in blocks}) == 1 else "markdown"
    named = kinds[0] if len(kinds) == 1 else "、".join(kinds[:-1]) + "和" + kinds[-1]  # 甲和乙, 甲、乙和丙
    return about(named[:40], lang, "、".join(tools)[:40], text)


def of_input(kind, text, name=None, table=False, preamble=""):
    """What a picture of `kind` drawn from `text` is; None when there is no text to give back (pdf)."""
    if text is None or kind == "pdf":
        return None
    ext = os.path.splitext(name or "")[1].lower()
    if kind == "doc" and (table or ext in (".csv", ".tsv")):
        first = text.strip().split("\n", 1)[0]
        lang = ext[1:] if ext in (".csv", ".tsv") else "markdown" if "|" in first else "tsv" if "\t" in first else "csv"
        return about("表格", lang, "markdown-it", text)
    if kind == "doc":
        return of_markdown(text)
    if kind == "diagram":
        return about(mermaid_kind(text), "mermaid", "Mermaid", text)
    if kind == "image":
        svg = re.match(r"<\?xml[^>]*>\s*<svg|<svg[\s>]", text.lstrip()[:500], re.I) or ext == ".svg"
        return about("SVG", "svg", "Chrome", text) if svg else about("HTML 頁面", "html", "Chrome", text)
    if kind == "tex":
        return about("LaTeX 文件", "latex", "Tectonic", (preamble + "\n" if preamble else "") + text)
    fixed = {"chart": ("圖表", "json", "Vega-Lite"), "graph": ("Graphviz 圖", "dot", "Graphviz"),
             "diff": ("程式差異", "diff", "diff2html"), "term": ("終端機輸出", "ansi", "display"),
             "plot": ("matplotlib 圖", "python", "matplotlib")}
    k, lang, tool = fixed[kind]
    return about(k, lang, tool, text)


def path_of(picture):
    return picture + SUFFIX


def write(picture, info):
    """The sidecar of `picture`; none (and an old one gone) when there is no source or it is too long."""
    side = path_of(picture)
    if not info or len(info["source"].encode("utf-8")) > SOURCE_MAX:
        if os.path.lexists(side):
            os.remove(side)
        return
    assert LANG.match(info["lang"]) and 0 < len(info["kind"]) <= 40 and 0 < len(info["tool"]) <= 40, info
    with open(side, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)


def read(picture):
    try:
        with open(path_of(picture), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
