# ai-gate

對公開網址跑「AI 可抓取網站」十道閘門，輸出每一閘的狀態與證據。唯讀檢查：不登入、不送表單、不修改網站。

適合快速判斷一個網站是否容易被 AI 爬蟲取得、解析與引用。真實網站會因為 CDN、防火牆、A/B 測試或伺服器暫時狀態而有不同結果，所以報告要看證據，不只看分數。

## 怎麼執行

```powershell
python ai_gate.py https://example.com
```

產生 HTML 與 CSV 報告：

```powershell
python ai_gate.py https://example.com --html report.html --csv report.csv
```

輸出 JSON：

```powershell
python ai_gate.py https://example.com --json
```

從網站地圖多檢查幾個 URL：

```powershell
python ai_gate.py https://example.com --crawl 10 --html report.html --csv report.csv
```

`--crawl N` 會從 robots.txt 或 `/sitemap.xml` 找網站地圖，再多檢查最多 N 個 URL；工具內建上限 50。

換掉送出的 `Accept-Language`：

```powershell
python ai_gate.py https://example.com --accept-language "en-US,en;q=0.9"
```

## 輸出怎麼看

每一閘會顯示：

- `PASS`：通過。
- `FAIL`：未通過，會列入修正清單。
- `WARN`：警示，不等同硬失敗；例如 robots.txt 可讀且未封鎖 AI 爬蟲，但缺 Sitemap 指令。
- `ERROR`：檢查過程出錯；工具會繼續檢查其他閘門。
- `MANUAL`：遠端工具不能判定，必須人工查伺服器資料。

分數格式是 `X/9 hard gates passed`。第十閘「爬蟲足跡可觀測」不能遠端檢查，所以不列入分數。

程式輸出與報告都是英文，閘門名稱對照如下。

## 十道閘門白話說明

| # | 輸出上的名稱 | 白話 |
|---|---|---|
| 1 | Reachable without login | 免登入可達：網址 GET 要回 200，不能被登入頁、同意牆或密碼欄擋住。 |
| 2 | Body text in the raw HTML | 內文在原始碼裡：不執行 JavaScript，只看原始 HTML，就要能看到主要文字內容。這是最重要的一閘。 |
| 3 | AI crawlers treated equally | AI 爬蟲不被差別待遇：一般瀏覽器、GPTBot、ClaudeBot 三種身分拿到的狀態碼要相同，內容長度誤差要在 5% 內。 |
| 4 | robots.txt complete | 爬蟲規則檔完備：`/robots.txt` 要可讀，且不得對主要 AI bot 禁止 `/`。缺 Sitemap 只警示。 |
| 5 | Sitemap complete | 網站地圖完整：要找到可解析的 sitemap，至少含一個 URL，並回報有多少 URL 帶 lastmod。 |
| 6 | Exactly one h1 per page | 每頁單一主標題：頁面要剛好一個 `<h1>`，標題層級不能跳級。 |
| 7 | Structured data present | 機器名片齊備：頁面要有有效 JSON-LD，且每個項目至少有 `@context` 與 `@type`。 |
| 8 | Author and date machine-readable | 身分與時間可讀：要有機器可讀作者訊號，也要有可解析的絕對日期。 |
| 9 | Self-contained paragraphs (heuristic) | 段落自足：啟發式掃描相對時間詞與跨段引用詞。這不是硬判決，只是提醒。 |
| 10 | Crawler hits observable | 爬蟲足跡可觀測：遠端無法檢查。必須到伺服器 log 搜 GPTBot、ClaudeBot、PerplexityBot 等 user-agent。 |

## 已知限制

**第九閘只對中文頁面有效。** 它掃的相對時間詞與跨段引用詞都是中文詞表，英文頁面一定掃到零個命中而通過，那不代表文字品質好，只代表「沒量到」。

**預設 `Accept-Language` 偏好繁體中文。** 多語網站可能因此回你中文版頁面。要換用 `--accept-language`。

第十閘不能從外部網站遠端驗證。工具永遠回報 `MANUAL`，不會回報 `PASS`。

工具不執行 JavaScript，所以它檢查的是 AI 爬蟲通常能取得的原始回應，不是瀏覽器最終畫面。

工具只做 GET/HEAD，15 秒 timeout，同一 host 至少間隔 1.5 秒；不登入、不 POST、不下載看起來像二進位檔的 URL。回應內容最多讀 2 MB。

robots.txt 解析支援 user-agent 群組、`*` 群組、Allow/Disallow 的根路徑判定；複雜 wildcard 規則用於判定 `/` 是否被封鎖。

第五閘會逐一嘗試 robots.txt 內所有 `Sitemap:` 指令；若宣告的地圖都沒有找到 URL，才追加嘗試 `/sitemap.xml` 與 `/sitemap_index.xml`。支援標準 sitemap、sitemap index 一層追蹤、Atom/RSS feed 連結解析，以及 `.xml.gz` 或 gzip 回應解壓；單一閘門最多抓取 8 個 sitemap 相關檔案，失敗或逾時會記在該項證據並繼續檢查。

## 授權

MIT，見 [LICENSE](LICENSE)。

English version: [README.md](README.md)
