# ai-gate

A read-only checker that measures how easily an AI crawler can fetch, parse and cite a public website, and reports the result as ten pass/fail gates with evidence.

## What it does / why

When a page is meant to be found and quoted by an AI assistant, the things that break it are boring and mechanical: the body text only exists after JavaScript runs, `robots.txt` blocks GPTBot but not Googlebot, the CDN serves a bot a different response than a browser, there is no sitemap, the page has three `<h1>` elements and no structured data. Each of those is cheap to check from outside and expensive to leave broken.

`ai_gate.py` runs ten such checks against a URL and prints, for each gate, a status and the evidence it was decided on. It is strictly read-only: `GET` and `HEAD` only, never logs in, never submits a form, never posts, and skips URLs that look like binaries. It waits at least 1.5 seconds between requests to the same host and times out at 15 seconds.

It does not run JavaScript. That is deliberate — what it measures is what an AI crawler typically receives, not what a browser eventually renders.

### The ten gates

| # | Gate | What it checks |
| --- | --- | --- |
| 1 | Reachable without login | `GET` returns 200, and the final URL is not a login or consent page, and the page has no password field. |
| 2 | Body text in the raw HTML | Without executing JavaScript, the raw HTML already contains the visible body text. The single most important gate. |
| 3 | AI crawlers treated equally | An ordinary browser UA, `GPTBot` and `ClaudeBot` get the same status code, with response sizes within 5% of each other. |
| 4 | robots.txt complete | `/robots.txt` is readable and does not `Disallow: /` any major AI bot. A missing `Sitemap:` directive is only a warning. |
| 5 | Sitemap complete | A parseable sitemap is found — via every `Sitemap:` directive in `robots.txt`, then `/sitemap.xml` and `/sitemap_index.xml` — containing at least one URL. Handles sitemap indexes (one level deep), Atom/RSS feeds, and gzipped sitemaps. Reports how many URLs carry `lastmod`. |
| 6 | Exactly one h1 per page | The page has exactly one `<h1>`, and heading levels descend without skipping. |
| 7 | Structured data present | Valid `application/ld+json` exists and every item has at least `@context` and `@type`. |
| 8 | Author and date machine-readable | A machine-readable author signal and a parseable absolute date are both present. |
| 9 | Self-contained paragraphs (heuristic) | Scans for relative time words and cross-paragraph back-references. A hint, not a verdict — **and see the note on language below.** |
| 10 | Crawler hits observable | Cannot be checked from outside. Always reports `MANUAL`. |

Statuses are `PASS`, `FAIL`, `WARN`, `ERROR` and `MANUAL`. A gate that errors does not stop the run — the remaining gates still execute.

## Requirements

- Python 3.9 or newer. Standard library only — nothing to install.
- Outbound HTTP/HTTPS access to the site being checked.

## Install

No install step. Clone and run:

```
git clone <repo-url>
cd ai-gate
python ai_gate.py --help
```

## Usage

Check one URL:

```
python ai_gate.py https://example.com
```

Write HTML and CSV reports alongside the console output:

```
python ai_gate.py https://example.com --html report.html --csv report.csv
```

Print the whole result as JSON:

```
python ai_gate.py https://example.com --json
```

Also check pages discovered through the site's sitemap:

```
python ai_gate.py https://example.com --crawl 10 --html report.html
```

Send a different `Accept-Language` header:

```
python ai_gate.py https://example.com --accept-language "en-US,en;q=0.9"
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `url` (positional) | required | The URL to check. |
| `--html PATH` | — | Write an HTML report to this path. |
| `--csv PATH` | — | Write a CSV report (one row per gate per URL) to this path. |
| `--json` | off | Print the full result as JSON to stdout, after the human-readable output. |
| `--crawl N` | `0` | Additionally check up to N more URLs found via the sitemap. Capped at 50; URLs that look like binaries are skipped. |
| `--accept-language VALUE` | `zh-TW,zh;q=0.9,en;q=0.8` | The `Accept-Language` header sent with every request. |

## Output

The console prints one line per gate — `[STATUS] Gate name — evidence` — then the score and a numbered fix list naming the concrete change each failing gate needs.

The score reads `passed/9`. Gate 10 is excluded because it cannot be judged remotely. `WARN` counts as passed for scoring purposes.

`--csv` writes `url, final_url, gate, status, evidence`. `--html` writes a standalone self-styled page with the same table plus an "Additional information" block carrying `title`, `description`, `canonical`, `html_lang`, `og_tags_present`, `img_missing_alt`, `llms_txt_exists`, `feed_linked` and `response_time_sec`. `--json` emits a list of report objects, each with `url`, `final_url`, `gates`, `info`, `score` and `fixes`.

Exit codes:

- `0` — no gate among the first nine, other than the heuristic gate 9, came back `FAIL` or `ERROR`.
- `1` — at least one did, or the run was interrupted.

Note the asymmetry: gate 9 counts toward the printed score but never toward the exit code, because it is a heuristic and would otherwise fail CI on prose style.

## Limitations

- **Gate 9 only works on Chinese-language pages.** The relative-time and back-reference word lists it scans for are Traditional Chinese. On an English page it will report zero hits and pass, which says nothing about the prose. Treat a gate 9 `PASS` on a non-Chinese site as "not measured".
- **The default `Accept-Language` prefers Traditional Chinese.** On a multilingual site that means you may be checking the Chinese variant of a page. Use `--accept-language` to control this.
- **No JavaScript.** A site that renders its body client-side will fail gate 2 by design. That is the point of the gate, but it also means the tool cannot tell you what such a page looks like once hydrated.
- **Gate 10 can never pass.** Whether AI crawlers actually reach your site is only visible in your server logs; grep them for `GPTBot`, `ClaudeBot`, `PerplexityBot` and friends.
- **A single reading, not a verdict.** CDNs, WAFs, A/B tests, geographic routing and transient server state all move these results. Read the evidence column, not just the score.
- **Gate 5 stops at 8 fetches** of sitemap-related files per gate, and follows a sitemap index only one level down. A very large or deeply nested sitemap tree is sampled, not exhausted.
- **Response bodies are read up to 2 MB.** Longer pages are truncated before analysis.
- **Gate 3 compares three user agents from one IP.** A CDN that discriminates by ASN or geography rather than user agent will look clean here.

## License

MIT. See [LICENSE](LICENSE).

A Traditional Chinese version of this document is in [README.zh-TW.md](README.zh-TW.md).
