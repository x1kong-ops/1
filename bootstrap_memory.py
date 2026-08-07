#!/usr/bin/env python3
"""Materialize and summarize the compact Guolaoxing project-memory bundle.

The repository stores the compressed bundle as numbered base64 text parts so it can
be created through GitHub's text Contents API. This script verifies the archive,
extracts only the approved metadata/summary files, validates JSONL records, and
writes human- and machine-readable status files.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parent
PART_DIR = ROOT / "bootstrap"
EXPECTED_ARCHIVE_SHA256 = "70e0dafa2b3c09ef7eb783ebc92d695475a08e1ac05858e25f4465ee9e0a8409"
REQUIRED_FILES = (
    "articles.jsonl",
    "market_claims.jsonl",
    "figures.jsonl",
    "site_index.jsonl",
    "phase3_profiles.jsonl",
    "phase3_queue.jsonl",
    "PHASE2_STATUS.json",
    "PHASE3_STATUS.json",
    "FINAL_VALIDATION.json",
)
JSONL_FILES = tuple(name for name in REQUIRED_FILES if name.endswith(".jsonl"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def validate_jsonl(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected a JSON object at {path}:{line_number}")
            count += 1
    return count


def safe_extract(archive_path: Path) -> None:
    root_resolved = ROOT.resolve()
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            if member.issym() or member.islnk():
                raise ValueError(f"Archive links are not allowed: {member.name}")
            target = (ROOT / member.name).resolve()
            if target != root_resolved and root_resolved not in target.parents:
                raise ValueError(f"Unsafe archive path: {member.name}")
        try:
            archive.extractall(ROOT, members=members, filter="data")
        except TypeError:  # Python < 3.12 compatibility.
            archive.extractall(ROOT, members=members)


def materialize() -> dict[str, Any]:
    parts = sorted(PART_DIR.glob("core_memory.part*"))
    if not parts:
        raise FileNotFoundError(f"No bootstrap parts found in {PART_DIR}")

    expected_names = [f"core_memory.part{index:02d}" for index in range(len(parts))]
    actual_names = [part.name for part in parts]
    if actual_names != expected_names:
        raise ValueError(f"Bootstrap parts are not contiguous: {actual_names}")

    encoded = "".join(part.read_text(encoding="ascii").strip() for part in parts)
    try:
        archive_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # binascii.Error is intentionally wrapped with context.
        raise ValueError("The bootstrap base64 payload is invalid") from exc

    archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    if archive_sha != EXPECTED_ARCHIVE_SHA256:
        raise ValueError(
            f"Bootstrap archive SHA-256 mismatch: expected {EXPECTED_ARCHIVE_SHA256}, got {archive_sha}"
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="guolaoxing-core-", suffix=".tar.gz", delete=False) as handle:
            handle.write(archive_bytes)
            temporary_path = Path(handle.name)
        safe_extract(temporary_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Materialization did not produce required files: {missing}")

    return {
        "part_count": len(parts),
        "archive_bytes": len(archive_bytes),
        "archive_sha256": archive_sha,
    }


def build_status(materialization: dict[str, Any] | None = None) -> dict[str, Any]:
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Cannot build status; required files are missing: {missing}")

    counts = {name: validate_jsonl(ROOT / name) for name in JSONL_FILES}
    phase2 = read_json(ROOT / "PHASE2_STATUS.json")
    phase3 = read_json(ROOT / "PHASE3_STATUS.json")
    final_validation = read_json(ROOT / "FINAL_VALIDATION.json")
    refresh = read_json(ROOT / "refresh_status.json")

    live_shard_files = sorted((ROOT / "memory" / "site_index_shards").glob("*.jsonl"))
    live_shard_records = 0
    for shard in live_shard_files:
        live_shard_records += validate_jsonl(shard)

    status: dict[str, Any] = {
        "generated_at": utc_now(),
        "memory_model": "external_versioned_repository",
        "repository": os.environ.get("GITHUB_REPOSITORY", "x1kong-ops/1"),
        "core_materialized": True,
        "core_counts": counts,
        "core_validation_ok": bool(final_validation.get("ok", False)),
        "phase2_materialization_status": phase2.get("materialization_status"),
        "phase2_full_site_materialized": bool(phase2.get("full_site_materialized", False)),
        "phase3_status": phase3.get("status"),
        "phase3_full_archive_analyzed": bool(phase3.get("full_archive_analyzed", False)),
        "live_metadata": {
            "refresh_status": refresh.get("status", "not_run"),
            "mode": refresh.get("mode"),
            "completed_at": refresh.get("completed_at"),
            "record_count": refresh.get("record_count", live_shard_records),
            "shard_count": refresh.get("shard_count", len(live_shard_files)),
            "source_total_reported": refresh.get("source_total_reported"),
            "complete_scan": refresh.get("complete_scan", False),
            "error": refresh.get("error"),
        },
        "retrieval_policy": {
            "curated_market_first": True,
            "xingming_isolated": True,
            "astrology_context_only": True,
            "current_facts_require_fresh_verification": True,
        },
    }
    if materialization:
        status["bootstrap"] = materialization

    (ROOT / "PROJECT_MEMORY_STATUS.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    live = status["live_metadata"]
    lines = [
        "# Project Memory Status",
        "",
        f"- Generated: `{status['generated_at']}`",
        f"- Repository: `{status['repository']}`",
        "- Memory type: external, versioned project memory (not model-parameter memory)",
        f"- Core validation: `{'ok' if status['core_validation_ok'] else 'not confirmed'}`",
        "",
        "## Curated core",
        "",
        f"- Articles: **{counts['articles.jsonl']}**",
        f"- Structured market claims: **{counts['market_claims.jsonl']}**",
        f"- Figure contexts: **{counts['figures.jsonl']}**",
        f"- Curated/seed metadata index: **{counts['site_index.jsonl']}**",
        f"- Isolated phase-three profiles: **{counts['phase3_profiles.jsonl']}**",
        f"- Phase-three queue records: **{counts['phase3_queue.jsonl']}**",
        "",
        "## Live public-metadata index",
        "",
        f"- Refresh status: `{live['refresh_status']}`",
        f"- Last mode: `{live['mode'] or 'not_run'}`",
        f"- Records: **{live['record_count'] or 0}**",
        f"- Searchable shards: **{live['shard_count'] or 0}**",
        f"- Complete scan: `{bool(live['complete_scan'])}`",
    ]
    if live.get("error"):
        lines.append(f"- Last error: `{live['error']}`")
    lines.extend(
        [
            "",
            "## Retrieval guardrails",
            "",
            "1. Use `market_claims.jsonl`, `articles.jsonl`, and `figures.jsonl` for curated market context.",
            "2. Use `memory/site_index_shards/` only to discover titles, people, dates, categories, and source URLs.",
            "3. Keep `phase3_profiles.jsonl` and `phase3_queue.jsonl` isolated from core investment conclusions unless independently verified.",
            "4. Treat astrology/命理 material as source context only, never as a standalone trade signal.",
            "5. Verify prices, filings, earnings, macro data, laws, and other time-sensitive facts from current primary sources.",
            "",
        ]
    )
    (ROOT / "PROJECT_MEMORY_STATUS.md").write_text("\n".join(lines), encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Validate already materialized files and regenerate status without extracting the bootstrap archive.",
    )
    args = parser.parse_args()

    materialization = None if args.status_only else materialize()
    status = build_status(materialization)
    print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
