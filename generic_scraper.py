#!/usr/bin/env python3
"""
Generic config-driven web scraper  (listing pages -> detail pages -> SQLite/JSON/CSV)

Nothing site-specific lives in this file. Everything that describes a website -
URL patterns, pagination, CSS selectors, which cards to ignore, what to
download - lives in a separate JSON / YAML / TOML "site rules" file.

Quick start (public practice site, no browser needed):
    pip install beautifulsoup4 lxml
    python generic_scraper.py --config sites/books_toscrape.json --test
    python generic_scraper.py --config sites/books_toscrape.json --max-pages 2 --download thumbnails

Optional extras:
    pip install selenium      # only for sites with an age-gate / JavaScript-rendered pages
    pip install pyyaml        # only if you prefer YAML configs

Design in short
  * Pages are fetched as plain HTML over HTTP by a small thread pool
    (retry + back-off on 429/5xx, gzip, robots.txt respected by default).
  * A real browser (Selenium) is used only when the config asks for it
    ("age_gate", "use_browser") or as a fallback for pages that failed.
  * Results go into <out>/<site name>/items.db (SQLite). Re-running skips
    items whose detail page was already scraped. JSON and CSV are exported
    at the end.

Always check a site's terms of service and robots.txt before scraping it.
"""

import argparse
import csv
import gzip
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:  # Selenium is optional: only needed for age gates / JS-rendered sites
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    HAVE_SELENIUM = True
except ImportError:
    HAVE_SELENIUM = False

DEFAULT_UA = "generic-scraper/1.0 (portfolio project)"
SELECTOR_KEYS = {"selector", "item_selector", "root_selector", "wait_selector", "next_selector",
                 "row_selector", "key_selector", "value_selector", "click_selector"}


# ----------------------------------------------------------------- small helpers
def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


def make_soup(html):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # lxml missing -> built-in parser
        return BeautifulSoup(html, "html.parser")


# ----------------------------------------------------------------- site-rules file
def _strip_comments(x):
    """Keys starting with '_' (e.g. "_comment") are documentation only."""
    if isinstance(x, dict):
        return {k: _strip_comments(v) for k, v in x.items() if not str(k).startswith("_")}
    if isinstance(x, list):
        return [_strip_comments(v) for v in x]
    return x


def _check_selectors_and_regex(cfg):
    import soupsieve as sv  # installed together with beautifulsoup4
    errors = []

    def walk(x, path):
        if isinstance(x, dict):
            for k, v in x.items():
                p = f"{path}.{k}"
                if k in SELECTOR_KEYS and isinstance(v, str):
                    try:
                        sv.compile(v)
                    except Exception as e:
                        errors.append(f"{p} = {v!r} is not a valid CSS selector ({e})")
                elif k == "regex" and isinstance(v, str):
                    try:
                        re.compile(v)
                    except re.error as e:
                        errors.append(f"{p} = {v!r} is not a valid regular expression ({e})")
                else:
                    walk(v, p)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}[{i}]")

    walk(cfg, "config")
    return errors


def load_config(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        sys.exit(f"Cannot read config file: {e}")
    try:
        if ext == ".json":
            cfg = json.loads(raw.decode("utf-8-sig"))
        elif ext in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                sys.exit("YAML configs need PyYAML (pip install pyyaml). Or use a .json config.")
            cfg = yaml.safe_load(raw)
        elif ext == ".toml":
            try:
                import tomllib
            except ImportError:
                sys.exit("TOML configs need Python 3.11+. Or use a .json config.")
            cfg = tomllib.loads(raw.decode("utf-8"))
        else:
            sys.exit("Config file must end in .json, .yaml/.yml or .toml")
    except Exception as e:  # SystemExit (missing PyYAML etc.) is not an Exception and passes through
        sys.exit(f"Config file is not valid {ext[1:].upper()}: {e}")

    cfg = _strip_comments(cfg)
    errs = []
    base = cfg.get("base_url")
    if not (isinstance(base, str) and base.startswith("http")):
        errs.append("base_url must be a full URL such as https://example.com")
    else:
        cfg["base_url"] = base.rstrip("/")
        cfg.setdefault("name", safe_name(urlparse(base).netloc))

    pg = cfg.get("pagination")
    if not isinstance(pg, dict) or pg.get("mode") not in ("pattern", "next_link"):
        errs.append("pagination.mode must be 'pattern' or 'next_link'")
    elif pg["mode"] == "pattern":
        if "{page}" not in str(pg.get("url_pattern", "")):
            errs.append("pagination.url_pattern is required and must contain {page} (and optionally {base})")
    else:
        if not pg.get("first_url") or not pg.get("next_selector"):
            errs.append("pagination.first_url and pagination.next_selector are required for mode 'next_link'")

    lc = cfg.get("listing")
    if not isinstance(lc, dict) or not lc.get("item_selector"):
        errs.append("listing.item_selector is required")
    elif not isinstance(lc.get("link"), dict):
        errs.append("listing.link is required, e.g. {\"selector\": \"h3 a\", \"attr\": \"href\"}")

    dc = cfg.get("detail")
    if dc is not None and dc.get("enabled", True) and not (dc.get("fields") or dc.get("pairs")):
        errs.append("detail needs at least one entry in 'fields' or 'pairs' (or set \"enabled\": false)")

    errs += _check_selectors_and_regex(cfg)
    if errs:
        sys.exit("Problems in the config file:\n  - " + "\n  - ".join(errs))
    return cfg


# ----------------------------------------------------------------- extraction rules
def extract(node, spec, page_url):
    """Apply one field rule to a BeautifulSoup node.

    spec is either a CSS selector string, or a dict:
      selector     CSS selector (omit to use the node itself)
      attr         "text" (default) or an attribute name such as "href", "src", "title", "class"
      regex        keep the first capture group (or whole match) of this regex
      replace      list of [old, new] text replacements
      strip_chars  characters to trim from both ends, e.g. ",:"
      absolute     true -> turn a relative URL into an absolute one
      skip         ignore the first N matches
      multiple     true -> return a list of all matches instead of the first
      default      value to use when nothing matches
    """
    if isinstance(spec, str):
        spec = {"selector": spec}
    sel = spec.get("selector")
    els = node.select(sel) if sel else [node]
    els = els[spec.get("skip", 0):]
    attr = spec.get("attr", "text")
    vals = []
    for el in els:
        if attr == "text":
            v = clean(el.get_text(" "))
        else:
            v = el.get(attr, "")
            if isinstance(v, list):  # e.g. class="star-rating Three"
                v = " ".join(v)
            v = clean(v)
        rx = spec.get("regex")
        if rx and v:
            m = re.search(rx, v)
            v = (m.group(1) if m.groups() else m.group(0)) if m else ""
        for old, new in spec.get("replace", []):
            v = v.replace(old, new)
        if spec.get("strip_chars"):
            v = v.strip(spec["strip_chars"] + " ")
        if spec.get("absolute") and v:
            v = urljoin(page_url, v)
        if v:
            vals.append(v)
    if spec.get("multiple"):
        return vals
    return vals[0] if vals else spec.get("default", "")


def extract_pairs(node, spec):
    """Label/value rows (tables, definition lists, 'Studio: X' lines) -> {label: value}.
    spec: row_selector, key_selector, value_selector, multiple (keep list), strip_chars."""
    out = {}
    strip = spec.get("strip_chars", ",") + " "
    for row in node.select(spec["row_selector"]):
        k_el = row.select_one(spec["key_selector"])
        if k_el is None:
            continue
        key = clean(k_el.get_text(" ")).rstrip(":").strip()
        if not key:
            continue
        vals = []
        for v_el in row.select(spec["value_selector"]):
            if v_el is k_el:
                continue
            txt = clean(v_el.get_text(" ")).strip(strip)
            if txt:
                vals.append(txt)
        if vals:
            out[key] = vals if spec.get("multiple") else ", ".join(vals)
    return out


def is_ignored(item, rules, site_host):
    """rules: [{"field": "title", "equals": "..."}, {"field": "url", "contains": "..."}, {"external": true}]"""
    for r in rules or []:
        if r.get("external"):
            if urlparse(item["url"]).netloc.lower().replace("www.", "") != site_host:
                return True
            continue
        val = str(item.get(r.get("field", "title"), "")).lower()
        if "equals" in r and val == str(r["equals"]).lower():
            return True
        if "contains" in r and str(r["contains"]).lower() in val:
            return True
    return False


def parse_listing(html, page_url, cfg):
    lc = cfg["listing"]
    site_host = urlparse(cfg["base_url"]).netloc.lower().replace("www.", "")
    link_spec = dict(lc["link"])
    link_spec.setdefault("attr", "href")
    link_spec.setdefault("absolute", True)
    soup = make_soup(html)
    items, skipped = [], 0
    for card in soup.select(lc["item_selector"]):
        url = extract(card, link_spec, page_url)
        if not url:
            continue
        item = {"url": url}
        for name, spec in lc.get("fields", {}).items():
            item[name] = extract(card, spec, page_url)
        if is_ignored(item, lc.get("ignore"), site_host):
            skipped += 1
            continue
        items.append(item)
    return items, skipped


def parse_detail(html, page_url, dc):
    soup = make_soup(html)
    root = soup.select_one(dc["root_selector"]) if dc.get("root_selector") else soup
    if root is None:
        return None
    out = {}
    for name, spec in dc.get("fields", {}).items():
        scope = soup if isinstance(spec, dict) and spec.get("scope") == "page" else root
        out[name] = extract(scope, spec, page_url)
    for name, spec in dc.get("pairs", {}).items():
        scope = soup if spec.get("scope") == "page" else root
        out[name] = extract_pairs(scope, spec)
    return out


def total_pages_from_first_page(html, url, pg):
    soup = make_soup(html)
    if pg.get("total_pages"):
        return int(pg["total_pages"])
    if pg.get("total_pages_from"):
        digits = re.sub(r"\D", "", str(extract(soup, pg["total_pages_from"], url)))
        return int(digits) if digits else None
    if pg.get("total_items_from") and pg.get("items_per_page"):
        digits = re.sub(r"\D", "", str(extract(soup, pg["total_items_from"], url)))
        return math.ceil(int(digits) / int(pg["items_per_page"])) if digits else None
    return None  # unknown -> keep going until a page has no items


def find_next_url(html, url, pg):
    el = make_soup(html).select_one(pg["next_selector"])
    return urljoin(url, el["href"]) if el is not None and el.get("href") else None


# ----------------------------------------------------------------- storage
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS items (
                   url TEXT PRIMARY KEY, listing TEXT, detail TEXT,
                   detail_done INTEGER DEFAULT 0, scraped_at TEXT)""")
        self.db.commit()

    def is_done(self, url):
        r = self.db.execute("SELECT detail_done FROM items WHERE url=?", (url,)).fetchone()
        return bool(r and r[0])

    def save_listing(self, url, listing):
        self.db.execute(
            "INSERT INTO items(url, listing) VALUES(?, ?) "
            "ON CONFLICT(url) DO UPDATE SET listing=excluded.listing",
            (url, json.dumps(listing, ensure_ascii=False)))

    def save_detail(self, url, detail):
        self.db.execute(
            "UPDATE items SET detail=?, detail_done=1, scraped_at=? WHERE url=?",
            (json.dumps(detail, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), url))

    def commit(self):
        self.db.commit()

    def purge_ignored(self, rules, site_host):
        """Remove never-detailed rows from earlier runs that now match the ignore rules."""
        n = 0
        for url, listing in self.db.execute("SELECT url, listing FROM items WHERE detail_done=0").fetchall():
            item = json.loads(listing) if listing else {}
            item["url"] = url
            if is_ignored(item, rules, site_host):
                self.db.execute("DELETE FROM items WHERE url=?", (url,))
                n += 1
        self.db.commit()
        return n

    def all_rows(self):
        return [{"url": u, "listing": json.loads(l) if l else {}, "detail": json.loads(d) if d else {}}
                for u, l, d in self.db.execute("SELECT url, listing, detail FROM items")]


def export(store, out_dir):
    rows = store.all_rows()
    with open(os.path.join(out_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    def j(v):
        return "; ".join(map(str, v)) if isinstance(v, list) else str(v)

    def flat(prefix, data):
        out = {}
        for k, v in (data or {}).items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    out[f"{prefix}{k}.{k2}"] = j(v2)
            else:
                out[f"{prefix}{k}"] = j(v)
        return out

    flat_rows, cols = [], ["url"]
    for r in rows:
        listing = {k: v for k, v in r["listing"].items() if k != "url"}
        fr = {"url": r["url"], **flat("", listing), **flat("detail.", r["detail"])}
        flat_rows.append(fr)
        for c in fr:
            if c not in cols:
                cols.append(c)
    with open(os.path.join(out_dir, "items.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(flat_rows)
    log(f"Exported {len(rows)} items -> items.json / items.csv")


# ----------------------------------------------------------------- robots.txt
class Robots:
    def __init__(self, base, ua, enabled):
        self.ua, self.rp, self.crawl_delay = ua, None, None
        if not enabled:
            return
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(urljoin(base + "/", "/robots.txt"), headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=20) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
            self.rp, self.crawl_delay = rp, rp.crawl_delay(ua)
        except Exception:
            self.rp = None  # no robots.txt (or unreachable): nothing to enforce

    def allowed(self, url):
        return True if self.rp is None else self.rp.can_fetch(self.ua, url)


# ----------------------------------------------------------------- HTTP fetcher (thread-safe)
class HttpFetcher:
    def __init__(self, base, ua, cookies, delay, robots, headers=None):
        self.base, self.host = base, urlparse(base).netloc
        self.ua, self.delay, self.robots = ua, delay, robots
        self.extra_headers = headers or {}
        self.pause_until, self.lock = 0.0, threading.Lock()
        self._warned = set()
        self.cookie = ""
        self.set_cookies(cookies)

    def set_cookies(self, cookies):
        parts = []
        for c in cookies:
            dom = (c.get("domain") or "").lstrip(".")
            if not dom or self.host == dom or self.host.endswith("." + dom):
                parts.append(f"{c['name']}={c['value']}")
        self.cookie = "; ".join(parts)

    def allowed(self, url):
        ok = self.robots.allowed(url)
        if not ok and url not in self._warned and len(self._warned) < 5:
            self._warned.add(url)
            log(f"  skipped (disallowed by robots.txt): {url}")
        return ok

    def _open(self, url, binary, retries=4):
        if not self.allowed(url):
            return None
        headers = {"User-Agent": self.ua, "Referer": self.base + "/", "Accept-Language": "en-US,en;q=0.9",
                   "Accept": "*/*" if binary else "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
        if not binary:
            headers["Accept-Encoding"] = "gzip"
        headers.update(self.extra_headers)
        if self.cookie and urlparse(url).netloc == self.host:  # never send site cookies elsewhere
            headers["Cookie"] = self.cookie
        for attempt in range(1, retries + 1):
            wait_s = self.pause_until - time.time()
            if wait_s > 0:
                time.sleep(wait_s)
            try:
                return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504):
                    try:
                        w = int(e.headers.get("Retry-After", ""))
                    except ValueError:
                        w = 5 * attempt
                    with self.lock:  # slow every worker down, not just this one
                        self.pause_until = max(self.pause_until, time.time() + min(w, 120))
                elif e.code in (400, 401, 403, 404, 410):
                    return None
                else:
                    time.sleep(attempt)
            except Exception:
                time.sleep(attempt)
        return None

    def get_html(self, url):
        r = self._open(url, binary=False)
        if r is None:
            return None
        try:
            with r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except Exception:
            return None

    def download(self, url, dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return True
        r = self._open(url, binary=True)
        if r is None:
            return False
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + f".{threading.get_ident()}.part"
        try:
            with r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dest)
            return True
        except Exception as e:
            log(f"  download error: {url}: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            return False


# ----------------------------------------------------------------- optional browser (age gate / JS / fallback)
class Browser:
    def __init__(self, name, headless):
        if not HAVE_SELENIUM:
            sys.exit("This config needs a real browser step. Install Selenium:  pip install selenium")
        if name == "firefox":
            o = FirefoxOptions()
            if headless:
                o.add_argument("-headless")
            o.page_load_strategy = "eager"
            o.set_preference("permissions.default.image", 2)
            self.driver = webdriver.Firefox(options=o)
        else:
            o = EdgeOptions() if name == "edge" else ChromeOptions()
            if headless:
                o.add_argument("--headless=new")
            o.add_argument("--window-size=1366,900")
            o.add_argument("--log-level=3")
            o.page_load_strategy = "eager"
            o.add_experimental_option("excludeSwitches", ["enable-logging"])
            o.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
            self.driver = webdriver.Edge(options=o) if name == "edge" else webdriver.Chrome(options=o)
        self.driver.set_page_load_timeout(60)
        self.age_selector = None

    def _load(self, url):
        try:
            self.driver.get(url)
        except TimeoutException:
            self.driver.execute_script("window.stop();")

    def click(self, selector, timeout=4):
        try:
            btn = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            try:
                btn.click()
            except WebDriverException:
                self.driver.execute_script("arguments[0].click();", btn)
            time.sleep(1)
            return True
        except TimeoutException:
            return False

    def open_site(self, url, age_selector):
        self.age_selector = age_selector
        self._load(url)
        if age_selector:
            self.click(age_selector)

    def get_html(self, url, wait_selector=None, page_wait=10):
        for attempt in range(1, 4):
            try:
                self._load(url)
                if self.age_selector:
                    els = self.driver.find_elements(By.CSS_SELECTOR, self.age_selector)
                    if els and els[0].is_displayed():
                        self.click(self.age_selector, 2)
                        self._load(url)
                if wait_selector:
                    try:
                        WebDriverWait(self.driver, page_wait).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
                    except TimeoutException:
                        pass
                return self.driver.page_source
            except WebDriverException as e:
                log(f"  browser retry {attempt}/3 for {url}: {str(e).splitlines()[0]}")
                time.sleep(2 * attempt)
        return None

    def user_agent(self):
        return self.driver.execute_script("return navigator.userAgent")

    def cookies(self):
        return self.driver.get_cookies()

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Generic config-driven scraper")
    ap.add_argument("--config", required=True, help="Site rules file (.json / .yaml / .toml)")
    ap.add_argument("--out", default="output", help="Output folder (a sub-folder per site is created)")
    ap.add_argument("--test", action="store_true",
                    help="Dry run: fetch the first listing page + first detail page, print what was "
                         "extracted, write nothing")
    ap.add_argument("--workers", type=int, default=None, help="Parallel connections (default: config or 4)")
    ap.add_argument("--delay", type=float, default=None, help="Seconds each worker pauses after a request")
    ap.add_argument("--start-page", type=int, default=1, help="First listing page to process")
    ap.add_argument("--max-pages", type=int, default=0, help="Process at most this many listing pages")
    ap.add_argument("--limit", type=int, default=0, help="Stop after this many items (handy for demos)")
    ap.add_argument("--no-detail", action="store_true", help="Listing data only")
    ap.add_argument("--download", action="append", default=[],
                    help="Name of a 'downloads' entry in the config, or 'all'. Can be repeated")
    ap.add_argument("--use-browser", action="store_true", help="Load every page in the browser (JS-rendered sites)")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge", "firefox"])
    ap.add_argument("--headed", action="store_true", help="Show the browser window")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = cfg["base_url"]
    host = urlparse(base).netloc.lower().replace("www.", "")
    pg, lc = cfg["pagination"], cfg["listing"]
    dc = cfg.get("detail") or {}
    want_detail = bool(dc) and dc.get("enabled", True) and not args.no_detail
    workers = max(1, args.workers or cfg.get("workers", 4))
    delay = args.delay if args.delay is not None else cfg.get("delay", 0.5)
    ua = cfg.get("user_agent") or DEFAULT_UA

    robots = Robots(base, ua, cfg.get("respect_robots", True))
    if robots.crawl_delay:
        delay = max(delay, float(robots.crawl_delay))
        log(f"robots.txt asks for a crawl delay of {robots.crawl_delay}s - using it")

    age_selector = (cfg.get("age_gate") or {}).get("click_selector")
    need_browser = bool(age_selector) or args.use_browser or cfg.get("use_browser", False)
    state = {"http": not (args.use_browser or cfg.get("use_browser", False))}

    br, cookies = None, []
    if need_browser:
        try:
            br = Browser(args.browser, not args.headed)
        except Exception as e:
            sys.exit(f"Could not start {args.browser}: {str(e).splitlines()[0]}")
        log(f"Opening {base} in the browser")
        br.open_site(base, age_selector)
        ua, cookies = br.user_agent(), br.cookies()

    fetcher = HttpFetcher(base, ua, cookies, delay, robots, cfg.get("headers"))
    ex = ThreadPoolExecutor(max_workers=workers)
    store = None

    def get_page(url, wait_sel, check=None):
        """HTTP first; the browser (if configured) is the fallback."""
        if not robots.allowed(url):
            return None
        if state["http"]:
            html = fetcher.get_html(url)
            if html and (check is None or check(html)):
                return html
        if br:
            html = br.get_html(url, wait_sel)
            fetcher.set_cookies(br.cookies())
            return html
        return None

    try:
        # ---- first listing page (also gives the total page count)
        first_num = int(pg.get("first_page_number", 1))

        def url_for(n):
            num = first_num + n - 1
            tmpl = pg["first_url"] if (n == 1 and pg.get("first_url")) else pg["url_pattern"]
            return tmpl.replace("{base}", base).replace("{page}", str(num))

        list_wait = lc.get("wait_selector") or lc["item_selector"]
        first_url = url_for(1)
        has_items = lambda h: bool(parse_listing(h, first_url, cfg)[0])  # noqa: E731
        first_html = get_page(first_url, list_wait, has_items)
        if not first_html:
            sys.exit(f"Could not load the first listing page: {first_url}")
        first_items, _ = parse_listing(first_html, first_url, cfg)
        if not first_items:
            sys.exit(f"No items found on {first_url}. Check listing.item_selector / listing.link "
                     "in your config (use --test to experiment).")
        total = total_pages_from_first_page(first_html, first_url, pg) if pg["mode"] == "pattern" else None

        # ---- dry run
        if args.test:
            items, ads = parse_listing(first_html, first_url, cfg)
            print(f"\nListing page: {len(items)} items found ({ads} ignored by rules); "
                  f"total pages: {total if total else 'unknown'}")
            for it in items[:2]:
                print(json.dumps(it, indent=2, ensure_ascii=False))
            if want_detail:
                h = get_page(items[0]["url"], dc.get("wait_selector") or dc.get("root_selector"))
                d = parse_detail(h, items[0]["url"], dc) if h else None
                print("\nDetail page of the first item:")
                print(json.dumps(d, indent=2, ensure_ascii=False) if d is not None else
                      "  -> nothing found. Check detail.root_selector / detail.fields.")
            return

        out_dir = os.path.join(args.out, cfg["name"])
        os.makedirs(out_dir, exist_ok=True)
        store = Store(os.path.join(out_dir, "items.db"))
        if lc.get("ignore"):
            removed = store.purge_ignored(lc["ignore"], host)
            if removed:
                log(f"Removed {removed} earlier-saved entries that now match the ignore rules")

        downloads = cfg.get("downloads", {})
        wanted = set(downloads) if "all" in args.download else set(args.download)
        unknown = wanted - set(downloads)
        if unknown:
            sys.exit(f"Unknown --download name(s): {', '.join(sorted(unknown))}. "
                     f"Defined in config: {', '.join(downloads) or 'none'}")

        log(f"Site: {cfg['name']} | pages: {total if total else 'until empty'} | workers: {workers} | "
            f"delay: {delay}s | mode: {'HTTP' if state['http'] else 'browser'}"
            f"{' + browser fallback' if br and state['http'] else ''}")

        def fetch_detail(it):
            try:
                h = fetcher.get_html(it["url"])
                d = parse_detail(h, it["url"], dc) if h else None
            except Exception as e:
                log(f"  parse error for {it['url']}: {e}")
                d = None
            time.sleep(delay)
            return it, d

        # ---- walk listing pages
        n, processed, seen, next_url = 0, 0, 0, None
        while True:
            n += 1
            if pg["mode"] == "pattern" and n < args.start_page:
                n = args.start_page  # jump straight to the first wanted page
            if n == 1:
                url, html = first_url, first_html
            else:
                url = url_for(n) if pg["mode"] == "pattern" else next_url
                chk = lambda h, u=url: bool(parse_listing(h, u, cfg)[0])  # noqa: E731
                html = get_page(url, list_wait, chk)
                if not html:
                    log(f"Page {n}: could not load {url}")
                    if pg["mode"] == "next_link" or not total:
                        break
                    continue
            items, ads = parse_listing(html, url, cfg)
            if pg["mode"] == "next_link":
                next_url = find_next_url(html, url, pg)
            if not items and not total:
                log(f"Page {n}: no items - end of listing")
                break

            if n >= args.start_page:
                if args.limit:
                    items = items[: max(0, args.limit - seen)]
                seen += len(items)
                t0 = time.time()
                for it in items:
                    store.save_listing(it["url"], it)
                store.commit()

                dl = []
                for name in wanted:
                    spec = downloads[name]
                    for it in items:
                        src = it.get(spec["field"])
                        if not src or not isinstance(src, str):
                            continue
                        vid = str(it.get("id") or safe_name(urlparse(it["url"]).path.strip("/")))
                        ext = os.path.splitext(urlparse(src).path)[1] or spec.get("default_ext", "")
                        dl.append(ex.submit(fetcher.download, src,
                                            os.path.join(out_dir, spec.get("folder", name), f"{vid}{ext}")))

                todo = [it for it in items if want_detail and not store.is_done(it["url"])]
                saved, failed = 0, []
                if state["http"] and todo:
                    for f in as_completed([ex.submit(fetch_detail, it) for it in todo]):
                        it, d = f.result()
                        if d is not None:
                            store.save_detail(it["url"], d)
                            saved += 1
                        else:
                            failed.append(it)
                    if saved == 0 and len(todo) >= 3 and br:
                        state["http"] = False
                        log("HTTP fetching of detail pages is not working here -> switching to the browser.")
                else:
                    failed = todo
                for it in failed:  # slow path: browser fallback
                    if not br:
                        log(f"  no detail data (and no browser configured): {it['url']}")
                        continue
                    if not robots.allowed(it["url"]):
                        continue
                    h = br.get_html(it["url"], dc.get("wait_selector") or dc.get("root_selector"))
                    d = parse_detail(h, it["url"], dc) if h else None
                    if d is not None:
                        store.save_detail(it["url"], d)
                        saved += 1
                    else:
                        log(f"  detail content not found: {it['url']}")
                if failed and br and state["http"]:
                    fetcher.set_cookies(br.cookies())
                store.commit()
                wait(dl)
                processed += 1
                log(f"Page {n}{f'/{total}' if total else ''}: {len(items)} listed, {len(todo)} new, {saved} saved"
                    f"{f' ({len(failed)} via browser/failed)' if failed else ''}"
                    f"{f', {ads} ignored' if ads else ''} in {time.time() - t0:.1f}s")

            if args.max_pages and processed >= args.max_pages:
                break
            if args.limit and seen >= args.limit:
                break
            if pg["mode"] == "pattern" and total and n >= total:
                break
            if pg["mode"] == "next_link" and not next_url:
                break
    except KeyboardInterrupt:
        log("Interrupted - progress is saved, run again to continue.")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        if store:
            store.commit()
        if br:
            br.close()

    if store:
        export(store, os.path.join(args.out, cfg["name"]))
    log("Done.")


if __name__ == "__main__":
    main()
