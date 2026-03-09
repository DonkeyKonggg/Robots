# Robots Meta Tag Auditor

A lightweight Python CLI tool that audits `robots` meta tag directives and `X-Robots-Tag` HTTP headers for a list of URLs. It checks whether pages are set to `index` or `noindex`, both globally and per crawler (Googlebot, Bingbot), and reports any conflicts.

Built for auditing ScienceDirect article and page URLs as part of SEO and accessibility compliance workflows.

---

## What It Checks

For each URL the tool reports:

- **Global robots directive** — from `<meta name="robots">` or `X-Robots-Tag` header
- **Googlebot directive** — crawler-specific override if present
- **Bingbot directive** — crawler-specific override if present
- **Indexable** — whether the page is effectively indexable (true/false)
- **Notes** — flags conflicts between crawlers or between meta tag and HTTP header
- **Error** — timeout, connection error, or HTTP error if the request failed

### Directive priority (high → low)

1. Crawler-specific `X-Robots-Tag` header (e.g. `googlebot: noindex`)
2. Crawler-specific `<meta>` tag (e.g. `<meta name="googlebot">`)
3. Global `X-Robots-Tag` header
4. Global `<meta name="robots">` tag

---

## Installation

Requires Python 3.10+.

```bash
pip install curl-cffi beautifulsoup4 lxml
```

---

## Usage

```bash
# Basic run
python robots_audit.py input.csv

# Custom output file
python robots_audit.py input.csv --output my_results.csv

# Custom delay between requests (default: 0.6s)
python robots_audit.py input.csv --delay 1.0

# Custom User-Agent
python robots_audit.py input.csv --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
```

### Input CSV format

One URL per row, first column used. Header row is auto-detected and skipped.

```
url
https://www.sciencedirect.com/science/article/pii/S0957417424010339
https://www.sciencedirect.com/journal/pharmacology
```

---

## Output

Results are written to `results.csv` (or the path specified with `--output`).

| url | final_url | robots_global | robots_googlebot | robots_bingbot | indexable | notes | error |
|-----|-----------|---------------|------------------|----------------|-----------|-------|-------|
| https://www.example.com/article | https://www.example.com/article | index, follow | not set | not set | true | | |
| https://www.example.com/search | https://www.example.com/search | noindex, follow | not set | not set | false | | |
| https://www.example.com/page | https://www.example.com/page | index, follow | index, follow | noindex | false | Crawler conflict: googlebot differs from bingbot | |

A summary is also printed to the terminal at the end:

```
--- Audit Summary ---
Total URLs:   100
Indexable:    87 (87.0%)
Noindex:      11 (11.0%)
Errors:       2 (2.0%)
```

---

## Crawl Safety

The tool is designed to be safe to run against production URLs:

- Max **2 requests/second** (configurable via `--delay`)
- Max **3 concurrent requests** (semaphore-capped)
- **10 second timeout** per request, with one automatic retry on timeout or 5xx
- Follows redirects up to **5 hops**, records final URL
- Uses `curl_cffi` to impersonate Chrome's TLS fingerprint, avoiding WAF false positives
- Does **not** spoof Googlebot or other crawlers by default

---

## Troubleshooting

**Getting 403 errors?**

The tool uses a Chrome TLS fingerprint via `curl_cffi`. If you still get 403s, try passing a full browser User-Agent explicitly:

```bash
python robots_audit.py input.csv --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
```

**Permission error writing results.csv?**

Close the file in Excel before running the script.

**pip not recognised on Windows?**

Use:
```bash
python -m pip install curl-cffi beautifulsoup4 lxml
```

---

## License

Internal tool — Elsevier / ScienceDirect SEO team.
