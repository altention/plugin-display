# display 使用中的問題

日常使用時有人回報、或自己用 display 時發現不好用的，記在這裡，累積幾項再一起修。
每項寫：日期、誰發現、情境（輸入、指令）、看到什麼、預期什麼。

- 2026-10-20，開發時核對 README 發現：`tex` 讀得到這台的檔案。`display -e '\input{/某個/檔案}' --kind tex` 會把那個檔案的內容排進圖裡；`--untrusted` 只關掉 shell-escape 這類，擋不住 `\input` 絕對路徑。瀏覽器類（含 HTML、SVG、Markdown 圖片、Mermaid）實測讀不到本機檔、連不到網路（含 127.0.0.1）。已決定要修，見 decisions.md。**已修（v0.2.0）**：tex 在 Landlock／sandbox-exec 裡跑，README 已知限制已改。
- 2026-10-20，修上一項時發現：`tex` 也**寫得到**這台的檔案。`\immediate\openout` 接絕對路徑或 `../` 會直接建檔、改檔（實測在暫存資料夾外建出檔案），`--untrusted` 也擋不住。**已修（v0.2.0）**：只寫得到自己的暫存資料夾；文件跑在用完就丟的快取上，寫不進真的 TeX 快取。
- 2026-10-20，審查 tex 沙箱時提出（記下不修）：惡意的 tex 文件不用真的載入套件，只要用 `\openout` 在丟棄快取放套件包裡的檔名，每張圖就會讓真快取多抓這些正版檔。有上限：每張最多 300 個檔、600 秒；整個套件包約 2.78 GB，是硬碟用量的上限。內容一定是套件包的正版，不會汙染快取，只是浪費時間和硬碟。
- 2026-10-20，盲測時發現（新的 Claude，沒提示要用 display，問「比較 Adam、SGD、AdamW、Lion…」）：Claude 自己用 `doc` 畫了比較表，但表格的英文字在格子裡被從中間切斷：表頭「SGD (+momentum)」斷成「momentu／m」，「Transformer」斷成「Transforme／r」。預期：英文單字整個換行，不從字中間斷；欄寬也可以依內容分配（第一欄的列名很窄，「記憶體（參數倍數，不含梯度）」擠成四行）。**已修（v0.3.0）**：原因是 `overflow-wrap: anywhere` 讓表格算欄寬時把單字當成可以隨處斷，改成 `break-word`。表格最寬維持 720：1100 寬在手機（約 330pt）上讀不到。
- 2026-10-20，同一次盲測（問「TCP 三次握手和四次揮手的流程」）：長文件自動切成兩張時，「四次揮手（關閉連線）」這個標題落在第 1 張的最底，它的時序圖在第 2 張。預期：不要在標題後面切，標題跟著下一段到下一張。另外兩張圖貼出的時間差 30 秒，原因未查（可能是上傳或機器人的發送排程）。**標題已修（v0.3.0）**：切點計算時標題跟下一個區塊黏在一起。30 秒：連貼 3 則 1 秒內都出現，沒重現；屬核心的發送迴圈。display 這邊一度改成每則等上一則出現（最多 10 秒）再貼；Altention 0.3.2 會照順序貼同一個寄件者的訊息之後，這個等待已拿掉。
- 2026-10-20，審查 `--for-bot` 時提出（記下不修）：fence 那一行本身超過約 1890 字時（例如語言名稱 1950 字），切出的第 1 則會到 1962 字。仍在 Discord 的 2000 字內，實際不會這樣寫。
- 2026-10-22，審 HTML 縮窄時提出（記下不修）：用絕對定位、寫死 px 寬的 `::before`／`::after` 裝飾（例如 700px 的線）在頁面縮窄後會被裁掉；`width:100%` 的不受影響。`box-shadow` 往外偏移的也會被裁。另外 `gantt title 計畫` 這種 title 寫在同一行的，不會關掉「今天」的線。都少見，沒壞圖。
- 2026-10-24，做安裝引導時在 docker 實測發現（記下不修）：fedora:42 映像裡用 `dnf install chromium` 裝的 Chromium 畫不出圖，每張都等到逾時（「Chrome 沒有在時間內畫完」）。原因是 docker 預設不給 user namespace，Chromium 的 sandbox 起不來（stderr 是 `Failed to move to new namespace…Operation not permitted`、zygote `Check failed`），但程序卡住不結束，不會以非 0 退出；`run_chrome` 只在「結束碼非 0 且 stderr 有 sandbox」時加 `--no-sandbox` 重試，所以接不到。真的 Fedora 主機有 user namespace，不受影響；ubuntu、debian 映像裡用 Playwright 裝的 headless shell 也畫得出來。
