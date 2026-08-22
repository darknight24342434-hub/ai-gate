#!/usr/bin/env python3
import argparse
import csv
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

DEFAULT_UA = "ai_gate/1.0 (+read-only AI crawlability checker; GET/HEAD only)"
DEFAULT_ACCEPT_LANGUAGE = "zh-TW,zh;q=0.9,en;q=0.8"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
GPTBOT_UA = "GPTBot"
CLAUDEBOT_UA = "ClaudeBot"
AI_BOTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot",
    "Claude-User", "PerplexityBot", "Google-Extended",
]
BINARY_EXT = re.compile(r"\.(?:pdf|zip|rar|7z|png|jpe?g|gif|webp|avif|svg|mp4|mov|mp3|wav|exe|dmg|iso)(?:[?#].*)?$", re.I)
LOGIN_RE = re.compile(r"(login|signin|sign-in|log-in|consent|cookie-consent|privacy-consent)", re.I)
RELATIVE_RE = re.compile(r"(昨日|昨天|今天|上個月|本月|近期|日前|最近|去年|明年)")
BACKREF_RE = re.compile(r"(如上所述|如前所述|詳見|承上|上一節|下一節|如下圖|見上文)")
MAX_SITEMAP_FETCHES = 8
SITEMAP_INDEX_CHILD_LIMIT = 5


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    text: str = ""
    elapsed: float = 0.0
    error: str = ""


@dataclass
class GateResult:
    name: str
    status: str
    evidence: str


@dataclass
class PageReport:
    url: str
    final_url: str
    gates: list
    info: dict


class PoliteFetcher:
    def __init__(self, delay=1.5, timeout=15, accept_language=DEFAULT_ACCEPT_LANGUAGE):
        self.delay = delay
        self.timeout = timeout
        self.accept_language = accept_language
        self.last_by_host = {}

    def fetch(self, url, ua=DEFAULT_UA, method="GET", max_bytes=2_000_000):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return FetchResult(url=url, error=f"unsupported scheme: {parsed.scheme}")
        if method not in ("GET", "HEAD"):
            return FetchResult(url=url, error="method prohibited")
        if BINARY_EXT.search(parsed.path):
            return FetchResult(url=url, error="binary-looking URL skipped")
        host = parsed.netloc.lower()
        wait = self.delay - (time.time() - self.last_by_host.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_by_host[host] = time.time()
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,text/plain;q=0.9,*/*;q=0.2",
            "Accept-Language": self.accept_language,
        })
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = b"" if method == "HEAD" else resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    body = body[:max_bytes]
                headers = dict(resp.headers.items())
                return FetchResult(
                    url=url,
                    final_url=resp.geturl(),
                    status=getattr(resp, "status", resp.getcode()),
                    headers=headers,
                    body=body,
                    text=decode_body(body, headers),
                    elapsed=time.time() - start,
                )
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read(max_bytes)
            except Exception:
                pass
            headers = dict(e.headers.items()) if e.headers else {}
            return FetchResult(url=url, final_url=e.geturl() or url, status=e.code, headers=headers,
                               body=body, text=decode_body(body, headers), elapsed=time.time() - start,
                               error="")
        except Exception as e:
            return FetchResult(url=url, final_url=url, elapsed=time.time() - start, error=str(e))


def decode_body(body, headers):
    if not body:
        return ""
    ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
    m = re.search(r"charset=([\w.-]+)", ctype, re.I)
    encodings = [m.group(1)] if m else []
    encodings += ["utf-8", "utf-8-sig", "big5", "latin-1"]
    for enc in encodings:
        try:
            return body.decode(enc, errors="replace")
        except Exception:
            continue
    return body.decode("utf-8", errors="replace")


class SimpleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.text_parts = []
        self.h1_count = 0
        self.headings = []
        self.ldjson = []
        self.in_ldjson = False
        self.ld_buf = []
        self.meta = []
        self.links = []
        self.title_parts = []
        self.in_title = False
        self.img_missing_alt = 0
        self.time_datetimes = []
        self.password_input = False
        self.root_divs = 0
        self.body_open = False
        self.body_direct_nonempty = 0
        self.html_lang = ""

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        if tag == "html":
            self.html_lang = attrs.get("lang", "")
        if tag == "body":
            self.body_open = True
        if self.body_open and tag == "div":
            self.root_divs += 1
        if tag == "title":
            self.in_title = True
        if re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            if level == 1:
                self.h1_count += 1
            self.headings.append((level, clean_ws(attrs.get("aria-label", ""))))
        if tag == "script" and attrs.get("type", "").lower().split(";")[0].strip() == "application/ld+json":
            self.in_ldjson = True
            self.ld_buf = []
        if tag == "meta":
            self.meta.append(attrs)
        if tag == "link":
            self.links.append(attrs)
        if tag == "img" and not attrs.get("alt", "").strip():
            self.img_missing_alt += 1
        if tag == "time" and attrs.get("datetime"):
            self.time_datetimes.append(attrs["datetime"])
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.password_input = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style", "noscript", "template") and self.skip:
            if self.in_ldjson and tag == "script":
                self.ldjson.append("".join(self.ld_buf).strip())
                self.ld_buf = []
                self.in_ldjson = False
            self.skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_ldjson:
            self.ld_buf.append(data)
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.skip:
            return
        t = clean_ws(data)
        if t:
            self.text_parts.append(t)
            if self.body_open:
                self.body_direct_nonempty += 1


def parse_html(text):
    p = SimpleHTML()
    try:
        p.feed(text or "")
    except Exception:
        pass
    return p


def clean_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def visible_text(parser):
    return clean_ws(" ".join(parser.text_parts))


def script_bytes(raw):
    return sum(len(m.group(0).encode("utf-8", errors="ignore")) for m in re.finditer(r"<script\b[^>]*>.*?</script>", raw or "", re.I | re.S))


def main_content_chars(text):
    chunks = [c.strip() for c in re.split(r"(?:\n\s*){2,}|[。！？!?]\s+", text) if c.strip()]
    longest = max((len(c) for c in chunks), default=0)
    return max(len(text), longest)


def parse_date(value):
    if not value:
        return False
    v = str(value).strip()
    if not re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", v):
        return False
    v2 = v.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            datetime.strptime(v2[:10] if fmt in ("%Y-%m-%d", "%Y/%m/%d") else v2, fmt)
            return True
        except Exception:
            pass
    try:
        datetime.fromisoformat(v2)
        return True
    except Exception:
        return False


def flatten_jsonld(obj):
    out = []
    if isinstance(obj, list):
        for x in obj:
            out.extend(flatten_jsonld(x))
    elif isinstance(obj, dict):
        if "@graph" in obj:
            out.append(obj)
            out.extend(flatten_jsonld(obj.get("@graph")))
        else:
            out.append(obj)
    return out


def parse_jsonld(blocks):
    items, errors = [], []
    for i, block in enumerate(blocks, 1):
        try:
            obj = json.loads(block)
            items.extend(flatten_jsonld(obj))
        except Exception as e:
            errors.append(f"block {i}: {e}")
    return items, errors


def meta_value(parser, names):
    wanted = {n.lower() for n in names}
    for m in parser.meta:
        key = (m.get("name") or m.get("property") or "").lower()
        if key in wanted and m.get("content"):
            return m.get("content")
    return ""


def link_value(parser, rel_name):
    rel_name = rel_name.lower()
    for lnk in parser.links:
        rels = (lnk.get("rel") or "").lower().split()
        if rel_name in rels and lnk.get("href"):
            return lnk.get("href")
    return ""


def origin(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def absolute_url(base, path):
    return urllib.parse.urljoin(base, path)


def parse_robots(text):
    groups = []
    current_agents, current_rules, current_lines = [], [], []
    sitemaps = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = [x.strip() for x in line.split(":", 1)]
        lk = key.lower()
        if lk == "sitemap":
            sitemaps.append(value)
            continue
        if lk == "user-agent":
            if current_rules:
                groups.append((current_agents, current_rules, current_lines))
                current_agents, current_rules, current_lines = [], [], []
            current_agents.append(value.lower())
            current_lines.append((lineno, raw.rstrip()))
        elif lk in ("allow", "disallow") and current_agents:
            current_rules.append((lk, value, lineno, raw.rstrip()))
            current_lines.append((lineno, raw.rstrip()))
    if current_agents or current_rules:
        groups.append((current_agents, current_rules, current_lines))
    return groups, sitemaps


def robot_block_for(bot, groups):
    bot_l = bot.lower()
    matched = []
    for agents, rules, _lines in groups:
        if bot_l in agents or "*" in agents:
            specificity = 1 if bot_l in agents else 0
            matched.append((specificity, rules))
    if not matched:
        return None
    rules = sorted(matched, key=lambda x: x[0], reverse=True)[0][1]
    best = None
    for kind, path, lineno, raw in rules:
        if path == "":
            continue
        if robot_path_matches("/", path):
            score = len(path)
            if best is None or score > best[0] or (score == best[0] and kind == "allow"):
                best = (score, kind, lineno, raw)
    if best and best[1] == "disallow":
        return f"line {best[2]}: {best[3]}"
    return None


def robot_path_matches(target, pattern):
    if not pattern:
        return False
    if pattern == "/":
        return True
    escaped = re.escape(pattern).replace(r"\*", ".*")
    if escaped.endswith(r"\$"):
        escaped = escaped[:-2] + "$"
    return re.match(escaped, target) is not None


def sitemap_urls(fetcher, base_url, robots_text=""):
    _, robot_sitemaps = parse_robots(robots_text)
    fallbacks = [
        absolute_url(origin(base_url), "/sitemap.xml"),
        absolute_url(origin(base_url), "/sitemap_index.xml"),
    ]
    found_urls, lastmod_count, used = [], 0, ""
    tried = []
    fetch_count = 0

    def remaining_fetches():
        return MAX_SITEMAP_FETCHES - fetch_count

    def note(sm, result):
        tried.append(f"{sitemap_label(sm)}={result}")

    def parse_candidate(sm, allow_children=True):
        nonlocal fetch_count
        if remaining_fetches() <= 0:
            note(sm, "skipped fetch cap")
            return [], 0, ""
        fetch_count += 1
        res = fetcher.fetch(sm, max_bytes=5_000_000)
        if res.error or res.status != 200:
            note(sm, str(res.error or res.status))
            return [], 0, ""
        try:
            root = sitemap_xml_root(res)
            urls, lastmods, kind = urls_from_sitemap_root(root)
            if kind == "sitemapindex":
                if not allow_children:
                    note(sm, "index/not followed")
                    return [], 0, "sitemapindex"
                children = [
                    loc.text.strip()
                    for loc in root.findall(".//{*}sitemap/{*}loc")
                    if loc.text and loc.text.strip()
                ]
                child_urls, child_lastmods = [], 0
                for child in children[:SITEMAP_INDEX_CHILD_LIMIT]:
                    if remaining_fetches() <= 0:
                        break
                    cu, cl, _ck = parse_candidate(child, allow_children=False)
                    child_urls.extend(cu)
                    child_lastmods += cl
                suffix = "fetch cap reached" if children[:SITEMAP_INDEX_CHILD_LIMIT] and remaining_fetches() <= 0 else f"{len(children[:SITEMAP_INDEX_CHILD_LIMIT])} children"
                note(sm, f"index/{len(child_urls)} urls via {suffix}")
                return child_urls, child_lastmods, "sitemapindex"
            note(sm, sitemap_result_text(kind, len(urls), lastmods))
            return urls, lastmods, kind
        except Exception as e:
            note(sm, f"parse error {e}")
            return [], 0, ""

    for sm in robot_sitemaps:
        urls, lastmods, kind = parse_candidate(sm)
        if urls and not used:
            used = sm
        found_urls.extend(urls)
        lastmod_count += lastmods

    if not found_urls:
        declared_set = {normalize_url(sm) for sm in robot_sitemaps}
        for sm in fallbacks:
            if normalize_url(sm) in declared_set:
                continue
            urls, lastmods, kind = parse_candidate(sm)
            if urls and not used:
                used = sm
            found_urls.extend(urls)
            lastmod_count += lastmods
            if found_urls:
                break

    evidence = f"{len(robot_sitemaps)} declared: " + ("; ".join(tried) if tried else "none tried")
    return used, found_urls, lastmod_count, evidence


def sitemap_label(url):
    path = urllib.parse.urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def sitemap_xml_root(fetch_result):
    body = sitemap_body_bytes(fetch_result)
    return ET.fromstring(body)


def sitemap_body_bytes(fetch_result):
    body = fetch_result.body or b""
    enc = fetch_result.headers.get("Content-Encoding", "") or fetch_result.headers.get("content-encoding", "")
    path = urllib.parse.urlparse(fetch_result.final_url or fetch_result.url).path.lower()
    if "gzip" in enc.lower() or path.endswith(".gz") or body.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body


def urls_from_sitemap_root(root):
    tag = strip_ns(root.tag).lower()
    if tag == "sitemapindex":
        return [], 0, "sitemapindex"
    urls, lastmods = [], 0
    for u in root.findall(".//{*}url"):
        loc = u.find("{*}loc")
        if loc is not None and loc.text and loc.text.strip():
            urls.append(loc.text.strip())
        if u.find("{*}lastmod") is not None:
            lastmods += 1
    if urls:
        return urls, lastmods, "urlset"
    feed_urls = feed_urls_from_root(root)
    if feed_urls:
        return feed_urls, 0, "feed"
    return [], 0, tag or "xml"


def feed_urls_from_root(root):
    urls = []
    for entry in root.findall(".//{*}entry"):
        links = entry.findall("{*}link")
        href = ""
        for link in links:
            rel = (link.attrib.get("rel") or "alternate").lower()
            if rel == "alternate" and link.attrib.get("href"):
                href = link.attrib.get("href")
                break
            if not href and link.attrib.get("href"):
                href = link.attrib.get("href")
        if href:
            urls.append(href.strip())
    for item in root.findall(".//{*}item"):
        link = item.find("{*}link")
        if link is not None and link.text and link.text.strip():
            urls.append(link.text.strip())
    return urls


def sitemap_result_text(kind, count, lastmods):
    if kind == "feed":
        return f"feed/{count} entries"
    if kind == "urlset":
        return f"{count} urls ({lastmods} lastmod)"
    if count:
        return f"{kind}/{count} urls"
    return f"{kind}/0 urls"


def strip_ns(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def gate_results(url, fetcher, shared=None):
    shared = shared or {}
    page = fetcher.fetch(url)
    parser = parse_html(page.text)
    text = visible_text(parser)
    jsonld_items, jsonld_errors = parse_jsonld(parser.ldjson)
    gates = []

    def add(name, status, evidence):
        gates.append(GateResult(name, status, evidence))

    try:
        if page.error:
            add("Reachable without login", "ERROR", page.error)
        elif page.status == 200 and not LOGIN_RE.search(page.final_url or "") and not parser.password_input:
            add("Reachable without login", "PASS", f"GET 200; final URL {page.final_url}")
        else:
            reason = []
            if page.status != 200:
                reason.append(f"status {page.status}")
            if LOGIN_RE.search(page.final_url or ""):
                reason.append("final URL looks like login/consent")
            if parser.password_input:
                reason.append("password input found")
            add("Reachable without login", "FAIL", "; ".join(reason) or "blocked")
    except Exception as e:
        add("Reachable without login", "ERROR", str(e))

    try:
        chars = len(text)
        js_shell = (chars < 200 and script_bytes(page.text) > 100_000) or bool(re.search(r"<body[^>]*>\s*<div[^>]*(?:id=[\"'](?:root|app|__next)[\"'])?[^>]*>\s*</div>\s*(?:<script|\s*</body>)", page.text or "", re.I | re.S))
        status = "PASS" if chars >= 200 and not js_shell else "FAIL"
        add("Body text in the raw HTML", status, f"raw visible text {chars} chars; script bytes {script_bytes(page.text)}; JS shell={js_shell}")
    except Exception as e:
        add("Body text in the raw HTML", "ERROR", str(e))

    try:
        triad = []
        for label, ua in (("browser", BROWSER_UA), ("GPTBot", GPTBOT_UA), ("ClaudeBot", CLAUDEBOT_UA)):
            r = fetcher.fetch(url, ua=ua)
            triad.append((label, r.status, len(r.body), r.error))
        statuses = [x[1] for x in triad]
        lengths = [x[2] for x in triad]
        same_status = len(set(statuses)) == 1
        max_len, min_len = max(lengths), min(lengths)
        within = (max_len == 0 and min_len == 0) or (max_len > 0 and (max_len - min_len) / max_len <= 0.05)
        ev = "; ".join(f"{a}=({s},{l})" + (f" err={er}" if er else "") for a, s, l, er in triad)
        add("AI crawlers treated equally", "PASS" if same_status and within else "FAIL", ev)
    except Exception as e:
        add("AI crawlers treated equally", "ERROR", str(e))

    robots_res = None
    robots_text = ""
    try:
        robots_url = absolute_url(origin(url), "/robots.txt")
        robots_res = fetcher.fetch(robots_url)
        robots_text = robots_res.text if robots_res.status == 200 and not robots_res.error else ""
        if robots_res.error or robots_res.status != 200:
            add("robots.txt complete", "FAIL", f"{robots_url} unreachable: {robots_res.error or robots_res.status}")
        else:
            groups, sitemaps = parse_robots(robots_text)
            blocked = {bot: robot_block_for(bot, groups) for bot in AI_BOTS}
            blocked = {k: v for k, v in blocked.items() if v}
            status = "FAIL" if blocked else ("WARN" if not sitemaps else "PASS")
            ev = "blocked: " + (", ".join(f"{k} by {v}" for k, v in blocked.items()) or "none")
            if not sitemaps:
                ev += "; WARN no Sitemap directive"
            else:
                ev += f"; sitemap directives {len(sitemaps)}"
            add("robots.txt complete", status, ev)
    except Exception as e:
        add("robots.txt complete", "ERROR", str(e))

    sitemap_found = []
    sitemap_lastmod = 0
    try:
        used, sitemap_found, sitemap_lastmod, sm_evidence = sitemap_urls(fetcher, url, robots_text)
        if used and sitemap_found:
            add("Sitemap complete", "PASS", f"{sm_evidence}; total {len(sitemap_found)} URLs; {sitemap_lastmod} with lastmod")
        else:
            add("Sitemap complete", "FAIL", sm_evidence or "no sitemap URLs found")
    except Exception as e:
        add("Sitemap complete", "ERROR", str(e))

    try:
        levels = [level for level, _ in parser.headings]
        skips = []
        prev = None
        for level in levels:
            if prev is not None and level > prev + 1:
                skips.append(f"h{prev}->h{level}")
            prev = level
        outline = " ".join(f"h{lvl}" for lvl in levels) or "none"
        add("Exactly one h1 per page", "PASS" if parser.h1_count == 1 and not skips else "FAIL",
            f"h1 count {parser.h1_count}; outline {outline}; skips {', '.join(skips) or 'none'}")
    except Exception as e:
        add("Exactly one h1 per page", "ERROR", str(e))

    try:
        missing = [i for i in jsonld_items if not (isinstance(i, dict) and i.get("@type") and i.get("@context"))]
        types = []
        for item in jsonld_items:
            t = item.get("@type") if isinstance(item, dict) else None
            if isinstance(t, list):
                types.extend(map(str, t))
            elif t:
                types.append(str(t))
        if jsonld_errors:
            add("Structured data present", "FAIL", "JSON-LD parse errors: " + "; ".join(jsonld_errors))
        elif not jsonld_items:
            add("Structured data present", "FAIL", "no application/ld+json blocks found")
        elif missing:
            add("Structured data present", "FAIL", f"{len(missing)} JSON-LD item(s) missing @type or @context; types {types or 'none'}")
        else:
            add("Structured data present", "PASS", "types: " + (", ".join(types) or "none"))
    except Exception as e:
        add("Structured data present", "ERROR", str(e))

    try:
        author = ""
        date = ""
        for item in jsonld_items:
            if isinstance(item, dict):
                if not author and item.get("author"):
                    author = "JSON-LD author"
                for key in ("datePublished", "dateModified"):
                    if not date and parse_date(item.get(key)):
                        date = f"JSON-LD {key}={item.get(key)}"
        if not author and meta_value(parser, ["author"]):
            author = "meta author"
        if not author and link_value(parser, "author"):
            author = "rel=author"
        if not date:
            for dt in parser.time_datetimes:
                if parse_date(dt):
                    date = f"time datetime={dt}"
                    break
        if not date:
            mv = meta_value(parser, ["article:published_time", "article:modified_time", "date", "publish_date"])
            if parse_date(mv):
                date = f"meta date={mv}"
        add("Author and date machine-readable", "PASS" if author and date else "FAIL", f"author: {author or 'not found'}; date: {date or 'not found/invalid'}")
    except Exception as e:
        add("Author and date machine-readable", "ERROR", str(e))

    try:
        rel_hits = len(RELATIVE_RE.findall(text))
        back_hits = len(BACKREF_RE.findall(text))
        per_1000 = ((rel_hits + back_hits) / max(len(text), 1)) * 1000
        add("Self-contained paragraphs (heuristic)", "PASS" if per_1000 < 3 else "WARN",
            f"HEURISTIC relative={rel_hits}, back-reference={back_hits}, hits/1000 chars={per_1000:.2f}")
    except Exception as e:
        add("Self-contained paragraphs (heuristic)", "ERROR", str(e))

    add("Crawler hits observable", "MANUAL", "check server logs for GPTBot/ClaudeBot/PerplexityBot user-agent strings")

    og = [m for m in parser.meta if (m.get("property") or "").lower().startswith("og:")]
    info = {
        "title": clean_ws(" ".join(parser.title_parts)),
        "description": meta_value(parser, ["description"]),
        "canonical": link_value(parser, "canonical"),
        "html_lang": parser.html_lang,
        "og_tags_present": len(og),
        "img_missing_alt": parser.img_missing_alt,
        "llms_txt_exists": llms_exists(fetcher, url),
        "feed_linked": bool(feed_link(parser)),
        "response_time_sec": f"{page.elapsed:.3f}",
    }
    return PageReport(url=url, final_url=page.final_url or url, gates=gates, info=info), sitemap_found


def llms_exists(fetcher, url):
    r = fetcher.fetch(absolute_url(origin(url), "/llms.txt"), method="HEAD")
    if r.status == 405:
        r = fetcher.fetch(absolute_url(origin(url), "/llms.txt"))
    return bool(not r.error and r.status == 200)


def feed_link(parser):
    for lnk in parser.links:
        typ = (lnk.get("type") or "").lower()
        rel = (lnk.get("rel") or "").lower()
        if "alternate" in rel and typ in ("application/rss+xml", "application/atom+xml"):
            return lnk.get("href", "")
    return ""


def score(report):
    hard = report.gates[:9]
    passed = sum(1 for g in hard if g.status in ("PASS", "WARN"))
    return passed, 9


def fixes(report):
    order = ["Body text in the raw HTML", "Reachable without login", "AI crawlers treated equally", "robots.txt complete", "Sitemap complete",
             "Exactly one h1 per page", "Structured data present", "Author and date machine-readable", "Self-contained paragraphs (heuristic)"]
    by_name = {g.name: g for g in report.gates}
    items = []
    for name in order:
        g = by_name.get(name)
        if g and g.status not in ("PASS", "MANUAL"):
            items.append(f"{name}: {fix_text(name)}")
    return items


def fix_text(name):
    return {
        "Body text in the raw HTML": "Render the main content server-side, statically generate it, or add prerendering, so the raw HTML carries the body text.",
        "Reachable without login": "Remove the login wall, consent wall, or redirect that blocks public content; a public page must answer GET with 200.",
        "AI crawlers treated equally": "Check the CDN, WAF and bot rules so GPTBot, ClaudeBot and an ordinary browser all receive equivalent content.",
        "robots.txt complete": "Fix robots.txt: do not Disallow / for the major AI bots, and add a Sitemap directive.",
        "Sitemap complete": "Publish a parseable sitemap.xml, or a robots.txt Sitemap directive, listing at least the public URLs and ideally carrying lastmod.",
        "Exactly one h1 per page": "Keep exactly one h1 and step heading levels down in order (h2, then h3) without skipping.",
        "Structured data present": "Add valid application/ld+json carrying at least @context and @type, describing what the page actually contains.",
        "Author and date machine-readable": "Add a machine-readable author and an absolute date, for example JSON-LD author/datePublished, or meta/time elements.",
        "Self-contained paragraphs (heuristic)": "Replace relative time references with absolute dates, and cut down on cross-paragraph back-references.",
    }.get(name, "Fix according to the evidence.")


def write_html(path, reports):
    parts = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>AI crawlability gate report</title>",
             "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.7;margin:28px;color:#17202a}table{border-collapse:collapse;width:100%;margin:14px 0 28px}td,th{border:1px solid #d8dee4;padding:8px;vertical-align:top}th{background:#eef6f7}.PASS{color:#15803d;font-weight:700}.FAIL,.ERROR{color:#b91c1c;font-weight:700}.WARN{color:#a16207;font-weight:700}.MANUAL{color:#475569;font-weight:700}code{background:#eef2f7;padding:1px 4px}</style>",
             "<h1>AI crawlability gate report</h1>"]
    for report in reports:
        passed, total = score(report)
        parts.append(f"<h2>{html.escape(report.url)}</h2><p>Final URL: {html.escape(report.final_url)}<br>Hard gate score: {passed}/{total}</p>")
        parts.append("<table><tr><th>Status</th><th>Gate</th><th>Evidence</th></tr>")
        for g in report.gates:
            parts.append(f"<tr><td class='{g.status}'>{g.status}</td><td>{html.escape(g.name)}</td><td>{html.escape(g.evidence)}</td></tr>")
        parts.append("</table><h3>Additional information</h3><table>")
        for k, v in report.info.items():
            parts.append(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>")
        parts.append("</table><h3>Fix list</h3><ol>")
        fx = fixes(report)
        if fx:
            for item in fx:
                parts.append(f"<li>{html.escape(item)}</li>")
        else:
            parts.append("<li>No hard failures to fix.</li>")
        parts.append("</ol>")
    parts.append("</html>")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(parts))


def write_csv(path, reports):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "final_url", "gate", "status", "evidence"])
        for report in reports:
            for g in report.gates:
                w.writerow([report.url, report.final_url, g.name, g.status, g.evidence])


def report_to_dict(report):
    return {
        "url": report.url,
        "final_url": report.final_url,
        "gates": [g.__dict__ for g in report.gates],
        "info": report.info,
        "score": dict(zip(["passed", "total"], score(report))),
        "fixes": fixes(report),
    }


def print_report(report):
    if report.url != report.final_url:
        print(f"URL {report.url} -> {report.final_url}")
    for g in report.gates:
        print(f"[{g.status}] {g.name} — {g.evidence}")
    passed, total = score(report)
    print(f"score {passed}/{total} hard gates passed")
    fx = fixes(report)
    print("fix list:")
    if fx:
        for i, item in enumerate(fx, 1):
            print(f"{i}. {item}")
    else:
        print("none")


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI crawlability gate checker")
    ap.add_argument("url")
    ap.add_argument("--html", dest="html_path")
    ap.add_argument("--csv", dest="csv_path")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--crawl", type=int, default=0)
    ap.add_argument("--accept-language", dest="accept_language", default=DEFAULT_ACCEPT_LANGUAGE,
                    help=f"Accept-Language header sent with every request (default: {DEFAULT_ACCEPT_LANGUAGE!r})")
    args = ap.parse_args(argv)

    fetcher = PoliteFetcher(accept_language=args.accept_language)
    first, sm_urls = gate_results(args.url, fetcher)
    reports = [first]
    crawl_n = max(0, min(args.crawl, 50))
    seen = {normalize_url(args.url), normalize_url(first.final_url)}
    for u in sm_urls:
        if len(reports) >= crawl_n + 1:
            break
        if normalize_url(u) in seen or BINARY_EXT.search(urllib.parse.urlparse(u).path):
            continue
        seen.add(normalize_url(u))
        r, _ = gate_results(u, fetcher)
        reports.append(r)

    for idx, report in enumerate(reports):
        if idx:
            print("")
        print_report(report)
    if args.json:
        print(json.dumps([report_to_dict(r) for r in reports], ensure_ascii=False, indent=2))
    if args.html_path:
        write_html(args.html_path, reports)
    if args.csv_path:
        write_csv(args.csv_path, reports)
    all_pass = all(
        g.status not in ("FAIL", "ERROR")
        for r in reports
        for g in r.gates[:9]
        if g.name != "Self-contained paragraphs (heuristic)"
    )
    return 0 if all_pass else 1


def normalize_url(url):
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", p.query, ""))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR interrupted", file=sys.stderr)
        raise SystemExit(1)
