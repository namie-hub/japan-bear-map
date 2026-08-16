#!/usr/bin/env python3
"""Offline simulation of consecutive ingest runs (no network).

Stubs every adapter with records from the existing bundle, then verifies:
  1. run 1 writes outputs in the new line-per-record format
  2. an identical run 2 leaves both output files byte-identical (commit guard)
  3. run 3 with records missing from a rolling-window source retains them
     as archived history
  4. a partial --sources run carries all other sources through unchanged
"""
import copy
import json
import shutil
import sys
from pathlib import Path

import ingest

SRC = Path("akita_bears.json")
WORK = Path("_test_out")


def stub_adapters(pack, drop=None):
    """Build ADAPTERS stubs returning per-source data from `pack`."""
    drop = drop or {}
    by_source = {}
    for r in pack["records"]:
        by_source.setdefault(r["source_key"], []).append(r)
    metas = {s["key"]: s for s in pack["meta"]["sources"]}
    stubs = {}
    for key in by_source:
        recs = [dict(r) for r in by_source[key]]
        for r in recs:
            r.pop("archived", None)
        if key in drop:
            recs = recs[drop[key]:]  # drop the first N (newest) records
        meta = {k: v for k, v in metas[key].items()
                if k not in ("fetch_status", "last_fetch_attempt",
                             "retained_history_count")}
        meta["record_count"] = len(recs)
        stubs[key] = (lambda rr=recs, mm=meta: ([dict(x) for x in rr],
                                                dict(mm)))
    return stubs


def main():
    original = json.loads(SRC.read_text(encoding="utf-8"))
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    json_out, js_out = WORK / "akita_bears.json", WORK / "bear_data.js"

    keys = [s["key"] for s in original["meta"]["sources"]]
    ingest.ADAPTERS = stub_adapters(original)

    # --- run 1: fresh build ------------------------------------------------
    n1 = ingest.build(json_out, js_out, keys)
    pack1 = json.loads(json_out.read_text(encoding="utf-8"))
    assert n1 == len(pack1["records"]), "run 1 count mismatch"
    assert '\n"records":[\n' in json_out.read_text(encoding="utf-8"), \
        "expected line-per-record output"
    print(f"run 1 OK: wrote {n1:,} records")

    # --- run 2: identical data must not rewrite outputs --------------------
    before = (json_out.read_bytes(), js_out.read_bytes())
    n2 = ingest.build(json_out, js_out, keys)
    after = (json_out.read_bytes(), js_out.read_bytes())
    assert before == after, "unchanged run rewrote outputs (commit guard broken)"
    assert n2 == n1
    print("run 2 OK: unchanged data left outputs byte-identical")

    # --- run 3: rolling-window source loses records -> archived ------------
    DROP = 10
    ingest.ADAPTERS = stub_adapters(pack1, drop={"fukui": DROP})
    n3 = ingest.build(json_out, js_out, keys)
    pack3 = json.loads(json_out.read_text(encoding="utf-8"))
    archived = [r for r in pack3["records"]
                if r["source_key"] == "fukui" and r.get("archived")]
    fukui_meta = next(s for s in pack3["meta"]["sources"]
                      if s["key"] == "fukui")
    assert n3 == n1, f"records lost: {n1} -> {n3}"
    assert len(archived) == DROP, f"expected {DROP} archived, got {len(archived)}"
    assert fukui_meta["retained_history_count"] == DROP
    assert fukui_meta["record_count"] == \
        sum(1 for r in pack3["records"] if r["source_key"] == "fukui")
    print(f"run 3 OK: {DROP} dropped records retained as archived history")

    # --- run 4: partial run carries other sources through ------------------
    ingest.ADAPTERS = stub_adapters(pack3)
    n4 = ingest.build(json_out, js_out, ["tottori"])
    pack4 = json.loads(json_out.read_text(encoding="utf-8"))
    assert n4 == n3, f"partial run dropped records: {n3} -> {n4}"
    assert {s["key"] for s in pack4["meta"]["sources"]} == set(keys), \
        "partial run lost source metadata"
    print("run 4 OK: partial --sources run carried all other sources through")

    # --- content-key dedupe guards the id-scheme transition ----------------
    renamed = copy.deepcopy(pack4)
    for r in renamed["records"]:
        if r["source_key"] == "yamagata":
            r["id"] = "yamagata-LEGACY-" + r["id"]
    json_out.write_text(ingest.serialize_pack(renamed), encoding="utf-8")
    ingest.ADAPTERS = stub_adapters(pack4)  # new-scheme ids
    n5 = ingest.build(json_out, js_out, keys)
    pack5 = json.loads(json_out.read_text(encoding="utf-8"))
    y_new = sum(1 for r in pack5["records"] if r["source_key"] == "yamagata")
    y_old = sum(1 for r in pack4["records"] if r["source_key"] == "yamagata")
    assert y_new == y_old, f"id-scheme change duplicated yamagata: {y_old} -> {y_new}"
    print("run 5 OK: id-scheme change did not duplicate records")

    shutil.rmtree(WORK)
    print("ALL OFFLINE BUILD TESTS PASSED")


if __name__ == "__main__":
    sys.exit(main())
