#!/usr/bin/env python3
"""Validate the generated bear-data bundle before publication."""

from datetime import datetime
from pathlib import Path
import argparse
import json


def validate(path: Path, minimum_sources: int) -> tuple[int, int]:
    pack = json.loads(path.read_text(encoding="utf-8"))
    records = pack.get("records")
    sources = pack.get("meta", {}).get("sources")
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty list")
    if not isinstance(sources, list) or len(sources) < minimum_sources:
        raise ValueError(f"expected at least {minimum_sources} source entries")

    ids = set()
    counts = {}
    for index, record in enumerate(records):
        record_id = record.get("id")
        source_key = record.get("source_key")
        if not record_id or record_id in ids:
            raise ValueError(f"missing or duplicate id at record {index}: {record_id}")
        ids.add(record_id)
        counts[source_key] = counts.get(source_key, 0) + 1

        latitude, longitude = record.get("latitude"), record.get("longitude")
        if not isinstance(latitude, (int, float)) or not 20 <= latitude <= 47:
            raise ValueError(f"invalid latitude for {record_id}: {latitude}")
        if not isinstance(longitude, (int, float)) or not 120 <= longitude <= 155:
            raise ValueError(f"invalid longitude for {record_id}: {longitude}")
        try:
            timestamp = datetime.fromisoformat(record.get("reported_at", ""))
        except ValueError as exc:
            raise ValueError(f"invalid timestamp for {record_id}") from exc
        if timestamp.tzinfo is None:
            raise ValueError(f"timezone missing for {record_id}")

    for source in sources:
        key = source.get("key")
        if counts.get(key, 0) != source.get("record_count"):
            raise ValueError(
                f"source count mismatch for {key}: "
                f"metadata={source.get('record_count')} records={counts.get(key, 0)}")
        if source.get("fetch_status") not in {"current", "stale_fallback"}:
            raise ValueError(f"invalid fetch status for {key}")

    if pack.get("meta", {}).get("record_count") != len(records):
        raise ValueError("total record count does not match metadata")
    return len(records), len(sources)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="akita_bears.json")
    parser.add_argument("--minimum-sources", type=int, default=8)
    args = parser.parse_args()
    record_count, source_count = validate(Path(args.path), args.minimum_sources)
    print(f"Validated {record_count:,} records from {source_count} sources")
