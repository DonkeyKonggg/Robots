#!/usr/bin/env python3
"""
robots_audit.py - Audit robots meta tag directives (index/noindex) for a list of URLs.

Dependencies:
    pip install httpx beautifulsoup4 lxml

Usage:
    python robots_audit.py input.csv
    python robots_audit.py input.csv --output my_results.csv --delay 1.0
    python robots_audit.py input.csv --user-agent "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
"""

import asyncio
import csv
import sys
import argparse
import time
import re
from typing import Optional
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 5
MAX_CONCURRENCY = 3
DEFAULT_DELAY = 0.6  # seconds between requests

# Crawler-specific meta tag names we recognise (lowercased)
KNOWN_CRAWLERS = ["googlebot", "bingbot", "googlebot-news", "googlebot-image", "googlebot-video"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RobotsResult:
    url: str
    final_url: str = ""
    robots_global: str = "not set"
    robots_googlebot: str = "not set"
    robots_bingbot: str = "not set"
    indexable: bool = True
    notes: list = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _normalise(value: str) -> str:
    """Strip whitespace and lowercase a directive value."""
    return value.strip().lower()


def _directives_from_content(content: str) -> set[str]:
    """Split a comma-separated robots content value into a set of directive tokens."""
    return {_normalise(d) for d in content.split(",") if d.strip()}


def _has_noindex(directives: set[str]) -> bool:
    return "noindex" in directives


def _has_index(directives: set[str]) -> bool:
    return "index" in directives


def parse_meta_robots(soup: BeautifulSoup) -> dict[str, str]:
    """
    Extract all robots-related <meta> tags from parsed HTML.

    Returns a dict mapping crawler name (e.g. 'robots', 'googlebot') to raw content value.
    """
    meta_directives: dict[str, str] = {}
    for tag in soup.find_all("meta", attrs={"name": True, "content": True}):
        name = _normalise(tag["name"])
        content = tag["content"].strip()
        if name == "robots" or name in KNOWN_CRAWLERS:
            # If duplicate tags exist for the same crawler, last one wins (browser behaviour)
            meta_directives[name] = content
    return meta_directives


def parse_x_robots_tag(headers) -> dict[str, str]:
    """
    Parse X-Robots-Tag HTTP response header(s).

    The header may appear multiple times and may contain an optional crawler prefix:
        X-Robots-Tag: noindex
        X-Robots-Tag: googlebot: noindex, nofollow
    Returns a dict mapping crawler name to content string (using 'global' as key for
    directives that apply to all crawlers).
    """
    x_robots: dict[str, str] = {}
    raw_values = headers.get_list("x-robots-tag") if hasattr(headers, "get_list") else []
    if not raw_values:
        raw = headers.get("x-robots-tag")
        raw_values = [raw] if raw else []

    for value in raw_values:
        value = value.strip()
        # Check for "crawlername: directives" format
        colon_pos = value.find(":")
        if colon_pos != -1:
            potential_crawler = _normalise(value[:colon_pos])
            remainder = value[colon_pos + 1:].strip()
            # Heuristic: if the part before the colon looks like a known crawler or at least
            # doesn't contain spaces/commas, treat it as a crawler-specific directive.
            if potential_crawler and " " not in potential_crawler and "," not in potential_crawler:
                x_robots[potential_crawler] = remainder
                continue
        # Global directive (no crawler prefix)
        x_robots["global"] = value

    return x_robots


def effective_directive(
    crawler: str,
    meta: dict[str, str],
    x_robots: dict[str, str],
) -> tuple[str, list[str]]:
    """
    Determine the effective directive for a given crawler.

    Priority (high → low):
    1. Crawler-specific X-Robots-Tag header
    2. Crawler-specific <meta> tag
    3. Global X-Robots-Tag header
    4. Global <meta name="robots"> tag
    5. Default: 'index' (indexable)

    Returns (directive_string, notes_list).
    """
    notes: list[str] = []

    crawler_x = x_robots.get(crawler)
    crawler_meta = meta.get(crawler)
    global_x = x_robots.get("global")
    global_meta = meta.get("robots")

    sources: list[tuple[str, str]] = []
    if crawler_x:
        sources.append(("crawler X-Robots-Tag", crawler_x))
    if crawler_meta:
        sources.append(("crawler meta tag", crawler_meta))
    if global_x:
        sources.append(("global X-Robots-Tag", global_x))
    if global_meta:
        sources.append(("global meta tag", global_meta))

    if not sources:
        return "not set", []

    # Effective value is the highest-priority source
    effective = sources[0][1]

    # Flag conflicts between meta tag and X-Robots-Tag at the same scope
    if crawler_x and crawler_meta:
        dx = _directives_from_content(crawler_x)
        dm = _directives_from_content(crawler_meta)
        if _has_noindex(dx) != _has_noindex(dm):
            notes.append(
                f"Conflict for {crawler}: crawler-specific X-Robots-Tag ({crawler_x!r}) "
                f"vs meta tag ({crawler_meta!r})"
            )

    if global_x and global_meta:
        dx = _directives_from_content(global_x)
        dm = _directives_from_content(global_meta)
        if _has_noindex(dx) != _has_noindex(dm):
            notes.append(
                f"Conflict: global X-Robots-Tag ({global_x!r}) "
                f"vs global meta tag ({global_meta!r})"
            )

    return effective, notes


def compute_indexable(
    meta: dict[str, str],
    x_robots: dict[str, str],
) -> tuple[bool, list[str]]:
    """
    Compute overall indexability and collect cross-crawler notes.

    A page is considered not indexable if *any* major crawler is blocked.
    Also flags cases where crawlers have conflicting directives.
    """
    notes: list[str] = []

    googlebot_val, gn = effective_directive("googlebot", meta, x_robots)
    bingbot_val, bn = effective_directive("bingbot", meta, x_robots)
    notes.extend(gn)
    notes.extend(bn)

    # Also check global directives
    global_x = x_robots.get("global", "")
    global_meta = meta.get("robots", "")

    # Determine noindex status for each scope
    g_noindex = _has_noindex(_directives_from_content(googlebot_val)) if googlebot_val != "not set" else None
    b_noindex = _has_noindex(_directives_from_content(bingbot_val)) if bingbot_val != "not set" else None

    global_noindex: Optional[bool] = None
    if global_x:
        global_noindex = _has_noindex(_directives_from_content(global_x))
    elif global_meta:
        global_noindex = _has_noindex(_directives_from_content(global_meta))

    # Flag cross-crawler conflicts
    if g_noindex is not None and b_noindex is not None and g_noindex != b_noindex:
        notes.append(
            f"Crawler conflict: googlebot directive ({googlebot_val!r}) "
            f"differs from bingbot directive ({bingbot_val!r})"
        )

    # Determine overall indexability
    # If a crawler-specific override exists, it takes precedence for that crawler.
    # For the global indexability judgement we use: noindex if global noindex OR
    # any crawler-specific noindex.
    noindex_signals = []
    if global_noindex:
        noindex_signals.append("global")
    if g_noindex:
        noindex_signals.append("googlebot")
    if b_noindex:
        noindex_signals.append("bingbot")

    indexable = len(noindex_signals) == 0
    return indexable, notes


# ---------------------------------------------------------------------------
# Global rate limiter — enforces minimum spacing between request *starts*
# ---------------------------------------------------------------------------

class RateLimiter:
    """Ensures at least `delay` seconds between consecutive request starts."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._delay - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()


# ---------------------------------------------------------------------------
# HTTP fetching
# ---------------------------------------------------------------------------

async def fetch_url(
    session: httpx.AsyncClient,
    url: str,
    headers: dict,
) -> tuple[object, str]:
    """Fetch a URL, retrying once on timeout or 5xx. Returns (response, error_string)."""
    for attempt in range(2):
        try:
            response = await session.get(url, headers=headers)
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(1)
                continue
            return response, ""
        except httpx.TimeoutException:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return None, f"Timeout after {TIMEOUT_SECONDS}s"
        except httpx.TooManyRedirects:
            return None, f"Too many redirects (>{MAX_REDIRECTS})"
        except httpx.RequestError as exc:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return None, f"Connection error: {exc}"
    return None, "Unknown fetch error"


# ---------------------------------------------------------------------------
# Main audit logic per URL
# ---------------------------------------------------------------------------

async def audit_url(
    session: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    rate_limiter: RateLimiter,
    url: str,
    headers: dict,
) -> RobotsResult:
    result = RobotsResult(url=url)
    async with semaphore:
        await rate_limiter.acquire()
        response, error = await fetch_url(session, url, headers)

    if error or response is None:
        result.error = error or "No response"
        result.final_url = url
        result.indexable = False
        return result

    result.final_url = str(response.url)

    # Check HTTP-level errors
    if response.status_code == 403:
        result.error = "HTTP 403 Forbidden (server blocked the request; try --delay or check IP restrictions)"
        result.indexable = False
        return result
    if response.status_code >= 400:
        result.error = f"HTTP {response.status_code}"
        result.indexable = False
        return result

    # Parse HTML
    try:
        soup = BeautifulSoup(response.text, "lxml")
    except Exception:
        soup = BeautifulSoup(response.text, "html.parser")

    meta = parse_meta_robots(soup)
    x_robots = parse_x_robots_tag(response.headers)

    # Populate result fields
    result.robots_global = meta.get("robots") or x_robots.get("global") or "not set"

    gbot_effective, _ = effective_directive("googlebot", meta, x_robots)
    result.robots_googlebot = gbot_effective

    bbot_effective, _ = effective_directive("bingbot", meta, x_robots)
    result.robots_bingbot = bbot_effective

    indexable, notes = compute_indexable(meta, x_robots)
    result.indexable = indexable
    result.notes = notes

    return result


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

def load_urls(csv_path: str) -> list[str]:
    """Load URLs from the first column of a CSV file, skipping a header row if present."""
    urls: list[str] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        first_row = True
        for row in reader:
            if not row:
                continue
            value = row[0].strip()
            if not value:
                continue
            # Skip header: if the first non-empty cell doesn't look like a URL, treat as header
            if first_row:
                first_row = False
                if not (value.startswith("http://") or value.startswith("https://")):
                    continue
            urls.append(value)
    return urls


OUTPUT_FIELDNAMES = [
    "url", "final_url", "robots_global", "robots_googlebot",
    "robots_bingbot", "indexable", "notes", "error",
]


def write_results(results: list[RobotsResult], output_path: str) -> None:
    try:
        fh = open(output_path, "w", newline="", encoding="utf-8")
    except PermissionError:
        sys.exit(
            f"Error: cannot write to '{output_path}' — close the file in Excel (or any other program) and try again."
        )
    with fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        for r in results:
            writer.writerow({
                "url": r.url,
                "final_url": r.final_url,
                "robots_global": r.robots_global,
                "robots_googlebot": r.robots_googlebot,
                "robots_bingbot": r.robots_bingbot,
                "indexable": r.indexable,
                "notes": "; ".join(r.notes),
                "error": r.error,
            })


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

async def run_audit(urls: list[str], delay: float, user_agent: str = USER_AGENT) -> list[RobotsResult]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    rate_limiter = RateLimiter(delay)
    headers = {"User-Agent": user_agent}

    results: list[RobotsResult] = []
    total = len(urls)
    completed = 0

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        timeout=TIMEOUT_SECONDS,
    ) as session:
        tasks = [audit_url(session, semaphore, rate_limiter, url, headers) for url in urls]

        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            completed += 1
            print(f"Checked {completed}/{total} URLs...", file=sys.stderr, end="\r")

    print(file=sys.stderr)  # newline after progress
    # Restore original order
    url_order = {url: i for i, url in enumerate(urls)}
    results.sort(key=lambda r: url_order.get(r.url, 0))
    return results


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(results: list[RobotsResult]) -> None:
    total = len(results)
    errors = sum(1 for r in results if r.error)
    indexable = sum(1 for r in results if r.indexable and not r.error)
    noindex = sum(1 for r in results if not r.indexable and not r.error)

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "0.0%"

    print("\n--- Audit Summary ---")
    print(f"Total URLs:   {total}")
    print(f"Indexable:    {indexable} ({pct(indexable)})")
    print(f"Noindex:      {noindex} ({pct(noindex)})")
    print(f"Errors:       {errors} ({pct(errors)})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit robots meta tag directives for a list of URLs from a CSV file."
    )
    parser.add_argument("input_csv", help="Path to CSV file containing URLs (one per row, first column used).")
    parser.add_argument(
        "--output", "-o",
        default="results.csv",
        help="Output CSV file path (default: results.csv).",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Delay in seconds between requests (default: {DEFAULT_DELAY}).",
    )
    parser.add_argument(
        "--user-agent", "-u",
        default=USER_AGENT,
        help="User-Agent string to send with requests (default: Chrome impersonation UA).",
    )
    args = parser.parse_args()

    urls = load_urls(args.input_csv)
    if not urls:
        print("No URLs found in input file.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(urls)} URL(s) from {args.input_csv}", file=sys.stderr)
    print(f"User-Agent: {args.user_agent or '(Chrome impersonation default)'}", file=sys.stderr)

    start = time.monotonic()
    results = asyncio.run(run_audit(urls, args.delay, args.user_agent))
    elapsed = time.monotonic() - start

    write_results(results, args.output)
    print(f"Results written to {args.output} ({elapsed:.1f}s elapsed)")
    print_summary(results)


if __name__ == "__main__":
    main()
