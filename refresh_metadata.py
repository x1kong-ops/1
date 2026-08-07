#!/usr/bin/env python3
"""Refresh the public Guolaoxing WordPress metadata index.

This crawler is deliberately metadata-only: it stores titles, dates, categories,
post IDs, and public URLs. It does not store article bodies, raw HTML, comments,
accounts, orders, or image files. A full scan is intended for the initial run and
weekly audits; daily runs use a modified-time overlap window and merge into the
existing sharded index.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "memory"
SHARD_DIR = MEMORY_DIR / "site_index_shards"
STATUS_PATH = ROOT / "refresh_status.json"
CATALOG_PATH = MEMORY_DIR / "site_index_catalog.json"
CATEGORIES_PATH = MEMORY_DIR / "categories.json"
RECENT_PATH = MEMORY_DIR / "site_index_recent.jsonl"

BASE_URL = "https://guolaoxing.com/"
ROBOTS_URL = urllib.parse.urljoin(BASE_URL, "robots.txt")
POSTS_URL = urllib.parse.urljoin(BASE_URL, "wp-json/wp/v2/posts")
CATEGORIES_URL = urllib.parse.urljoin(BASE_URL, "wp-json/wp/v2/categories")
BOT_NAME = "GuolaoxingPublicMetadataIndexer"
CONTACT_URL = "https://github.com/x1kong-ops/1/issues"
USER_AGENT = f"{BOT_NAME}/3.0 (+{CONTACT_URL}; public metadata only)"
POST_FIELDS = "id,date,modified,link,slug,status,type,title,categories"
CATEGORY_FIELDS = "id,name,slug,count"
PER_PAGE = 100
SHARD_COUNT = 256
DEFAULT_OVERLAP_HOURS = 72


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_html_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parser = TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(text).split())


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_wp_datetime(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # WordPress REST dates are normally local ISO-8601 values without a zone.
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(record: dict[str, Any]) -> str:
    stable = {
        "post_id": record["post_id"],
        "title": record["title"],
        "url": record["url"],
        "slug": record["slug"],
        "published_date": record["published_date"],
        "modified_date": record["modified_date"],
        "category_ids": record["category_ids"],
        "categories": record["categories"],
    }
    return hashlib.sha256(json_dumps(stable).encode("utf-8")).hexdigest()


class PoliteHttpClient:
    def __init__(self, delay_seconds: float, timeout_seconds: int = 35, retries: int = 5) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._last_request_monotonic: float | None = None

    def _wait(self) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str, *, accept: str = "application/json") -> tuple[bytes, dict[str, str], int]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                },
                method="GET",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    status = int(getattr(response, "status", 200))
                    self._last_request_monotonic = time.monotonic()
                    return body, headers, status
            except urllib.error.HTTPError as exc:
                self._last_request_monotonic = time.monotonic()
                if exc.code == 404:
                    raise
                last_error = exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt + 1 >= self.retries:
                    raise
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(min(60.0, delay + random.random()))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_request_monotonic = time.monotonic()
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise
                time.sleep(min(60.0, 2**attempt + random.random()))
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Failed to fetch {url}")

    def get_json(self, url: str) -> tuple[Any, dict[str, str], int]:
        body, headers, status = self.get(url)
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            sample = body[:200].decode("utf-8", errors="replace")
            raise ValueError(f"Invalid JSON from {url}: {sample!r}") from exc
        return payload, headers, status


def check_robots(client: PoliteHttpClient) -> dict[str, Any]:
    try:
        body, _, status = client.get(ROBOTS_URL, accept="text/plain,*/*;q=0.1")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"status": "not_found", "allowed": True, "http_status": 404}
        raise RuntimeError(f"robots.txt returned HTTP {exc.code}; refusing to crawl") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not read robots.txt; refusing to crawl: {exc}") from exc

    text = body.decode("utf-8", errors="replace")
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(ROBOTS_URL)
    parser.parse(text.splitlines())
    allowed_posts = parser.can_fetch(BOT_NAME, POSTS_URL)
    allowed_categories = parser.can_fetch(BOT_NAME, CATEGORIES_URL)
    if not (allowed_posts and allowed_categories):
        raise PermissionError("robots.txt does not allow the metadata endpoints for this crawler")
    crawl_delay = parser.crawl_delay(BOT_NAME) or parser.crawl_delay("*")
    if crawl_delay is not None:
        client.delay_seconds = max(client.delay_seconds, float(crawl_delay))
    return {
        "status": "read",
        "allowed": True,
        "http_status": status,
        "crawl_delay": crawl_delay,
    }


def api_url(base: str, params: dict[str, Any]) -> str:
    encoded = urllib.parse.urlencode(params, doseq=True)
    return f"{base}?{encoded}"


def header_int(headers: dict[str, str], name: str, default: int = 0) -> int:
    value = headers.get(name.lower())
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def fetch_categories(client: PoliteHttpClient) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    categories: dict[int, dict[str, Any]] = {}
    page = 1
    total_pages = 1
    total_reported = 0
    while page <= total_pages:
        url = api_url(
            CATEGORIES_URL,
            {
                "per_page": PER_PAGE,
                "page": page,
                "hide_empty": "false",
                "orderby": "id",
                "order": "asc",
                "_fields": CATEGORY_FIELDS,
            },
        )
        payload, headers, _ = client.get_json(url)
        if not isinstance(payload, list):
            raise ValueError("Categories endpoint did not return a list")
        if page == 1:
            total_pages = max(1, header_int(headers, "x-wp-totalpages", 1))
            total_reported = header_int(headers, "x-wp-total", len(payload))
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            category_id = int(item["id"])
            categories[category_id] = {
                "id": category_id,
                "name": clean_html_text(item.get("name")),
                "slug": str(item.get("slug") or ""),
                "count": int(item.get("count") or 0),
            }
        page += 1
    return categories, {
        "record_count": len(categories),
        "source_total_reported": total_reported,
        "page_count": total_pages,
    }


def normalize_post(
    item: dict[str, Any],
    categories: dict[int, dict[str, Any]],
    *,
    observed_at: str,
    existing: dict[str, Any] | None = None,
    source_method: str,
) -> dict[str, Any]:
    if not isinstance(item.get("id"), int):
        raise ValueError("Post record has no integer id")
    post_id = int(item["id"])
    raw_title = item.get("title")
    if isinstance(raw_title, dict):
        raw_title = raw_title.get("rendered")
    category_ids = sorted({int(value) for value in (item.get("categories") or []) if isinstance(value, int)})
    category_names = [categories.get(value, {"name": f"category_id:{value}"})["name"] for value in category_ids]
    record: dict[str, Any] = {
        "post_id": post_id,
        "title": clean_html_text(raw_title),
        "url": str(item.get("link") or f"{BASE_URL.rstrip('/')}/archives/{post_id}"),
        "slug": str(item.get("slug") or ""),
        "published_date": parse_wp_datetime(item.get("date")),
        "modified_date": parse_wp_datetime(item.get("modified")),
        "categories": category_names,
        "category_ids": category_ids,
        "status": str(item.get("status") or "publish"),
        "post_type": str(item.get("type") or "post"),
        "active": True,
        "source_method": source_method,
        "first_seen_at": (existing or {}).get("first_seen_at") or observed_at,
        "last_seen_at": observed_at,
    }
    record["record_hash"] = record_hash(record)
    return record


def fetch_post_pages(
    client: PoliteHttpClient,
    categories: dict[int, dict[str, Any]],
    *,
    mode: str,
    modified_after: str | None,
    max_pages: int | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    common: dict[str, Any] = {
        "per_page": PER_PAGE,
        "status": "publish",
        "_fields": POST_FIELDS,
    }
    if mode == "full":
        common.update({"orderby": "id", "order": "asc"})
    else:
        common.update({"orderby": "modified", "order": "asc", "modified_after": modified_after})

    observed_at = utc_now()
    records: dict[int, dict[str, Any]] = {}
    page = 1
    total_pages = 1
    source_total = 0
    pages_fetched = 0

    while page <= total_pages:
        if max_pages is not None and pages_fetched >= max_pages:
            break
        params = dict(common)
        params["page"] = page
        payload, headers, _ = client.get_json(api_url(POSTS_URL, params))
        if not isinstance(payload, list):
            raise ValueError("Posts endpoint did not return a list")
        if page == 1:
            total_pages = max(1, header_int(headers, "x-wp-totalpages", 1))
            source_total = header_int(headers, "x-wp-total", len(payload))
        for item in payload:
            if isinstance(item, dict):
                record = normalize_post(
                    item,
                    categories,
                    observed_at=observed_at,
                    source_method="wordpress_rest_full" if mode == "full" else "wordpress_rest_incremental",
                )
                records[record["post_id"]] = record
        pages_fetched += 1
        page += 1

    # In a full scan, a post may appear while pages are being traversed. Probe the
    # current totals and append newly created final pages when ordering by ID.
    final_total = source_total
    final_total_pages = total_pages
    if mode == "full" and max_pages is None:
        probe = dict(common)
        probe["page"] = 1
        _, headers, _ = client.get_json(api_url(POSTS_URL, probe))
        final_total = header_int(headers, "x-wp-total", source_total)
        final_total_pages = max(1, header_int(headers, "x-wp-totalpages", total_pages))
        while page <= final_total_pages:
            params = dict(common)
            params["page"] = page
            payload, _, _ = client.get_json(api_url(POSTS_URL, params))
            if not isinstance(payload, list):
                raise ValueError("Posts endpoint did not return a list")
            for item in payload:
                if isinstance(item, dict):
                    record = normalize_post(
                        item,
                        categories,
                        observed_at=observed_at,
                        source_method="wordpress_rest_full",
                    )
                    records[record["post_id"]] = record
            pages_fetched += 1
            page += 1

    complete_scan = mode != "full" or (
        max_pages is None and pages_fetched >= final_total_pages and len(records) == final_total
    )
    return records, {
        "pages_fetched": pages_fetched,
        "source_total_reported": final_total,
        "source_total_pages": final_total_pages,
        "complete_scan": complete_scan,
        "observed_at": observed_at,
        "max_pages": max_pages,
    }


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {path}:{line_number}")
            yield value


def load_existing_shards() -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    if not SHARD_DIR.exists():
        return records
    for path in sorted(SHARD_DIR.glob("*.jsonl")):
        for value in read_jsonl(path):
            post_id = value.get("post_id")
            if isinstance(post_id, int):
                records[post_id] = value
    return records


def merge_incremental(
    existing: dict[int, dict[str, Any]], updates: dict[int, dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], dict[str, int]]:
    added = 0
    changed = 0
    unchanged = 0
    for post_id, update in updates.items():
        previous = existing.get(post_id)
        if previous is None:
            added += 1
        else:
            update["first_seen_at"] = previous.get("first_seen_at") or update["first_seen_at"]
            if previous.get("record_hash") == update.get("record_hash"):
                unchanged += 1
            else:
                changed += 1
        existing[post_id] = update
    return existing, {"added": added, "changed": changed, "unchanged": unchanged}


def write_json_if_changed(path: Path, value: Any) -> bool:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def write_text_if_changed(path: Path, content: str) -> bool:
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def write_shards(records: dict[int, dict[str, Any]]) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    SHARD_DIR.mkdir(parents=True, exist_ok=True)
    buckets: dict[int, list[dict[str, Any]]] = {index: [] for index in range(SHARD_COUNT)}
    for post_id, record in records.items():
        buckets[post_id % SHARD_COUNT].append(record)

    manifest: list[dict[str, Any]] = []
    changed_files = 0
    expected_paths: set[Path] = set()
    for bucket, values in buckets.items():
        if not values:
            continue
        values.sort(key=lambda row: int(row["post_id"]))
        path = SHARD_DIR / f"{bucket:03d}.jsonl"
        expected_paths.add(path)
        content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in values)
        if write_text_if_changed(path, content):
            changed_files += 1
        manifest.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "record_count": len(values),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "min_post_id": values[0]["post_id"],
                "max_post_id": values[-1]["post_id"],
            }
        )

    for stale in SHARD_DIR.glob("*.jsonl"):
        if stale not in expected_paths:
            stale.unlink()
            changed_files += 1

    recent = sorted(
        records.values(),
        key=lambda row: (row.get("modified_date") or row.get("published_date") or "", row["post_id"]),
        reverse=True,
    )[:500]
    recent_content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in recent)
    if write_text_if_changed(RECENT_PATH, recent_content):
        changed_files += 1

    return {
        "shards": manifest,
        "shard_count": len(manifest),
        "record_count": len(records),
        "changed_files": changed_files,
        "recent_record_count": len(recent),
    }


def update_phase2_status(record_count: int, completed_at: str) -> None:
    path = ROOT / "PHASE2_STATUS.json"
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            value = {}
    else:
        value = {}
    value.update(
        {
            "full_site_materialized": True,
            "materialization_status": "complete_rest_metadata_scan_live",
            "current_active_records": record_count,
            "exact_unique_public_posts": record_count,
            "last_full_scan_at": completed_at,
            "metadata_only": True,
            "stores_article_bodies": False,
            "stores_image_files": False,
        }
    )
    write_json_if_changed(path, value)


def write_status(value: dict[str, Any]) -> None:
    write_json_if_changed(STATUS_PATH, value)


def run_refresh(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now()
    mode = args.mode
    if mode == "auto":
        weekday = dt.datetime.now(dt.timezone.utc).isoweekday()
        mode = "full" if weekday == 7 or not CATALOG_PATH.exists() else "incremental"
    if mode == "incremental" and not SHARD_DIR.exists():
        mode = "full"

    status: dict[str, Any] = {
        "status": "running",
        "mode": mode,
        "started_at": started_at,
        "completed_at": None,
        "metadata_only": True,
        "stores_article_bodies": False,
        "stores_image_files": False,
        "base_url": BASE_URL,
        "contact": CONTACT_URL,
        "error": None,
    }
    write_status(status)

    client = PoliteHttpClient(delay_seconds=args.delay, timeout_seconds=args.timeout, retries=args.retries)
    robots = check_robots(client)
    categories, category_stats = fetch_categories(client)

    modified_after = None
    if mode == "incremental":
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.overlap_hours)
        modified_after = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    fetched, fetch_stats = fetch_post_pages(
        client,
        categories,
        mode=mode,
        modified_after=modified_after,
        max_pages=args.max_pages,
    )

    if mode == "full":
        if not fetch_stats["complete_scan"] and args.max_pages is None:
            raise RuntimeError(
                "Full scan was incomplete: "
                f"fetched {len(fetched)} unique posts; source reported {fetch_stats['source_total_reported']}"
            )
        records = fetched
        changes = {"added": len(fetched), "changed": 0, "unchanged": 0}
    else:
        existing = load_existing_shards()
        records, changes = merge_incremental(existing, fetched)

    shard_stats = write_shards(records)
    completed_at = utc_now()

    category_list = sorted(categories.values(), key=lambda item: int(item["id"]))
    write_json_if_changed(
        CATEGORIES_PATH,
        {
            "generated_at": completed_at,
            "source": CATEGORIES_URL,
            "record_count": len(category_list),
            "categories": category_list,
        },
    )

    catalog = {
        "generated_at": completed_at,
        "source": POSTS_URL,
        "mode": mode,
        "metadata_only": True,
        "record_count": shard_stats["record_count"],
        "shard_count": shard_stats["shard_count"],
        "shards": shard_stats["shards"],
        "recent_index": RECENT_PATH.relative_to(ROOT).as_posix(),
        "schema": [
            "post_id",
            "title",
            "url",
            "slug",
            "published_date",
            "modified_date",
            "categories",
            "category_ids",
            "status",
            "post_type",
            "active",
            "source_method",
            "first_seen_at",
            "last_seen_at",
            "record_hash",
        ],
    }
    write_json_if_changed(CATALOG_PATH, catalog)

    status.update(
        {
            "status": "success",
            "completed_at": completed_at,
            "robots": robots,
            "modified_after": modified_after,
            "category_count": category_stats["record_count"],
            "category_source_total_reported": category_stats["source_total_reported"],
            "record_count": shard_stats["record_count"],
            "shard_count": shard_stats["shard_count"],
            "recent_record_count": shard_stats["recent_record_count"],
            "changed_files": shard_stats["changed_files"],
            "fetched_records_this_run": len(fetched),
            "source_total_reported": fetch_stats["source_total_reported"],
            "source_total_pages": fetch_stats["source_total_pages"],
            "pages_fetched": fetch_stats["pages_fetched"],
            "complete_scan": bool(fetch_stats["complete_scan"]),
            "changes": changes,
        }
    )
    if mode == "full" and fetch_stats["complete_scan"] and args.max_pages is None:
        update_phase2_status(shard_stats["record_count"], completed_at)
    write_status(status)
    return status


def self_test() -> None:
    assert clean_html_text("A &amp; B <em>测试</em>") == "A & B 测试"
    assert parse_wp_datetime("2026-08-06T12:34:56") == "2026-08-06T12:34:56"
    category_map = {2: {"id": 2, "name": "股市经济", "slug": "gsjj", "count": 1}}
    first = normalize_post(
        {
            "id": 101,
            "date": "2026-08-01T00:00:00",
            "modified": "2026-08-02T00:00:00",
            "link": "https://guolaoxing.com/archives/101",
            "slug": "example",
            "status": "publish",
            "type": "post",
            "title": {"rendered": "示例 &amp; 标题"},
            "categories": [2],
        },
        category_map,
        observed_at="2026-08-06T00:00:00Z",
        source_method="test",
    )
    assert first["title"] == "示例 & 标题"
    assert first["categories"] == ["股市经济"]
    existing = {101: first.copy()}
    second = first.copy()
    second["title"] = "已修改"
    second["record_hash"] = record_hash(second)
    merged, changes = merge_incremental(existing, {101: second, 102: {**first, "post_id": 102}})
    assert len(merged) == 2
    assert changes["changed"] == 1 and changes["added"] == 1
    print("refresh_metadata.py self-test: ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "full", "incremental"), default="auto")
    parser.add_argument("--delay", type=float, default=float(os.environ.get("GUOLAOXING_DELAY_SECONDS", "1.25")))
    parser.add_argument("--timeout", type=int, default=35)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS)
    parser.add_argument("--max-pages", type=int, default=None, help="Debug/testing limit; never marks a full scan complete.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    try:
        result = run_refresh(args)
    except Exception as exc:
        failure = {
            "status": "failed",
            "mode": args.mode,
            "started_at": None,
            "completed_at": utc_now(),
            "metadata_only": True,
            "stores_article_bodies": False,
            "stores_image_files": False,
            "base_url": BASE_URL,
            "contact": CONTACT_URL,
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            prior = json.loads(STATUS_PATH.read_text(encoding="utf-8")) if STATUS_PATH.exists() else {}
            if isinstance(prior, dict):
                failure["started_at"] = prior.get("started_at")
                if prior.get("mode"):
                    failure["mode"] = prior["mode"]
        except Exception:
            pass
        write_status(failure)
        print(failure["error"], file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
