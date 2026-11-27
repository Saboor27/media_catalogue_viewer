# Rules-file reference

A *rules file* describes one website. It can be written as **JSON** (`.json`, always available), **YAML** (`.yaml` / `.yml`, needs `pip install pyyaml`) or **TOML** (`.toml`, needs Python 3.11+). The examples here use JSON.

- Any key that starts with an underscore (`"_comment"`, `"_how_to_use"`…) is documentation and is ignored.
- The file is validated before anything is requested. Missing required keys, invalid CSS selectors and invalid regular expressions are reported with the name of the setting.
- Use `--test` (see the [authoring workflow](#authoring-workflow)) to check a rules file without writing any data.
- A complete, working example is `sites/books_toscrape.json`; `sites/template.json` is an annotated starting point.

## Top-level settings

| Key | Required | Default | Meaning |
|---|---|---|---|
| `base_url` | yes | – | Site root, e.g. `https://example.com`. Available as `{base}` in URL templates. |
| `name` | no | host name | Used for the output folder `output/<name>/`. |
| `user_agent` | no | `generic-scraper/1.0 (portfolio project)` | Sent with every request. A descriptive value with a contact is good practice. |
| `respect_robots` | no | `true` | Honour `robots.txt` (disallowed URLs are skipped, `Crawl-delay` is used). Only turn off for sites you own or are authorised to crawl. |
| `workers` | no | `4` | Parallel connections (overridden by `--workers`). |
| `delay` | no | `0.5` | Seconds each worker pauses after a request (overridden by `--delay`). |
| `headers` | no | – | Extra HTTP headers, e.g. `{"Accept-Language": "de"}`. |
| `age_gate` | no | – | `{"click_selector": "<css>"}`: a button a real browser clicks once before scraping. The resulting cookies are reused for fast HTTP fetching. Needs Selenium. |
| `use_browser` | no | `false` | Load *every* page in the browser (JavaScript-rendered sites). Needs Selenium. Same as `--use-browser`. |

## `pagination`

### Mode `pattern`

Pages follow a URL template.

| Key | Meaning |
|---|---|
| `mode` | `"pattern"` |
| `url_pattern` | **Required.** Must contain `{page}`; may contain `{base}`. Example: `{base}/catalogue/page-{page}.html` |
| `first_url` | Optional separate URL for page 1 (many sites use `/` or `/?per_page=75` for it). `{base}` / `{page}` allowed. |
| `first_page_number` | Number substituted for `{page}` on the first page. Default `1` (use `0` for zero-based sites). |
| `total_pages` | Fixed page count. |
| `total_pages_from` | A [field rule](#field-rules) applied to the first page; digits in the result are the page count. |
| `total_items_from` + `items_per_page` | A field rule giving the total number of items (digits are extracted), and how many appear per page. Pages = ⌈total ÷ per page⌉. |

If none of the three page-count options is present, pages are fetched until one contains no items.

### Mode `next_link`

| Key | Meaning |
|---|---|
| `mode` | `"next_link"` |
| `first_url` | **Required.** The first listing page. |
| `next_selector` | **Required.** CSS selector of the "next page" link; its `href` is followed until there is none. |

## `listing`

Describes one listing page.

| Key | Meaning |
|---|---|
| `item_selector` | **Required.** CSS selector matching each item "card". |
| `link` | **Required.** A field rule locating the link to the item's detail page *inside* the card. `attr` defaults to `href` and `absolute` to `true`. Omit `selector` if the card itself is the link. The resulting URL is the item's unique key. |
| `fields` | `{ "name": <field rule>, … }`. Each becomes a column. A field called `id` is used to name downloaded files. |
| `ignore` | A list of [ignore rules](#ignore-rules) for cards that are not real items (ads). |
| `wait_selector` | Browser mode only: element to wait for. Defaults to `item_selector`. |

### Ignore rules

```json
"ignore": [
  { "field": "title", "equals": "Sponsored" },
  { "field": "title", "contains": "advert" },
  { "field": "url",   "contains": "/ads/" },
  { "external": true }
]
```

`equals` / `contains` compare a field (default `title`; `url` is always available) case-insensitively. `{"external": true}` skips cards whose link points to another website. If a rule is added later, matching entries that were saved by earlier runs and never got their details are removed from the database.

## Field rules

A field rule is either a CSS selector string, or an object:

| Key | Meaning |
|---|---|
| `selector` | CSS selector, searched inside the card / detail root. Omit to use the element itself. |
| `attr` | `"text"` (default) or an attribute name (`href`, `src`, `title`, `class`, `data-id`…). `class` is joined with spaces. |
| `regex` | Keep the first capture group (or the whole match). No match gives an empty value. |
| `replace` | List of `[old, new]` replacements, e.g. `[["Price: ", ""]]`. |
| `strip_chars` | Characters to trim from both ends, e.g. `",:"`. |
| `absolute` | `true` turns a relative URL into an absolute one (relative to the page it was found on). |
| `skip` | Ignore the first *N* matches. |
| `multiple` | `true` returns a list of all matches instead of the first. |
| `default` | Value when nothing matches. |
| `scope` | Detail rules only: `"page"` searches the whole page instead of the `root_selector` element. |

Order of operations: select → read text/attribute → `regex` → `replace` → `strip_chars` → `absolute`.

## `detail`

Describes one detail page. Leave the section out (or set `"enabled": false`, or pass `--no-detail`) for listing-only scraping.

| Key | Meaning |
|---|---|
| `root_selector` | The element holding the content. If it is missing the page counts as failed (and is retried in the browser if one is configured). |
| `wait_selector` | Browser mode only: element to wait for. Defaults to `root_selector`. |
| `fields` | `{ "name": <field rule>, … }` for single values. |
| `pairs` | `{ "name": <pair rule>, … }` for label / value rows. |

### Pair rules

Turn rows such as `<tr><th>UPC</th><td>abc</td></tr>` or `Studio: X` lines into a `{label: value}` object.

| Key | Meaning |
|---|---|
| `row_selector` | Selector matching each row. |
| `key_selector` | Selector of the label inside a row (a trailing `:` is removed). |
| `value_selector` | Selector of the value(s) inside a row. |
| `multiple` | `true` keeps all values as a list (e.g. several actors); otherwise they are joined with `", "`. |
| `strip_chars` | Characters trimmed from values. Default `","`. |
| `scope` | `"page"` to search the whole page. |

## `downloads`

```json
"downloads": {
  "thumbnails": { "field": "thumbnail", "folder": "thumbnails", "default_ext": ".jpg" },
  "previews":   { "field": "preview",   "folder": "previews",   "default_ext": ".mp4" }
}
```

`field` names a listing field containing a URL. Enable entries with `--download thumbnails` (repeatable) or `--download all`. Files are saved to `output/<name>/<folder>/<id>.<ext>`, where `<id>` is the listing's `id` field or, if there is none, a file-system-safe version of the item's URL path. Existing files are skipped. Site cookies are only sent to the site's own host.

## `viewer` (optional hints)

Read by `generic_viewer.py` when you pass `--config`. Everything has a sensible automatic default.

| Key | Meaning |
|---|---|
| `title` | Field used as the card title. |
| `image` | Field containing the image URL. |
| `preview` | Field containing a short video URL (plays on hover). |
| `card_fields` | Fields shown on each card, in order. |
| `hide` | Fields not shown (they remain searchable). |
| `types` | Force a field's type: `text`, `category`, `list`, `number`, `duration`, `date`, `image`, `video`, `url`. |
| `labels` | `{ "field": "Nicer name" }`. |
| `default_sort` | `{ "field": "price", "dir": "asc" }`. |
| `page_size` | Items per page (default 48). |

Field names are the ones that appear in the viewer: listing fields as written, detail fields by name, pair rows by their label. A detail field that only repeats a listing field of the same name is dropped; a genuine name clash becomes `name (detail)` or `pair: label`.

Type detection: values are classified as image / video / URL by their form, as *list* when the scraper stored a list, as *date* or *duration* when at least 90 % of values parse as one, as *number* when at least 90 % parse as one (`£12.50`, `6.4K`, `84%`, `120 Min`), as *category* when there are few distinct values, and otherwise as text. Durations are filtered and sorted in minutes.

## Authoring workflow

1. **Find the pagination.** Open page 2 and 3 of the site and compare URLs. Note any text such as "Page 1 of 50" or "1,234 results" that gives the size.
2. **Find the card.** In the browser's developer tools, inspect one listing item and pick the outermost element that repeats. Check it matches every item: `document.querySelectorAll("<your selector>").length`.
3. **Pick fields.** Look for stable class names or `data-` attributes, not positions. Use `attr` for values in attributes (`title`, `src`, `href`).
4. **Run `--test`:**
   ```bash
   python generic_scraper.py --config sites/yours.json --test
   ```
   It prints how many items were found, the total page count, the first two items and the first item's detail data. An empty value means a selector matched nothing.
5. **Handle noise.** Cards that are really adverts show up as items with odd titles or external links; add `ignore` rules.
6. **Run a small job first:** `--max-pages 1 --limit 10`, look at it in the viewer, then run the full scrape.

Tips: a selector that is valid CSS but too specific is the most common problem; `multiple: true` is needed whenever a field can have several values; when a value contains extra text, use `regex` with a capture group; to make a rules file shareable, keep site-specific files for private sites in `sites/private/`, which is git-ignored.

## Command-line reference

### `generic_scraper.py`

| Option | Meaning |
|---|---|
| `--config FILE` | Rules file (required). |
| `--out DIR` | Output root (default `output`); a sub-folder named after `name` is created. |
| `--test` | Dry run: fetch the first listing page and the first detail page, print the extracted data, write nothing. |
| `--workers N` | Parallel connections (default: rules file or 4). |
| `--delay S` | Seconds each worker pauses after a request (default: rules file or 0.5; raised to the site's `Crawl-delay` if larger). |
| `--start-page N` | First listing page to process. In `pattern` mode earlier pages are skipped entirely. |
| `--max-pages N` | Process at most *N* listing pages. |
| `--limit N` | Stop after *N* items in total. |
| `--no-detail` | Listing data only. |
| `--download NAME` | Enable a `downloads` entry (repeatable, or `all`). |
| `--use-browser` | Load every page in the browser. |
| `--browser chrome\|edge\|firefox`, `--headed` | Browser choice and visibility. |

Behaviour worth knowing: pages are fetched over HTTP by `workers` threads; `429` / `502` / `503` / `504` replies pause all workers (using `Retry-After` when present) and are retried; a page that cannot be fetched over HTTP is retried in the browser if one is configured; if HTTP fetching of detail pages fails for a whole page, the scraper switches to the browser for the rest of the run. `Ctrl+C` saves progress and still writes the exports.

### `generic_viewer.py`

| Option | Meaning |
|---|---|
| `--dir DIR` | Site folder containing `items.db`. |
| `--config FILE` | Rules file: finds `<out>/<name>` and supplies the `viewer` hints. |
| `--out DIR` | Output root used with `--config` (default `output`). |
| `--port N` | Port (default 8765). |
| `--host ADDR` | `127.0.0.1` (default) or `0.0.0.0` to share on your network. |
| `--password TEXT` | Require a password (HTTP Basic authentication; any user name). |
| `--no-browser` | Do not open a browser window. |

## Output files

```
output/<name>/
├── items.db          SQLite, table `items`
├── items.json        [{ "url", "listing": {...}, "detail": {...} }, ...]
├── items.csv         one row per item
├── <download folders>/
└── views.json        saved views (written by the viewer)
```

Table `items`: `url` (primary key), `listing` (JSON), `detail` (JSON or NULL), `detail_done` (0/1), `scraped_at` (ISO timestamp of the detail scrape).

CSV columns: `url`, each listing field by name, each detail field as `detail.<field>`, each pair row as `detail.<pair>.<label>`. Lists are joined with `; `.
