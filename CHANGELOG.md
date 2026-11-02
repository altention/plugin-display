# Changelog

Every release has a `## vX.Y.Z — date` heading, newest first, and an annotated git tag `vX.Y.Z` on the commit it
describes. The format follows the engine's CHANGELOG: **For people**, **For people (zh-TW)**, **For agents**.

## v0.4.3 — 2026-11-03

### For people

- **Installing Chrome mostly needs no sudo now.** On Debian, Ubuntu and Fedora, Chrome is fetched without sudo when
  the machine already has the system libraries it needs (most with a desktop do); when some are missing, it names
  them and gives the one line, with sudo, that installs them.

### For people (zh-TW)

- **裝 Chrome 多半不用 sudo 了。** Debian、Ubuntu、Fedora 上，機器已經有 Chrome 要的系統函式庫時（有桌面的多半有），不用 sudo 就裝好；缺的時候先列出缺哪些，再給一行要 sudo 的指令把它們裝上。

### For agents

- `display --install chrome` (Linux): Playwright 1.64.1's wheel from PyPI (sha256 checked), its Node driver installs
  `chromium-headless-shell` into `~/.cache/ms-playwright`; no venv, pip or python3-venv. Libraries missing (by
  `ldd`): exit 1 with the sudo line, run only from a terminal. `--check chrome` now also fails (1) when the Chrome
  found lacks libraries. plugin.json's Debian and Fedora install is this command.

## v0.4.2 — 2026-11-03

### For people

- **Long formulas are no longer cut off unnoticed.** A one-line formula wider than the text now gets a wider
  picture and is drawn whole. One wider still is not drawn: the reply is posted as written, never with half a
  formula.

### For people (zh-TW)

- **長公式不再被悄悄切掉。** 比文字寬的一行公式，圖會跟著加寬、整條畫出來；再寬的就不畫，整則照原文貼出，不會只貼半條公式。

### For agents

- A display formula is drawn up to 1200 px wide (`MATH_MAX`; text and tables stay at `--width`). A formula still
  wider is reported on stderr as 「第 N 個公式太寬被裁掉：拆成多行（align）」 (N counts the page's display
  formulas). With `--for-bot` that fails the whole reply (exit 2), so the bot posts it as written.

## v0.4.1 — 2026-11-02

### For people

- **A formula right under a table stays a formula.** When a formula came right after a table, it could be drawn as
  the table's last row; now each is drawn on its own, one under the other in the same picture.
- **Numbered equations no longer overlap.** In a formula with equation numbers (such as `align`), (1) and (2)
  were drawn over it; they now sit to its right.

### For people (zh-TW)

- **表格下面的公式不會再變成表格的一列。** 表格後面緊接公式時，公式有時會被畫成表格的最後一列；現在各自畫好，上下排在同一張圖裡。
- **有編號的公式不再疊字。** 多行公式的編號 (1)、(2) 會壓在公式上；現在排在公式右邊。

### For agents

- Pictures next to each other in `--segments` and `--for-bot` are joined with an empty line, in the picture and in
  its `.source.json` `source` alike.
- A picture's `.source.json` `kind` names three or more parts as 「表格、公式和流程圖」 (was 「表格和公式和流程圖」); two stay
  「公式和表格」.

## v0.4.0 — 2026-11-02

### For people

- **Pictures without the empty space.** A chart by month or by name is wide enough to read, with its labels
  upright; a Gantt chart has no red "today" line across it; a side-by-side diff and a short HTML page are as wide
  as what they show, not a wide card that is mostly empty.
- **Ready for the bot to draw replies as they are posted.** When the bot can hand a reply to a plugin
  before posting it, display takes the whole reply and gives back the messages to post: the text, with each
  formula, table or diagram as a picture under the text before it. A reply with nothing to draw is left to the
  bot. `$x$` in the text is left as it is there, since the reply is already written.
- **No more lopsided box diagrams.** Claude no longer draws boxes and arrows with `┌─┐` in a code block, whose
  right edges never line up around Chinese text on Discord; such a picture is drawn as a small diagram (top to
  bottom, so it reads on a phone) or written as a plain list.
- **Tap 📝 on a picture to get what it was drawn from.** Each picture has its source next to it (the LaTeX of a
  formula, the Mermaid of a diagram, the Markdown of a table…); a bot that supports it posts it back, ready to copy,
  when you tap 📝 on the picture. Nothing changes on the picture itself.

### For people (zh-TW)

- **圖不再有大片空白。** 依月份或名稱畫的圖表夠寬、標籤是正的；甘特圖不再有一條跟內容無關的紅色「今天」線；左右對照的程式差異和短的 HTML 頁面照內容的寬度裁，不再是一大張大半空著的卡片。
- **機器人貼出回覆前可以交給 display 排版。** 機器人支援之後，Claude 的回覆在貼出前會整份交給 display，拿回要貼的幾則訊息：文字照常，公式、表格、流程圖畫成圖接在前面那段文字下面。沒有要畫的就照原樣交回給機器人。這時文字裡的 `$x$` 不擋，照原樣留著（回覆已經寫好了）。
- **不再有歪掉的框線圖。** Claude 不再用 `┌─┐` 在程式碼區塊裡畫方塊加箭頭的圖（框裡有中文時，Discord 上右邊一定對不齊），改畫成一張小圖（直的，手機看得清楚），或寫成條列。
- **在圖上按 📝 看原文。** 每張圖旁邊都留了它的原文（公式的 LaTeX、流程圖的 Mermaid、表格的 Markdown…）；機器人支援之後，在圖上按 📝 就會貼出可以複製的原文。圖本身跟現在一樣。

### For agents

- `--for-bot -o` (and any `-o`) makes the folder it names when it is not there.
- plugin.json has a `rewrite` block for the bot: `display - --for-bot -o {out}`, 30 seconds, called when the
  reply has display math, a table or a mermaid block. Nothing to set.
- Every picture has `<picture>.png.source.json`: `{"kind", "lang", "tool", "source"}` (kind and tool short Chinese,
  lang a code block language; none for a PDF or a source over 64 KB). `--for-bot` messages carry them as
  `"sources": {"<image path>": {…}}`; a bot that does not know the key leaves it.

## v0.3.0 — 2026-10-20

### For people

- **Only the part Discord cannot show is drawn.** Claude now posts an answer as a few messages in reading order:
  each formula, table or diagram as a small picture with the text about it, then the conclusion. A whole answer is drawn as one
  picture only when you ask, or for a document whose layout matters. `display answer.md --segments --post <channel>`
  does it in one go, in order; it stops, posting nothing, when the text still has `$…$` Discord would show as it is.
  `--post` with no channel posts back to the conversation's own channel by its ID, which keeps several posts in a
  row from waiting on Discord's limit for looking channels up by name.
- **Tables keep their words whole.** A word in a table cell is no longer cut in the middle ("Transforme/r"); columns
  are sized by their words. Tables stay at most 720 wide, so they can still be read on a phone: only a very long
  string (a hash, a URL) breaks inside its own cell.
- **A long document is never cut right after a heading,** and a picture of a single formula has less room around it.
- **Pictures in a row go in one message,** under the text before them (up to 10 pictures, 8 MB, a message; past
  that the rest follow in the next). The posts come in the order sent, without waiting on each other: Altention
  0.3.2 keeps them in order.
- **Installed with the rest of Altention.** Once the instance says where the plugin comes from, each machine's
  install fetches it, its drawing libraries and its skill, and lists the system tools it still lacks (Chrome,
  poppler, a Chinese font, python3-venv). Tectonic and matplotlib, the large ones, are fetched the first time
  they are needed; the error says what to run.

### For people (zh-TW)

- **只畫 Discord 顯示不了的那一塊。** Claude 會把回答照閱讀順序分成幾則貼：公式、表格或流程圖畫成小圖，跟講它的文字放在一起，最後是結論。整份畫成一張只在你要求、或要看排版的文件時。`display answer.md --segments --post <頻道>` 一次照順序貼好；文字裡還有 Discord 會原樣顯示的 `$…$` 時會停下來，什麼都不貼。`--post` 不寫頻道就用這個對話自己頻道的 ID 貼回去，連貼好幾則也不會卡在 Discord 查頻道名稱的次數限制。
- **表格不再把英文字從中間切斷。** 欄寬照字分配；表格最寬還是 720，手機上讀得到，只有很長的雜湊、網址會在自己的格子裡換行。
- **長文件不會切在標題正下方，** 只有一個公式的圖四周留白也變小。
- **跟 Altention 一起裝。** 實例寫了插件從哪來之後，每台安裝時會抓插件、畫圖用的函式庫和 skill，並列出還缺的系統工具（Chrome、poppler、中文字型、python3-venv）。比較大的 Tectonic 和 matplotlib 第一次用到才裝，錯誤訊息會說要跑什麼。
- **連在一起的幾張圖放同一則，** 在它前面那段文字下面（一則最多 10 張、8 MB，超過的放下一則）。幾則照送出的順序出現，不用互等：Altention 0.3.2 會照順序貼。

### For agents

- The skill changed: answers go out with `--segments`, text as messages. Nothing to set.
- Needs Altention 0.3.2 or later (several `--file` on one `dc post`, posts kept in order): update the engine first.
- plugin.json's setup is three items for the core's install of external plugins: `browser` runs at install,
  `tectonic` and `plot` are `lazy`. Its `needs` lists chrome, poppler, cjk-font and venv, each checked with
  `bin/display --check <name>`. On a core that does not install external plugins yet, run
  `bin/display --setup` by hand as before.

## v0.2.0 — 2026-10-20

### For people

- **Full LaTeX runs in a sandbox.** A LaTeX document can no longer read or write files of the machine
  (`\input`, `\openout` with an absolute path, `../` or a link) or change the TeX packages later pictures use. On
  Linux it needs Landlock (kernel 5.13 or later), on macOS sandbox-exec; where neither is there, LaTeX is not drawn
  and the reason is said. `display --check` shows a `sandbox` line.
- **A README for people deciding whether to install it.** `README.md` (English) and `README.zh-TW.md` follow the
  engine's plugin README layout, with a screenshot, requirements for Debian, Ubuntu and macOS, and what it does
  with your data and network.
- **MIT license.** The plugin is now under the MIT license, the same as Altention (`LICENSE`).

### For people (zh-TW)

- **完整 LaTeX 在沙箱裡跑。** LaTeX 文件讀不到也寫不到這台的檔案（`\input`、`\openout` 用絕對路徑、`../` 或連結都不行），也改不到之後別張圖用的套件。Linux 要 Landlock（kernel 5.13 以上），macOS 用 sandbox-exec；兩個都沒有就不畫 LaTeX，並說原因。`display --check` 多一行 `sandbox`。
- **給要不要裝的人看的說明。** `README.md`（英文）和 `README.zh-TW.md` 照引擎的插件 README 格式重寫，有截圖、Debian／Ubuntu／macOS 的需求，以及它怎麼處理你的資料和網路。
- **採用 MIT 授權。** 跟 Altention 一樣（`LICENSE`）。

### For agents

- `tex` now needs a sandbox: check the `sandbox` line of `bin/display --check` on each machine. Without one,
  `tex` is refused; nothing to set.

## v0.1.0 — 2026-10-20

### For people

- **First pinned release.** `bin/display` turns what Discord shows badly into images or files: math, tables,
  charts (Vega-Lite), Mermaid, Graphviz, diffs, long documents, ANSI terminal output, SVG/HTML/PDF and matplotlib
  plots. The `display` skill tells Claude when to use it and when Discord's own formatting is enough.
- **HTML is drawn in a sandbox.** Pages can't navigate to local files, pages without a background are drawn on
  white, and the size a page reports is capped; a page taller than what gets drawn is noted on stderr.

### For people (zh-TW)

- **第一個固定版本。** `bin/display` 把 Discord 顯示不好的內容轉成圖或檔：數學式、表格、圖表、Mermaid、Graphviz、diff、長文件、終端機顏色、SVG/HTML/PDF、matplotlib。`display` skill 讓 Claude 知道什麼時候用、什麼時候用 Discord 自己的格式就好。
- **HTML 在沙盒裡畫。** 頁面不能跳到本機檔，沒設背景的畫成白底，頁面自報的尺寸有上限，太長被截斷時會在 stderr 說。

### For agents

- Install the tools once per machine with `bin/display --setup all` (the core does not run it yet), link
  `~/.claude/skills/display` to `skill/`, then check with `bin/display --check`.
