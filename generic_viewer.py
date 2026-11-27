#!/usr/bin/env python3
"""
Generic viewer for data collected by generic_scraper.py.   (Python standard library only)

It does not know your site in advance. It reads <site folder>/items.db, looks at
every field the scraper stored, works out each field's type from the data
(text, category, list, number, date, image URL, video URL) and builds the
search, filters, sorting, grouping and "browse data" panels from that.

Usage:
    python generic_viewer.py --dir output/books_toscrape
    python generic_viewer.py --config sites/books_toscrape.json          # finds output/<name>/ and uses its hints
    python generic_viewer.py --dir output/mysite --host 0.0.0.0 --password secret   # share on your network

Optional hints: add a "viewer" section to the site rules file (and pass --config):
    "viewer": {
      "title": "title",                 # field used as card title
      "image": "thumbnail",             # field holding the image URL
      "preview": "preview",             # field holding a short video URL (plays on hover)
      "card_fields": ["price", "rating"],  # fields shown on each card
      "hide": ["availability"],         # fields not shown (still searchable)
      "types": {"price": "number"},     # force a type: text|category|list|number|date|image|video|url
      "labels": {"upc": "UPC"},         # nicer field names
      "default_sort": {"field": "price", "dir": "desc"},
      "page_size": 48
    }

Features: search box (phrases, -exclude), any number of filters (text / number /
date aware), sorting by any field, grouping, "Browse data" lists of every value
with counts (CSV download), saved views (stored in <site folder>/views.json),
local thumbnails/previews when the scraper downloaded them, optional password.
"""

import argparse
import base64
import calendar
import hmac
import json
import mimetypes
import os
import re
import socket
import sqlite3
import sys
import threading
import webbrowser
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")
IMAGE_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg)(\?|#|$)", re.I)
VIDEO_RE = re.compile(r"\.(mp4|webm|mov|m4v)(\?|#|$)", re.I)
# a number may start with a currency symbol / code and end with a short unit ("£12.50", "6.4K", "84%", "120 Min")
_CUR = r"(?:[^\w\s.\-+]{1,2}\s?|(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|INR|PKR|CNY|Rs\.?)\s?)?"
NUM_RE = re.compile(rf"^{_CUR}(-?\d[\d,]*(?:\.\d+)?)\s*([kKmM](?![A-Za-z]))?[^\d]{{0,8}}$")
DUR_RE = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{2})$")
DUR_WORDS_RE = re.compile(r"^(?:(\d+)\s*h(?:ours?|rs?)?\s*)?(\d+)\s*m(?:in(?:ute)?s?)?$", re.I)
DATE_FORMATS = ("%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d.%m.%Y", "%B %d %Y")
TYPES = ("text", "category", "list", "number", "duration", "date", "image", "video", "url")
ALLOWED_VIEW_KEYS = ("q", "filters", "sort", "dir", "group", "size")
SCRAPED = "_scraped"  # pseudo-field: the day the item's details were scraped


# ------------------------------------------------------------ parsing helpers
def safe_name(s):  # must match generic_scraper.py (download file names)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


def parse_number(s):
    m = NUM_RE.match(str(s).strip())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)


def parse_duration_min(s):
    """'2:38:04', '38:04', '120 Min', '1 h 30 min' -> minutes (float), else None."""
    s = str(s).strip()
    m = DUR_RE.match(s)
    if m:
        return int(m.group(1) or 0) * 60 + int(m.group(2)) + int(m.group(3)) / 60
    m = DUR_WORDS_RE.match(s)
    if m:
        return int(m.group(1) or 0) * 60 + int(m.group(2))
    return None


def parse_date(s):
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2})?)?", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def pretty(name):
    t = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    return t[:1].upper() + t[1:]


def first(v):
    return v[0] if isinstance(v, list) else v


def infer_type(values, n_records, is_list):
    """Guess a field's type from its non-empty values."""
    if not values:
        return "text"
    n = len(values)
    share = lambda pred: sum(1 for v in values if pred(v)) / n  # noqa: E731
    if share(lambda v: IMAGE_RE.search(v) is not None) >= 0.8:
        return "image"
    if share(lambda v: VIDEO_RE.search(v) is not None) >= 0.8:
        return "video"
    if share(lambda v: v.lower().startswith(("http://", "https://")) and " " not in v) >= 0.8:
        return "url"
    if is_list:
        return "list"
    if share(lambda v: parse_date(v) is not None) >= 0.9:
        return "date"
    if share(lambda v: parse_duration_min(v) is not None) >= 0.9:
        return "duration"
    if share(lambda v: parse_number(v) is not None) >= 0.9:
        return "number"
    if sum(len(v) for v in values) / n > 120:
        return "text"
    distinct = len(set(v.lower() for v in values))
    if distinct <= 200 and distinct <= max(5, 0.5 * n):
        return "category"
    return "text"


# ------------------------------------------------------------ config hints (optional)
def load_hints(path):
    if not path:
        return {}, None
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if ext == ".json":
            cfg = json.loads(raw.decode("utf-8-sig"))
        elif ext in (".yaml", ".yml"):
            import yaml
            cfg = yaml.safe_load(raw)
        elif ext == ".toml":
            import tomllib
            cfg = tomllib.loads(raw.decode("utf-8"))
        else:
            sys.exit("Config must be .json, .yaml/.yml or .toml")
    except ImportError as e:
        sys.exit(f"Cannot read this config format: {e}")
    except (OSError, ValueError) as e:
        sys.exit(f"Cannot read config file: {e}")
    return (cfg.get("viewer") or {}), cfg.get("name")


# ------------------------------------------------------------ data layer
class Index:
    def __init__(self, base, hints, site_name):
        self.base = os.path.abspath(base)
        self.db_path = os.path.join(self.base, "items.db")
        self.hints = hints or {}
        self.site_name = site_name or os.path.basename(self.base.rstrip("/\\")) or "Library"
        self.records, self.schema, self.types = [], {}, {}
        self.load()

    # ---- local files saved by the scraper (thumbnails/, previews/, ...)
    def _scan_local(self):
        found = {}
        for d in os.listdir(self.base):
            p = os.path.join(self.base, d)
            if d.startswith(".") or not os.path.isdir(p):
                continue
            for fn in os.listdir(p):
                if fn.endswith(".part"):
                    continue
                found.setdefault(os.path.splitext(fn)[0], []).append((d, fn))
        return found

    def load(self):
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(self.db_path)
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        rows = db.execute("SELECT url, listing, detail, scraped_at FROM items ORDER BY rowid").fetchall()
        db.close()
        local = self._scan_local()

        # pass 1: flatten listing + detail into one {field: value} dict per item, with stable names
        listing_keys = set()
        for _, listing, _, _ in rows:
            if listing:
                listing_keys.update(k for k in json.loads(listing) if k != "url")
        taken = {k.lower() for k in listing_keys}
        name_map, order, dup_of = {}, [], {}

        def field_name(kind, k, k2=None):
            key = (kind, k, k2)
            if key not in name_map:
                if kind == "listing":
                    nm = k
                elif kind == "detail":
                    nm = f"{k} (detail)" if k.lower() in taken else k
                    if nm != k:
                        dup_of[nm] = k
                else:  # a key inside a detail 'pairs' dict
                    nm = f"{k}: {k2}" if k2.lower() in taken else k2
                if kind != "listing":
                    taken.add(nm.lower())
                name_map[key] = nm
                order.append(nm)
            return name_map[key]

        def norm(v):
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return "" if v is None else str(v).strip()

        flat, raw_ids = [], []
        for url, listing, detail, scraped_at in rows:
            L = json.loads(listing) if listing else {}
            D = json.loads(detail) if detail else {}
            fields = {}
            for k, v in L.items():
                if k != "url":
                    fields[field_name("listing", k)] = norm(v)
            for k, v in D.items():
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        fields[field_name("pair", k, k2)] = norm(v2)
                else:
                    fields[field_name("detail", k)] = norm(v)
            flat.append((url, fields, scraped_at or ""))
            raw_ids.append(str(L.get("id") or safe_name(urlparse(url).path.strip("/"))))

        # a "<name> (detail)" field that only repeats the listing value adds nothing: drop it
        for nm, base in list(dup_of.items()):
            if all(f.get(nm, "") in ("", f.get(base, "")) for _, f, _ in flat):
                for _, f, _ in flat:
                    f.pop(nm, None)
                order.remove(nm)

        # pass 2: infer a type per field
        hide = set(self.hints.get("hide", []))
        forced = {k: v for k, v in (self.hints.get("types") or {}).items() if v in TYPES}
        types, distinct_counts, dur_fields = {}, {}, set()
        n_records = len(flat)
        for nm in order:
            vals, is_list = [], False
            for _, fields, _ in flat:
                v = fields.get(nm)
                if isinstance(v, list):
                    is_list = True
                    vals += v
                elif v:
                    vals.append(v)
            t = forced.get(nm) or infer_type(vals, n_records, is_list)
            if t == "duration":  # shown/filtered as a number of minutes
                dur_fields.add(nm)
                t = "number"
            types[nm] = t
            distinct_counts[nm] = len({x.lower() for x in vals})
        types[SCRAPED] = "date"

        visible = [nm for nm in order if nm not in hide]
        low = {nm.lower(): nm for nm in visible}
        title_f = self.hints.get("title")
        if title_f not in types:
            title_f = next((low[c] for c in ("title", "name", "headline", "label") if c in low), None)
            if title_f is None:
                title_f = next((nm for nm in visible if types[nm] in ("text", "category")), None)
        image_f = self.hints.get("image")
        if image_f not in types:
            image_f = next((nm for nm in order if types[nm] == "image"), None)
        preview_f = self.hints.get("preview")
        if preview_f not in types:
            preview_f = next((nm for nm in order if types[nm] == "video"), None)
        card_fields = [f for f in (self.hints.get("card_fields") or []) if f in types and f not in hide]
        if not card_fields:
            skip = {title_f, image_f, preview_f}
            card_fields = [nm for nm in visible
                           if nm not in skip and nm.lower() != "id" and distinct_counts[nm] > 1
                           and types[nm] in ("category", "list", "number", "date")][:4]

        # build records
        records = []
        for (url, fields, scraped_at), vid in zip(flat, raw_ids):
            title = first(fields.get(title_f, "")) if title_f else ""
            thumb, preview = "", ""
            for folder, fn in local.get(vid, []):
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXT and not thumb:
                    thumb = f"/files/{quote(folder)}/{quote(fn)}"
                elif ext in VIDEO_EXT and not preview:
                    preview = f"/files/{quote(folder)}/{quote(fn)}"
            local_thumb, local_preview = bool(thumb), bool(preview)
            if not thumb and image_f:
                u = first(fields.get(image_f, ""))
                thumb = u if str(u).startswith(("http://", "https://")) else ""
            if not preview and preview_f:
                u = first(fields.get(preview_f, ""))
                preview = u if str(u).startswith(("http://", "https://")) else ""

            vals, nums, dates, blob = {}, {}, {}, [url]
            for nm in order:
                v = fields.get(nm)
                items = v if isinstance(v, list) else ([v] if v else [])
                vals[nm] = [x.lower() for x in items]
                blob += items
                if types[nm] == "number":
                    conv = parse_duration_min if nm in dur_fields else parse_number
                    nums[nm] = [n for n in (conv(x) for x in items) if n is not None]
                elif types[nm] == "date":
                    dates[nm] = [d for d in (parse_date(x) for x in items) if d]
            sd = scraped_at[:10] if re.match(r"\d{4}-\d{2}-\d{2}", scraped_at) else ""
            vals[SCRAPED], dates[SCRAPED] = ([sd], [sd]) if sd else ([], [])
            blob.append(sd)
            records.append({
                "id": len(records), "url": url, "title": str(title or url), "thumb": thumb, "preview": preview,
                "local_thumb": local_thumb, "local_preview": local_preview, "scraped_at": scraped_at,
                "has_detail": bool(rows[len(records)][2]), "fields": fields,
                "_vals": vals, "_num": nums, "_date": dates, "_blob": "\n".join(blob).lower(),
            })

        labels = {nm: (self.hints.get("labels") or {}).get(nm) or pretty(nm) + (" (minutes)" if nm in dur_fields else "")
                  for nm in order}
        labels[SCRAPED] = "Scraped date"
        field_list = [{"name": nm, "label": labels[nm], "type": types[nm], "distinct": distinct_counts[nm]}
                      for nm in visible]
        field_list.append({"name": SCRAPED, "label": labels[SCRAPED], "type": "date", "distinct": 0})
        self.records, self.types = records, types
        self.schema = {
            "site": self.site_name, "count": len(records), "fields": field_list,
            "title": title_f, "image": image_f, "preview": preview_f, "card_fields": card_fields,
            "page_size": int(self.hints.get("page_size", 48)),
            "default_sort": self.hints.get("default_sort") or {},
            "facets": self._facets(visible),
        }

    # ---- every value with counts, plus the filter a click applies
    def _facets(self, visible):
        out = []
        for nm in visible + [SCRAPED]:
            t = self.types[nm]
            if t in ("category", "list"):
                c = Counter()
                for r in self.records:
                    v = r["fields"].get(nm)
                    for x in (v if isinstance(v, list) else ([v] if v else [])):
                        c[x] += 1
                entries = [{"v": k, "n": n, "f": {"field": nm, "op": "equals", "value": k}}
                           for k, n in c.most_common(20000)]
            elif t == "date":
                by_day = nm == SCRAPED
                c = Counter()
                for r in self.records:
                    for d in r["_date"].get(nm, []):
                        c[d if by_day else d[:7]] += 1
                entries = []
                for k in sorted(c, reverse=True):
                    if by_day:
                        a = b = k
                    else:
                        y, m = int(k[:4]), int(k[5:7])
                        a, b = f"{k}-01", f"{k}-{calendar.monthrange(y, m)[1]:02d}"
                    entries.append({"v": k, "n": c[k],
                                    "f": {"field": nm, "op": "between", "value": a, "value2": b}})
            else:
                continue
            if entries:
                out.append({"key": nm, "label": self.schema_label(nm), "type": t, "entries": entries})
        return out

    def schema_label(self, nm):
        return "Scraped date" if nm == SCRAPED else (self.hints.get("labels") or {}).get(nm) or pretty(nm)

    # ---- filtering
    def _compile(self, flt):
        f, op = flt.get("field"), flt.get("op")
        t = self.types.get(f)
        if not t:
            return None
        v, v2 = str(flt.get("value", "")).strip(), str(flt.get("value2", "")).strip()
        if op == "empty":
            return lambda r: not r["_vals"].get(f)
        if op == "not_empty":
            return lambda r: bool(r["_vals"].get(f))
        if t in ("number", "date"):
            conv = (lambda s: parse_number(s) if parse_number(s) is not None else None) if t == "number" \
                else (lambda s: parse_date(s))
            a = conv(v) if v else None
            b = conv(v2) if v2 else None
            if op == "gte":
                b = None
            elif op == "lte":
                a, b = None, (a if a is not None else b)
            elif op != "between":
                return None
            if a is None and b is None:
                return None
            src = "_num" if t == "number" else "_date"

            def pred(r):
                return any((a is None or x >= a) and (b is None or x <= b) for x in r[src].get(f, []))
            return pred
        if not v:
            return None
        if op == "equals":
            vl = v.lower()
            return lambda r: any(x == vl for x in r["_vals"].get(f, []))
        terms = [x.strip().lower() for x in v.split(",") if x.strip()]
        if op == "contains":
            return lambda r: all(any(tm in x for x in r["_vals"].get(f, [])) for tm in terms)
        if op == "not_contains":
            return lambda r: not any(any(tm in x for x in r["_vals"].get(f, [])) for tm in terms)
        return None

    def _sort_key(self, field):
        t = self.types.get(field)
        if t == "number":
            return lambda r: min(r["_num"][field])
        if t == "date":
            return lambda r: r["_date"][field][0]
        return lambda r: r["_vals"][field][0]

    def _has_value(self, r, field):
        src = "_num" if self.types.get(field) == "number" else ("_date" if self.types.get(field) == "date" else "_vals")
        return bool(r[src].get(field))

    def _group_label(self, r, field):
        t = self.types.get(field)
        if t == "date":
            d = (r["_date"].get(field) or [""])[0]
            return (d if field == SCRAPED else d[:7]) or "(none)"
        v = r["_vals"].get(field) or []
        raw = r["fields"].get(field)
        return str(first(raw)) if v and raw else "(none)"

    def search(self, p):
        def g(k):
            return (p.get(k, [""])[0] or "").strip()

        tokens = []
        for t in re.findall(r'-?"[^"]+"|\S+', g("q")):
            neg = t.startswith("-") and len(t) > 1
            t = (t[1:] if neg else t).strip('"').lower()
            if t:
                tokens.append((neg, t))
        preds = []
        try:
            flts = json.loads(g("filters") or "[]")
        except ValueError:
            flts = []
        for flt in (flts if isinstance(flts, list) else [])[:30]:
            if isinstance(flt, dict):
                pr = self._compile(flt)
                if pr:
                    preds.append(pr)

        def ok(r):
            for neg, t in tokens:
                if (t in r["_blob"]) == neg:
                    return False
            return all(pr(r) for pr in preds)

        res = [r for r in self.records if ok(r)]

        sort, desc = g("sort"), g("dir") == "desc"
        if sort in self.types:
            key = self._sort_key(sort)
            have, miss = [], []
            for r in res:
                (have if self._has_value(r, sort) else miss).append(r)  # items without a value always go last
            have.sort(key=key, reverse=desc)
            res = have + miss

        group = g("group")
        if group in self.types and self.types[group] in ("category", "date", "text", "number"):
            labels = [self._group_label(r, group) for r in res]
            known = sorted({l for l in labels if l != "(none)"}, reverse=(self.types[group] == "date"),
                           key=str.lower if self.types[group] != "date" else None)
            rank = {l: i for i, l in enumerate(known + (["(none)"] if "(none)" in labels else []))}
            res = [r for _, r in sorted(zip(labels, res), key=lambda t: rank[t[0]])]
        else:
            group = ""

        try:
            size = max(1, min(200, int(g("size") or self.schema.get("page_size", 48))))
            page = max(1, int(g("page") or 1))
        except ValueError:
            size, page = 48, 1
        total = len(res)
        pages = max(1, -(-total // size))
        page = min(page, pages)
        items = []
        for r in res[(page - 1) * size: page * size]:
            it = {k: v for k, v in r.items() if not k.startswith("_")}
            it["group"] = self._group_label(r, group) if group else ""
            items.append(it)
        return {"total": total, "all": len(self.records), "page": page, "pages": pages, "items": items}


INDEX = None
AUTH = None
VIEWS_LOCK = threading.Lock()


# ------------------------------------------------------------ saved views
def views_path():
    return os.path.join(INDEX.base, "views.json")


def read_views():
    try:
        with open(views_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_views(views):
    tmp = views_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(views, f, ensure_ascii=False, indent=2)
    os.replace(tmp, views_path())


def clean_view(params):
    out = {}
    for k in ALLOWED_VIEW_KEYS:
        v = params.get(k)
        if k == "filters":
            if isinstance(v, list):
                out[k] = [{kk: str(vv)[:200] for kk, vv in f.items() if kk in ("field", "op", "value", "value2")}
                          for f in v[:30] if isinstance(f, dict)]
        elif v is not None and str(v).strip():
            out[k] = str(v)[:500]
    return out


# ------------------------------------------------------------ HTTP layer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_bytes(self, data, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def send_file(self, path):
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m:
            g1, g2 = m.groups()
            if g1 == "" and g2 != "":
                start = max(size - int(g2), 0)
            else:
                start = int(g1) if g1 else 0
                end = int(g2) if g2 else size - 1
            end = min(end, size - 1)
            status = 206
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        if not AUTH:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                given = base64.b64decode(h[6:]).decode("utf-8", "replace")
            except Exception:
                given = ""
            if ":" in given and hmac.compare_digest(given.split(":", 1)[1].encode(), AUTH.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Library"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def read_json_body(self):
        if "application/json" not in self.headers.get("Content-Type", ""):  # blocks cross-site form posts
            return None
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > 100_000:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    def do_GET(self):
        if not self.authorized():
            return
        u = urlparse(self.path)
        path = u.path
        if path == "/":
            self.send_bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/schema":
            self.send_json(INDEX.schema)
        elif path == "/api/search":
            self.send_json(INDEX.search(parse_qs(u.query)))
        elif path == "/api/views":
            with VIEWS_LOCK:
                self.send_json(read_views())
        elif path == "/api/reload":
            INDEX.load()
            self.send_json({"ok": True, "all": len(INDEX.records)})
        elif path.startswith("/files/"):
            base = os.path.realpath(INDEX.base)
            full = os.path.realpath(os.path.join(base, unquote(path[7:])))
            # only files inside sub-folders (thumbnails/, previews/ ...), never the database itself
            if (full.startswith(base + os.sep) and os.path.dirname(full) != base
                    and not full.endswith(".part") and os.path.isfile(full)):
                self.send_file(full)
            else:
                self.send_bytes(b"Not found", "text/plain", 404)
        else:
            self.send_bytes(b"Not found", "text/plain", 404)

    def do_POST(self):
        if not self.authorized():
            return
        path = urlparse(self.path).path
        body = self.read_json_body()
        if not isinstance(body, dict):
            return self.send_json({"error": "bad request"}, 400)
        name = str(body.get("name", "")).strip()[:80]
        if not name:
            return self.send_json({"error": "name required"}, 400)
        if path == "/api/views":
            if not isinstance(body.get("params"), dict):
                return self.send_json({"error": "params required"}, 400)
            with VIEWS_LOCK:
                views = read_views()
                views[name] = clean_view(body["params"])
                write_views(views)
            self.send_json(views)
        elif path == "/api/views/delete":
            with VIEWS_LOCK:
                views = read_views()
                views.pop(name, None)
                write_views(views)
            self.send_json(views)
        else:
            self.send_json({"error": "not found"}, 404)


# ------------------------------------------------------------ front-end
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Library</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--panel2:#1f232c;--line:#2b303b;--text:#e8eaf0;--mute:#9099a8;--acc:#5b8cff;--acc2:#7c5cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:20;background:rgba(15,17,21,.95);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:12px 20px}
.top{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0 8px 0 0;white-space:nowrap}
#q{flex:1;min-width:240px;padding:11px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:15px}
#q:focus,input:focus,select:focus{outline:2px solid var(--acc);border-color:transparent}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;font:inherit}
button:hover{border-color:var(--acc)}
button.primary{background:linear-gradient(135deg,var(--acc),var(--acc2));border:0;font-weight:600}
#panel{display:none;margin-top:12px}
#panel.open{display:block}
.frow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.frow select,.frow input{min-width:120px}
.frow .rm{padding:6px 10px;color:var(--mute)}
.prow{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mute)}
input,select{padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);font:inherit}
input[type=date]{color-scheme:dark}
#views{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.hint{color:var(--mute);font-size:12px}
.vchip{display:inline-flex;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:999px;overflow:hidden}
.vname{padding:3px 6px 3px 12px;cursor:pointer}
.vname:hover{color:var(--acc)}
.vdel{padding:3px 10px 3px 6px;cursor:pointer;color:var(--mute)}
.vdel:hover{color:#ff7b7b}
#info{padding:12px 20px 0;color:var(--mute)}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;padding:14px 20px}
.ghead{grid-column:1/-1;font-size:16px;font-weight:600;border-bottom:1px solid var(--line);padding:8px 2px 6px;margin-top:10px}
.ghead:first-child{margin-top:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;transition:transform .12s,border-color .12s}
.card:hover{transform:translateY(-2px);border-color:var(--acc)}
.thumb{position:relative;aspect-ratio:4/3;background:#0a0b0e;cursor:pointer;overflow:hidden}
.thumb img,.thumb video{width:100%;height:100%;object-fit:cover;position:absolute;inset:0}
.noimg{position:absolute;inset:0;display:grid;place-items:center;color:var(--mute)}
.badge{position:absolute;left:6px;bottom:6px;background:rgba(0,0,0,.75);padding:1px 6px;border-radius:5px;font-size:12px;z-index:2;color:#8affb0}
.body{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.title{font-weight:600;cursor:pointer;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.title:hover{color:var(--acc)}
.kv{color:var(--mute);font-size:12px}
.kv b{font-weight:500;margin-right:4px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:1px 9px;font-size:12px;cursor:pointer}
.chip:hover{border-color:var(--acc);color:var(--acc)}
.chip.big{font-size:13px;padding:3px 10px}
.chip b{color:var(--mute);font-weight:500;margin-left:4px}
#pager{display:flex;gap:10px;justify-content:center;align-items:center;padding:6px 20px 30px}
#pager input{width:70px;text-align:center}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;z-index:50;overflow:auto;padding:30px 16px}
.overlay.open{display:block}
.mbox{max-width:900px;margin:0 auto;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;position:relative}
.mbox.wide{max-width:1050px}
.mbox h2{margin:0 40px 12px 0;font-size:20px}
.close{position:absolute;right:14px;top:14px}
.media{width:100%;max-height:480px;background:#000;border-radius:10px;display:block;margin-bottom:14px;object-fit:contain}
table{width:100%;border-collapse:collapse}
td{padding:6px 8px;border-top:1px solid var(--line);vertical-align:top}
td:first-child{color:var(--mute);width:160px}
a{color:var(--acc)}
.empty{grid-column:1/-1;text-align:center;color:var(--mute);padding:60px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 12px}
.tab small{color:var(--mute);margin-left:4px}
.tab.on{border-color:var(--acc);color:var(--acc)}
.brow{display:flex;gap:10px;margin-bottom:8px}
.brow input{flex:1}
#binfo{color:var(--mute);font-size:12px;margin-bottom:8px}
#blist{display:flex;flex-wrap:wrap;gap:6px;max-height:55vh;overflow:auto}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1 id="site">Library</h1>
    <input id="q" type="search" placeholder="Search anything (use &quot;phrase&quot; and -exclude)" autofocus>
    <button class="primary" id="go">Search</button>
    <button id="toggle">Filters</button>
    <button id="browse">Browse data</button>
    <button id="clear">Clear</button>
    <button id="reload" title="Re-read the database after a new scrape">Reload DB</button>
  </div>
  <div id="views"></div>
  <div id="panel">
    <div id="frows"></div>
    <div class="prow">
      <button id="addf">+ Add filter</button>
      <label>Sort by<select id="sort"></select></label>
      <label>Direction<select id="dir"><option value="asc">Ascending</option><option value="desc">Descending</option></select></label>
      <label>Group by<select id="group"></select></label>
      <label>Per page<select id="size"><option>24</option><option>48</option><option>96</option><option>200</option></select></label>
    </div>
  </div>
</header>
<datalist id="dl-store"></datalist>

<div id="info"></div>
<div id="grid"></div>
<div id="pager"></div>
<div id="modal" class="overlay"><div class="mbox" id="mbox"></div></div>
<div id="bmodal" class="overlay">
  <div class="mbox wide">
    <button class="close" id="bclose">✕</button>
    <h2>Browse data</h2>
    <div class="hint">Every value found in the database with how many items have it. Click a value to filter by it.</div>
    <div id="btabs" class="tabs"></div>
    <div class="brow"><input id="bfilter" placeholder="Filter this list..."><button id="bcsv">Download CSV</button></div>
    <div id="binfo"></div>
    <div id="blist"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const NEEDS_NO_VALUE = ["empty", "not_empty"];
const OPS = {
  text:   [["contains","contains"],["not_contains","does not contain"],["equals","equals"],["empty","is empty"],["not_empty","has a value"]],
  number: [["gte","at least"],["lte","at most"],["between","between"],["empty","is empty"],["not_empty","has a value"]],
  date:   [["gte","on or after"],["lte","on or before"],["between","between"],["empty","is empty"],["not_empty","has a value"]],
};
const kindOf = t => t === "number" ? "number" : t === "date" ? "date" : "text";

let S = null, filters = [], page = 1, items = [], bi = 0, views = {}, timer;
const typeOf = f => (S.fields.find(x => x.name === f) || {}).type || "text";
const labelOf = f => (S.fields.find(x => x.name === f) || {}).label || f;

// ---------- parameters / saved views
const complete = f => f.field && f.op && (NEEDS_NO_VALUE.includes(f.op) || (f.op === "between" ? (f.value || f.value2) : f.value));
function currentParams() {
  const o = { q: $("#q").value.trim(), filters: filters.filter(complete), sort: $("#sort").value,
              dir: $("#dir").value, group: $("#group").value, size: $("#size").value };
  return o;
}
function setParams(o) {
  $("#q").value = o.q || "";
  filters = (o.filters || []).map(f => ({ ...f }));
  const d = S.default_sort || {};
  $("#sort").value = o.sort !== undefined ? o.sort : (d.field || "");
  $("#dir").value = o.dir || d.dir || "asc";
  $("#group").value = o.group || "";
  $("#size").value = o.size || String(S.page_size);
  if (!$("#size").value) $("#size").value = "48";
  renderFilters();
  if (filters.length) $("#panel").classList.add("open");
}

let searchSeq = 0;
async function search(reset = true) {
  if (reset) page = 1;
  const p = currentParams();
  const seq = ++searchSeq;
  const qs = new URLSearchParams({ q: p.q, filters: JSON.stringify(p.filters), sort: p.sort, dir: p.dir,
                                    group: p.group, size: p.size, page });
  const d = await (await fetch("/api/search?" + qs)).json();
  if (seq !== searchSeq) return;  // a newer search was started meanwhile: drop this stale answer
  items = d.items; page = d.page;
  render(d);
}
const later = () => { clearTimeout(timer); timer = setTimeout(search, 300); };

// ---------- filter rows
function fieldOptions(sel) {
  return S.fields.map(f => `<option value="${esc(f.name)}" ${f.name === sel ? "selected" : ""}>${esc(f.label)}</option>`).join("");
}
function rowHtml(f, i) {
  const t = typeOf(f.field), kind = kindOf(t);
  const ops = OPS[kind].map(([v, l]) => `<option value="${v}" ${v === f.op ? "selected" : ""}>${l}</option>`).join("");
  const itype = kind === "number" ? "number" : kind === "date" ? "date" : "text";
  const list = (t === "category" || t === "list") ? ` list="dl-store"` : "";
  const show = !NEEDS_NO_VALUE.includes(f.op);
  const second = f.op === "between" ? `<input data-k="value2" data-i="${i}" type="${itype}" ${itype === "number" ? 'step="any"' : ""} value="${esc(f.value2 || "")}" placeholder="to">` : "";
  return `<div class="frow">
    <select data-k="field" data-i="${i}">${fieldOptions(f.field)}</select>
    <select data-k="op" data-i="${i}">${ops}</select>
    ${show ? `<input data-k="value" data-i="${i}" data-f="${esc(f.field)}" type="${itype}" ${itype === "number" ? 'step="any"' : ""}${list} value="${esc(f.value || "")}" placeholder="${f.op === "between" ? "from" : "value"}">` : ""}
    ${show ? second : ""}
    <button class="rm" data-rm="${i}" title="Remove filter">✕</button></div>`;
}
function renderFilters() {
  $("#frows").innerHTML = filters.map(rowHtml).join("");
}
function newFilter(field) {
  const t = typeOf(field);
  return { field, op: OPS[kindOf(t)][0][0], value: "", value2: "" };
}
function addFilter(f) {
  const key = JSON.stringify([f.field, f.op, f.value, f.value2 || ""]);
  if (!filters.some(x => JSON.stringify([x.field, x.op, x.value, x.value2 || ""]) === key)) filters.push({ value2: "", ...f });
  $("#panel").classList.add("open");
  renderFilters();
}
function fillDatalist(field) {
  const fx = (S.facets || []).find(x => x.key === field);
  $("#dl-store").innerHTML = fx ? fx.entries.slice(0, 3000).map(e => `<option value="${esc(e.v)}">`).join("") : "";
}

// ---------- cards / detail
function valueHtml(field, v, asChips) {
  const t = typeOf(field);
  if (Array.isArray(v)) return `<span class="chips">${v.slice(0, asChips ? 4 : 999).map(x => `<span class="chip" data-chip="${esc(field)}" data-val="${esc(x)}">${esc(x)}</span>`).join("")}${asChips && v.length > 4 ? `<span class="kv">+${v.length - 4}</span>` : ""}</span>`;
  if (t === "category") return `<span class="chip" data-chip="${esc(field)}" data-val="${esc(v)}">${esc(v)}</span>`;
  if (/^https?:\/\//i.test(v) && !/\s/.test(v)) return `<a href="${esc(v)}" target="_blank" rel="noopener noreferrer">${esc(v.length > 70 ? v.slice(0, 67) + "…" : v)}</a>`;
  return esc(v);
}
function card(v, i) {
  const img = v.thumb ? `<img loading="lazy" src="${esc(v.thumb)}" alt="">` : `<div class="noimg">No image</div>`;
  const rows = (S.card_fields || []).map(f => {
    const val = v.fields[f];
    if (val === undefined || val === "" || (Array.isArray(val) && !val.length)) return "";
    return (Array.isArray(val) || typeOf(f) === "category")
      ? `<div class="chips">${valueHtml(f, val, true)}</div>`
      : `<div class="kv"><b>${esc(labelOf(f))}</b>${esc(val)}</div>`;
  }).join("");
  return `<div class="card" data-i="${i}">
    <div class="thumb" data-i="${i}" data-prev="${esc(v.preview)}">${img}
      ${v.local_preview ? '<span class="badge">preview</span>' : ""}</div>
    <div class="body">
      <div class="title" data-i="${i}">${esc(v.title || "(untitled)")}</div>${rows}
      ${v.has_detail ? "" : '<div class="kv" title="Detail page not scraped yet">⚠ no details</div>'}
    </div></div>`;
}
function render(d) {
  $("#info").textContent = `${d.total.toLocaleString()} match${d.total === 1 ? "" : "es"} of ${d.all.toLocaleString()} items`;
  let html = "", last = null;
  items.forEach((v, i) => {
    if (v.group && v.group !== last) { html += `<div class="ghead">${esc(v.group)}</div>`; last = v.group; }
    html += card(v, i);
  });
  $("#grid").innerHTML = html || `<div class="empty">No items match.</div>`;
  document.querySelectorAll(".thumb").forEach(t => {
    t.addEventListener("mouseenter", () => {
      const src = t.dataset.prev;
      if (!src || t.querySelector("video")) return;
      const v = document.createElement("video");
      v.src = src; v.muted = true; v.loop = true; v.autoplay = true; v.playsInline = true;
      v.addEventListener("error", () => v.remove());
      t.appendChild(v);
    });
    t.addEventListener("mouseleave", () => { const v = t.querySelector("video"); if (v) v.remove(); });
  });
  $("#pager").innerHTML = d.pages > 1 ? `
    <button id="prev" ${d.page <= 1 ? "disabled" : ""}>← Prev</button>
    <span>Page <input id="pg" type="number" min="1" max="${d.pages}" value="${d.page}"> of ${d.pages}</span>
    <button id="next" ${d.page >= d.pages ? "disabled" : ""}>Next →</button>` : "";
  window.scrollTo({ top: 0 });
}
function openModal(v) {
  let media = "";
  if (v.preview) media = `<video class="media" src="${esc(v.preview)}" poster="${esc(v.thumb)}" controls muted loop></video>`;
  else if (v.thumb) media = `<img class="media" src="${esc(v.thumb)}" alt="">`;
  const rows = S.fields.filter(f => f.name !== "_scraped").map(f => {
    const val = v.fields[f.name];
    if (val === undefined || val === "" || (Array.isArray(val) && !val.length)) return "";
    return `<tr><td>${esc(f.label)}</td><td>${valueHtml(f.name, val, false)}</td></tr>`;
  }).join("");
  $("#mbox").innerHTML = `<button class="close" id="mclose">✕</button>
    <h2>${esc(v.title)}</h2>${media}
    <table>${rows}
      <tr><td>Scraped</td><td>${esc(v.scraped_at || "details not scraped yet")}</td></tr>
      <tr><td>Source page</td><td><a href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">${esc(v.url)}</a></td></tr></table>`;
  $("#modal").classList.add("open");
}
function closeModal() { const m = $("#mbox video"); if (m) m.pause(); $("#modal").classList.remove("open"); }

// ---------- browse data
function renderBrowseTabs() {
  $("#btabs").innerHTML = (S.facets || []).map((f, i) =>
    `<button class="tab ${i === bi ? "on" : ""}" data-tab="${i}">${esc(f.label)}<small>${f.entries.length.toLocaleString()}</small></button>`).join("");
}
function renderBrowseList() {
  const f = (S.facets || [])[bi];
  if (!f) { $("#blist").innerHTML = ""; $("#binfo").textContent = "Nothing to browse yet."; return; }
  const t = $("#bfilter").value.trim().toLowerCase();
  const list = [];
  f.entries.forEach((e, i) => { if (!t || String(e.v).toLowerCase().includes(t)) list.push([e, i]); });
  const shown = list.slice(0, 600);
  $("#binfo").textContent = `${list.length.toLocaleString()} of ${f.entries.length.toLocaleString()} values` +
    (list.length > shown.length ? ` — showing the first ${shown.length}, use the filter box to narrow` : "");
  $("#blist").innerHTML = shown.map(([e, i]) => `<span class="chip big" data-entry="${i}">${esc(e.v)}<b>${e.n.toLocaleString()}</b></span>`).join("");
}
function openBrowse() { renderBrowseTabs(); renderBrowseList(); $("#bmodal").classList.add("open"); }
function closeBrowse() { $("#bmodal").classList.remove("open"); }
function downloadCsv() {
  const f = (S.facets || [])[bi]; if (!f) return;
  const rows = [["value", "count"], ...f.entries.map(e => [e.v, e.n])];
  const csv = "\ufeff" + rows.map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = f.key.replace(/[^\w.-]+/g, "_") + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------- saved views
function renderViews() {
  const names = Object.keys(views).sort((a, b) => a.localeCompare(b));
  $("#views").innerHTML =
    (names.length ? '<span class="hint">Views:</span>' : '<span class="hint">No saved views yet — set filters, then click “+ Save view”.</span>') +
    names.map(n => `<span class="vchip"><span class="vname" data-view="${esc(n)}">${esc(n)}</span><span class="vdel" data-delview="${esc(n)}" title="Delete view">×</span></span>`).join("") +
    `<button id="saveview">+ Save view</button>`;
}
async function loadViews() {
  try { views = await (await fetch("/api/views")).json(); } catch (e) { views = {}; }
  renderViews();
}
async function postJson(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

// ---------- events
document.addEventListener("click", async e => {
  const be = e.target.closest("[data-entry]");
  if (be) { addFilter(S.facets[bi].entries[+be.dataset.entry].f); closeBrowse(); search(); return; }
  const tab = e.target.closest("[data-tab]");
  if (tab) { bi = +tab.dataset.tab; $("#bfilter").value = ""; renderBrowseTabs(); renderBrowseList(); return; }
  const rm = e.target.closest("[data-rm]");
  if (rm) { filters.splice(+rm.dataset.rm, 1); renderFilters(); search(); return; }
  const vw = e.target.closest("[data-view]");
  if (vw) { setParams(views[vw.dataset.view] || {}); search(); return; }
  const dv = e.target.closest("[data-delview]");
  if (dv) {
    if (confirm(`Delete view "${dv.dataset.delview}"?`)) { views = await postJson("/api/views/delete", { name: dv.dataset.delview }); renderViews(); }
    return;
  }
  if (e.target.id === "saveview") {
    const name = (prompt("Name for this view (saving an existing name replaces it):") || "").trim();
    if (name) { views = await postJson("/api/views", { name, params: currentParams() }); renderViews(); }
    return;
  }
  const ch = e.target.closest("[data-chip]");
  if (ch) { addFilter({ field: ch.dataset.chip, op: "equals", value: ch.dataset.val }); closeModal(); search(); return; }
  const t = e.target.closest(".thumb, .title");
  if (t && t.dataset.i !== undefined) { openModal(items[+t.dataset.i]); return; }
  if (e.target.id === "mclose" || e.target.id === "modal") closeModal();
  if (e.target.id === "bclose" || e.target.id === "bmodal") closeBrowse();
  if (e.target.id === "prev") { page--; search(false); }
  if (e.target.id === "next") { page++; search(false); }
});
document.addEventListener("input", e => {
  const el = e.target;
  if (el.dataset && el.dataset.k && el.dataset.i !== undefined && ["value", "value2"].includes(el.dataset.k)) {
    filters[+el.dataset.i][el.dataset.k] = el.value; later();
  }
});
document.addEventListener("focusin", e => { if (e.target.dataset && e.target.dataset.f) fillDatalist(e.target.dataset.f); });
document.addEventListener("change", e => {
  const el = e.target;
  if (el.id === "pg") { page = Math.max(1, +el.value || 1); search(false); return; }
  if (el.dataset && el.dataset.k === "field") { filters[+el.dataset.i] = newFilter(el.value); renderFilters(); return; }
  if (el.dataset && el.dataset.k === "op") { filters[+el.dataset.i].op = el.value; renderFilters(); later(); return; }
  if (["sort", "dir", "group", "size"].includes(el.id)) search();
  if (el.dataset && ["value", "value2"].includes(el.dataset.k)) { filters[+el.dataset.i][el.dataset.k] = el.value; later(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeModal(); closeBrowse(); } });
$("#q").addEventListener("input", later);
$("#bfilter").addEventListener("input", renderBrowseList);
$("#bcsv").onclick = downloadCsv;
$("#browse").onclick = openBrowse;
$("#go").onclick = () => search();
$("#toggle").onclick = () => $("#panel").classList.toggle("open");
$("#addf").onclick = () => {
  const first = S.fields.find(f => f.type === "category" || f.type === "list") || S.fields[0];
  if (first) { filters.push(newFilter(first.name)); renderFilters(); }
};
$("#clear").onclick = () => { setParams({}); search(); };
$("#reload").onclick = async () => { await fetch("/api/reload"); await loadSchema(false); search(false); };

async function loadSchema(initial = true) {
  S = await (await fetch("/api/schema")).json();
  $("#site").textContent = S.site; document.title = S.site;
  const sorts = [["", "Default order"], ["_scraped", "Scraped date"], ...S.fields.filter(f => f.name !== "_scraped").map(f => [f.name, f.label])];
  $("#sort").innerHTML = sorts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const groups = [["", "No grouping"], ...S.fields.filter(f => ["category", "date", "number"].includes(f.type)).map(f => [f.name, f.label])];
  $("#group").innerHTML = groups.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if (initial) setParams({}); else renderFilters();
  renderBrowseTabs();
}
(async () => { await loadSchema(); await loadViews(); search(); })();
</script>
</body>
</html>
"""


# ------------------------------------------------------------ main
def main():
    global INDEX, AUTH
    ap = argparse.ArgumentParser(description="Generic viewer for data scraped with generic_scraper.py")
    ap.add_argument("--dir", help="Site folder containing items.db (e.g. output/books_toscrape)")
    ap.add_argument("--config", help="Site rules file: used to find the folder and for the optional 'viewer' hints")
    ap.add_argument("--out", default="output", help="Output root used with --config when --dir is not given")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="Address to listen on. Use 0.0.0.0 to allow other devices on your network")
    ap.add_argument("--password", default=None, help="Optional password (the browser will ask for it)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    AUTH = args.password

    hints, name = load_hints(args.config)
    folder = args.dir or (os.path.join(args.out, name) if name else None)
    if not folder:
        sys.exit("Give --dir <site folder> or --config <site rules file>.")
    try:
        INDEX = Index(folder, hints, name)
    except FileNotFoundError as e:
        sys.exit(f"Database not found: {e}\nRun generic_scraper.py first, or check --dir.")
    except sqlite3.DatabaseError as e:
        sys.exit(f"Could not read the database ({e}). Was it created by generic_scraper.py?")

    url = f"http://127.0.0.1:{args.port}"
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Loaded {len(INDEX.records)} items, {len(INDEX.schema['fields']) - 1} fields from {INDEX.db_path}")
    print(f"Viewer running at {url}  (Ctrl+C to stop)")
    if args.host not in ("127.0.0.1", "localhost"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))  # no packets are actually sent
            lan_ip = s.getsockname()[0]
            s.close()
        except OSError:
            lan_ip = "<this-machine-ip>"
        print(f"Other devices on your network can open: http://{lan_ip}:{args.port}")
        if not AUTH:
            print("WARNING: no password set - anyone on your network can browse the data. Use --password.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
