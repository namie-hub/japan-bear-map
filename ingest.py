#!/usr/bin/env python3
"""Multi-source ingestion for the Japan Bear Activity Map.

Each prefecture is an adapter that returns normalized records. Sources are
deliberately kept distinct (source_key, category strings, accuracy labels)
because prefectures define and collect incidents differently — merging their
categories would create false equivalence.

Outputs:
  akita_bears.json  (kept name for compatibility) - {"meta": ..., "records": [...]}
  bear_data.js      - same payload as window.BEAR_DATA, loaded by index.html

Usage:
  python ingest.py                     # all available sources
  python ingest.py --sources akita     # one source only
"""

from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import re
import sys
import time

import pandas as pd
import requests

UA = {"User-Agent": "japan-bear-activity-map/0.3 (open-data aggregation)"}


def clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def download(url: str, retries: int = 3, backoff: float = 5.0) -> bytes:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=120, headers=UA)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            last_err = e
            print(f"  attempt {attempt}/{retries} failed: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Could not download {url}: {last_err}")


# ---------------------------------------------------------------- Akita ----

AKITA_CSV = (
    "https://ckan.pref.akita.lg.jp/dataset/"
    "f801a10f-f076-47e4-b5a6-0bb5569639e0/resource/"
    "0678f9b3-4bf7-4212-9c0e-c0cb9b09b3cf/download/050008_kumadas.csv"
)
AKITA_PAGE = "https://ckan.pref.akita.lg.jp/dataset/050008_shizenhogoka_003"
AKITA_RESOURCE_API = ("https://ckan.pref.akita.lg.jp/api/3/action/"
                      "resource_show?id=0678f9b3-4bf7-4212-9c0e-c0cb9b09b3cf")
AKITA_BOX = (38.5, 41.0, 139.3, 141.3)  # lat_min, lat_max, lon_min, lon_max


def _akita_published_date() -> str:
    """When the prefecture last republished the CSV on its portal."""
    try:
        info = json.loads(download(AKITA_RESOURCE_API, retries=1))
        return (info.get("result", {}).get("last_modified") or "")[:10]
    except Exception:
        return ""


def fetch_akita() -> tuple[list[dict], dict]:
    raw = download(AKITA_CSV)
    df = pd.read_csv(pd.io.common.BytesIO(raw), encoding="utf-8-sig")
    raw_rows = len(df)

    df = df[df["獣種"].eq("ツキノワグマ")].copy()
    df["reported_at"] = pd.to_datetime(df["目撃日時"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["x(緯度)"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["y(経度)"], errors="coerce")
    df = df.dropna(subset=["reported_at", "latitude", "longitude"])

    la, lb, lo, lp = AKITA_BOX
    df = df[df["latitude"].between(la, lb) & df["longitude"].between(lo, lp)]
    df = df.sort_values("reported_at").drop_duplicates(subset="出没情報ID", keep="last")

    records = [{
        "id": f"akita-{clean(r['出没情報ID'])}",
        "source_key": "akita",
        "incident_type": clean(r["情報種別"]),
        "municipality": clean(r["市町村"]),
        "location": clean(r["地番情報"]),
        "reported_at": r["reported_at"].isoformat(),
        "species": "Asian black bear",
        "sex": clean(r["性別"]),
        "family_status": clean(r["単独か親子"]),
        "count": clean(r["頭数"]),
        "description": clean(r["目撃時の状況"]),
        "accuracy": "",
        "latitude": round(float(r["latitude"]), 6),
        "longitude": round(float(r["longitude"]), 6),
    } for _, r in df.iterrows()]

    source_meta = {
        "key": "akita",
        "name": "Akita Prefecture Kumadas",
        "url": AKITA_PAGE,
        "live_map": "https://kumadas.net/",
        "license": "CC BY 4.0",
        "record_count": len(records),
        "raw_row_count": raw_rows,
        "date_min": min(r["reported_at"] for r in records) if records else None,
        "date_max": max(r["reported_at"] for r in records) if records else None,
        "published": _akita_published_date(),
        "update_cadence": "Near-real-time (fed continuously by municipalities and police)",
        "quality": ("Official prefecture system fed by municipalities and "
                    "police; detailed per-incident fields."),
    }
    return records, source_meta


# ---------------------------------------------------------------- Tokyo ----

TOKYO_DATA_PAGE = "https://www.kankyo.metro.tokyo.lg.jp/nature/animals_plants/bear/data"
TOKYO_CATALOG = "https://catalog.data.metro.tokyo.lg.jp/dataset/t000009d2000000060"
TOKYO_BOX = (35.3, 36.1, 138.7, 139.6)


def _find_tokyo_csv_url() -> str:
    """The CSV filename is date-stamped (tukinowaguma_sourceYYYYMMDD), so we
    locate the current link on the official download page each run."""
    html = download(TOKYO_DATA_PAGE).decode("utf-8", errors="replace")
    m = re.findall(r'href="(/documents/d/kankyo/tukinowaguma_source[^"]*)"', html)
    if not m:
        raise RuntimeError(
            "Could not find the Tokyo CSV link on the download page; "
            "the page layout may have changed: " + TOKYO_DATA_PAGE)
    return "https://www.kankyo.metro.tokyo.lg.jp" + m[0]


def fetch_tokyo() -> tuple[list[dict], dict]:
    csv_url = _find_tokyo_csv_url()
    m = re.search(r"tukinowaguma_source(\d{4})(\d{2})(\d{2})", csv_url)
    published = "-".join(m.groups()) if m else ""
    raw = download(csv_url)
    df = pd.read_csv(pd.io.common.BytesIO(raw), encoding="utf-8-sig")
    raw_rows = len(df)

    type_col = next(c for c in df.columns if "sightings" in c.lower())
    df["reported_at"] = pd.to_datetime(df["date"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["lat"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["reported_at", "latitude", "longitude"])

    la, lb, lo, lp = TOKYO_BOX
    df = df[df["latitude"].between(la, lb) & df["longitude"].between(lo, lp)]
    df = df.drop_duplicates(subset="number", keep="last")
    df["number"] = pd.to_numeric(df["number"], errors="coerce").astype("Int64")

    records = [{
        "id": f"tokyo-{clean(r['number'])}",
        "source_key": "tokyo",
        "incident_type": clean(r[type_col]),
        "municipality": "",          # not provided by this source
        "location": "",
        "reported_at": r["reported_at"].isoformat(),
        "species": "Asian black bear (unconfirmed reports may include lookalikes)",
        "sex": "",
        "family_status": "",
        "count": "",
        "description": "",
        "accuracy": clean(r["accuracy"]),   # high / middle / low, as published
        "latitude": round(float(r["latitude"]), 6),
        "longitude": round(float(r["longitude"]), 6),
    } for _, r in df.iterrows()]

    source_meta = {
        "key": "tokyo",
        "name": "Tokyo Metropolitan Government — TOKYO Kumap",
        "url": TOKYO_CATALOG,
        "live_map": "https://www.kankyo.metro.tokyo.lg.jp/nature/animals_plants/bear/witness",
        "license": "CC BY",
        "record_count": len(records),
        "raw_row_count": raw_rows,
        "date_min": min(r["reported_at"] for r in records) if records else None,
        "date_max": max(r["reported_at"] for r in records) if records else None,
        "published": published,
        "update_cadence": ("Batch file republished periodically "
                           "(date-stamped download; typically weeks between "
                           "updates)"),
        "quality": ("Official Environment Bureau dataset with per-record "
                    "accuracy labels (high/middle/low); positions may be "
                    "shifted to avoid overlaps; may include bear lookalikes "
                    "(e.g. serow); covers Tama area plus three adjacent "
                    "Yamanashi municipalities since Dec 2025. No municipality "
                    "names or free-text descriptions."),
    }
    return records, source_meta


# --------------------------------------------------------------- Tottori ---

# Tottori serves its official bear map through the tottori-geomap (Geolonia)
# platform. The current fiscal year is exposed as a stable GeoJSON; the URL
# below is the R8 (2026) "choujutsuhou" layer wired into the official viewer.
TOTTORI_GEOJSON = "https://tiles.tottori-geomap.jp/geojson/choujutsuhou.geojson"
TOTTORI_PAGE = "https://www.pref.tottori.lg.jp/item/1143816.htm"
TOTTORI_BOX = (35.0, 35.7, 133.1, 134.5)


def _tottori_type(note: str) -> str:
    """Map the free-text 備考 field to a clean category (original kept in desc)."""
    n = note or ""
    if "糞" in n or "足跡" in n or "皮剥" in n or "痕跡" in n:
        trace = True
    else:
        trace = False
    sighting = "目撃" in n
    if sighting and trace:
        return "目撃・痕跡"
    if trace:
        return "痕跡"
    if sighting:
        return "目撃"
    return "その他"


def fetch_tottori() -> tuple[list[dict], dict]:
    data = json.loads(download(TOTTORI_GEOJSON))
    feats = data.get("features", [])
    raw_rows = len(feats)

    records = []
    la, lb, lo, lp = TOTTORI_BOX
    for i, ft in enumerate(feats):
        try:
            lon, lat = ft["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        if not (la <= lat <= lb and lo <= lon <= lp):
            continue
        p = ft.get("properties", {})
        date = clean(p.get("日にち"))[:10].replace("/", "-")
        ts = pd.to_datetime(date, errors="coerce")
        if pd.isna(ts):
            continue
        time_str = clean(p.get("時間"))
        note = clean(p.get("備考"))
        loc = clean(p.get("場所"))
        mm_ = re.search(r"^(?:鳥取県)?(?:[^郡]{1,4}郡)?(.{1,6}?[市町村])", loc)
        muni = mm_.group(1) if mm_ else ""
        # Combine date + time into an ISO timestamp where a time is given
        iso = ts.isoformat()
        if time_str and re.match(r"^\d{1,2}:\d{2}", time_str):
            hh, mm = time_str.split(":")[:2]
            try:
                iso = ts.replace(hour=int(hh), minute=int(mm)).isoformat()
            except ValueError:
                pass
        records.append({
            "id": f"tottori-{clean(ft.get('id')) or i}",
            "source_key": "tottori",
            "incident_type": _tottori_type(note),
            "municipality": muni,          # extracted from the place text
            "location": loc,
            "reported_at": iso,
            "species": "Asian black bear (reports may include lookalikes)",
            "sex": "", "family_status": "", "count": "",
            "description": note,
            "accuracy": "",
            "latitude": round(float(lat), 6),
            "longitude": round(float(lon), 6),
        })

    dates = [r["reported_at"] for r in records]
    source_meta = {
        "key": "tottori",
        "name": "Tottori Prefecture bear sighting map",
        "url": TOTTORI_PAGE,
        "live_map": "https://www.pref.tottori.lg.jp/280334.htm",
        "license": "CC BY 4.0",
        "record_count": len(records),
        "raw_row_count": raw_rows,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": max(dates)[:10] if dates else "",
        "update_cadence": "Updated through the official Tottori GIS platform (roughly weekly)",
        "quality": ("Official prefecture map (current fiscal year only). "
                    "Categories derived from a free-text note field; "
                    "'place' holds a text location, not a municipality code; "
                    "reports may include bear lookalikes."),
    }
    return records, source_meta


# -------------------------------------------------------------- Yamagata ---

YAMAGATA_PAGE = ("https://www.pref.yamagata.jp/050011/kurashi/shizen/"
                 "seibutsu/about_kuma/kuma_yamagata_top.html")
YAMAGATA_BOX = (37.7, 39.2, 139.4, 140.7)


def _find_yamagata_csv() -> tuple[str, str]:
    """The CSV filename is date-stamped; locate the current link on the
    official page. Returns (url, published_date)."""
    html = download(YAMAGATA_PAGE).decode("utf-8", errors="replace")
    m = re.search(r'href="(/documents/\d+/(\d{8})[^"]*\.csv)"', html)
    if not m:
        raise RuntimeError("Could not find the Yamagata CSV link on " + YAMAGATA_PAGE)
    d = m.group(2)
    return ("https://www.pref.yamagata.jp" + m.group(1),
            f"{d[:4]}-{d[4:6]}-{d[6:]}")


def fetch_yamagata() -> tuple[list[dict], dict]:
    csv_url, published = _find_yamagata_csv()
    raw = download(csv_url)
    df = pd.read_csv(pd.io.common.BytesIO(raw), encoding="utf-8-sig")
    raw_rows = len(df)

    df["reported_at"] = pd.to_datetime(df["目撃した日付"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["緯度"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["経度"], errors="coerce")
    df = df.dropna(subset=["reported_at", "latitude", "longitude"])
    la, lb, lo, lp = YAMAGATA_BOX
    df = df[df["latitude"].between(la, lb) & df["longitude"].between(lo, lp)]

    records = []
    for i, r in df.iterrows():
        ts = r["reported_at"]
        t = clean(r.get("目撃した時間帯（0:00～24:00）"))
        if re.match(r"^\d{1,2}:\d{2}", t):
            hh, mm = t.split(":")[:2]
            try:
                ts = ts.replace(hour=int(hh), minute=int(mm))
            except ValueError:
                pass
        urban = clean(r.get("市街地（半径200m以内に人家が10軒以上）かどうか"))
        desc = " / ".join(x for x in [
            clean(r.get("周辺環境")),
            clean(r.get("個体の大きさ等")),
            ("市街地" if urban == "市街地" else ""),
            clean(r.get("備考"))] if x)
        records.append({
            "id": f"yamagata-{i}",
            "source_key": "yamagata",
            "incident_type": "目撃",
            "municipality": clean(r.get("ユーザ名")),
            "location": clean(r.get("地名等")),
            "reported_at": ts.isoformat(),
            "species": "Asian black bear",
            "sex": "", "family_status": "",
            "count": clean(r.get("目撃頭数")),
            "description": desc,
            "accuracy": "",
            "latitude": round(float(r["latitude"]), 6),
            "longitude": round(float(r["longitude"]), 6),
        })

    dates = [r["reported_at"] for r in records]
    source_meta = {
        "key": "yamagata",
        "name": "Yamagata Prefecture bear sighting map (Kemonote)",
        "url": YAMAGATA_PAGE,
        "live_map": YAMAGATA_PAGE,
        "license": "Published by Yamagata Prefecture as map source data",
        "record_count": len(records),
        "raw_row_count": raw_rows,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": published,
        "update_cadence": "CSV republished on the official page (roughly weekly)",
        "quality": ("Official prefecture map source data; sightings only "
                    "(current calendar year); includes urban-area flag, "
                    "count, and size; not exhaustive per the prefecture's "
                    "own disclaimer."),
    }
    return records, source_meta


ADAPTERS = {"akita": fetch_akita, "tokyo": fetch_tokyo,
            "tottori": fetch_tottori, "yamagata": fetch_yamagata}


# ------------------------------------------------- nationwide coverage -----

# Status of every prefecture, so the map can show coverage honestly.
#   covered  - adapter enabled above
#   possible - machine-readable data identified but adapter not enabled
#              (see note for why)
#   none     - no machine-readable open dataset found as of July 2026
# Prefectures not listed default to "none". Names must match the boundary
# file (prefectures.js) name field.
COVERAGE = {
    "北海道": {"status": "possible",
             "note": "IMPORTANT: Hokkaido has BROWN bears (higuma) — Japan's "
                     "largest and most dangerous bear, across the whole "
                     "island. No prefecture-wide dataset exists: the "
                     "prefecture only maintains a link list of municipal "
                     "pages and warns coverage is incomplete. Sapporo "
                     "publishes per-year sighting CSVs (open data, adapter "
                     "candidate); some towns share the Higumap system. "
                     "Gray/amber here means fragmented data — bear risk in "
                     "Hokkaido is HIGH everywhere outdoors."},
    "秋田県": {"status": "covered", "source_key": "akita"},
    "東京都": {"status": "covered", "source_key": "tokyo"},
    "鳥取県": {"status": "covered", "source_key": "tottori"},
    "山形県": {"status": "covered", "source_key": "yamagata"},
    "山梨県": {"status": "partial",
             "note": "Only 3 municipalities adjacent to Tokyo (Uenohara, "
                     "Kosuge, Tabayama) are included, via the Tokyo source. "
                     "Yamanashi's own open-data platform also publishes bear "
                     "sightings (adapter candidate)."},
    "島根県": {"status": "possible",
             "note": "A frequently-updated Shimane+Tottori map exists but is "
                     "news-compiled (San'in press) with unclear reuse "
                     "licence; adapter written but disabled."},
    "富山県": {"status": "possible",
             "note": "Official クマっぷ published via Google My Maps (KML "
                     "fetchable); adapter candidate."},
    "群馬県": {"status": "possible",
             "note": "Official ArcGIS dashboard; FeatureServer endpoint "
                     "likely queryable; adapter candidate."},
    "青森県": {"status": "possible",
             "note": "くまログあおもり system; export availability unverified."},
    "岩手県": {"status": "possible",
             "note": "Bears LINE app only; no public export; police data "
                     "not included. See fetch_iwate notes."},
    "宮城県": {"status": "possible",
             "note": "Annual 目撃等情報マップ published; format unverified."},
    "徳島県": {"status": "info",
             "note": "Shikoku's ~20-25 critically endangered bears (Tsurugi "
                     "range). Expert-confirmed sightings list only, no "
                     "dataset - by design, given conservation status."},
    "高知県": {"status": "info",
             "note": "Shares the Tsurugi-range population with Tokushima; "
                     "sightings announced via Forest Office notices."},
}

UNIVERSAL_LINKS = [
    {"name": "Hokkaido - municipal brown-bear (higuma) info directory",
     "url": "https://www.pref.hokkaido.lg.jp/ks/skn/higuma/joho.html"},
    {"name": "All-prefecture official bear pages (Yahoo! disaster notebook)",
     "url": "https://emg.yahoo.co.jp/notebook/contents/article/bearsummary251114.html"},
    {"name": "Ministry of the Environment - nationwide bear information",
     "url": "https://www.env.go.jp/nature/choju/effort/effort12/effort12.html"},
]


# ---------------------------------------------------------------- Iwate ----

def fetch_iwate() -> tuple[list[dict], dict]:
    """Not implemented — documented for future work.

    As of July 2026, Iwate Prefecture has no machine-readable open dataset
    comparable to Akita's Kumadas or Tokyo's catalog CSV. Official sighting
    reports flow through "Bears", a crowd-reporting app embedded in the
    prefecture's LINE account, with no public download or API; police-held
    incident data is not reflected in it. The prefecture site publishes
    human-injury cases only, as a Google My Map. Options, in order of
    preference, if/when this adapter is built:
      1. An official export, if the prefecture ever publishes one (watch
         https://www.pref.iwate.jp/kurashikankyou/shizen/yasei/1049881/).
      2. The Google My Maps KML of human-injury cases (very small, injuries
         only, licence unclear — would need a 'partial coverage' label).
      3. Crowd-posted CC BY 4.0 aggregators — usable licence but unofficial;
         would require a clearly separate quality tier in the UI.
    """
    raise NotImplementedError(fetch_iwate.__doc__)


# ----------------------------------------------------------------- main ----

def build(json_out: Path, js_out: Path, sources: list[str]) -> int:
    all_records, source_metas = [], []
    for key in sources:
        if key not in ADAPTERS:
            print(f"Unknown source '{key}' (available: {', '.join(ADAPTERS)})",
                  file=sys.stderr)
            continue
        print(f"Fetching {key}…")
        try:
            recs, meta = ADAPTERS[key]()
        except Exception as e:
            print(f"  {key} FAILED, skipping this source: {e}", file=sys.stderr)
            continue
        print(f"  {len(recs):,} records")
        all_records.extend(recs)
        source_metas.append(meta)

    if not all_records:
        raise SystemExit("No source produced data; refusing to write empty output.")

    all_records.sort(key=lambda x: x["reported_at"], reverse=True)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(all_records),
        "date_min": all_records[-1]["reported_at"],
        "date_max": all_records[0]["reported_at"],
        "sources": source_metas,
        "coverage": COVERAGE,
        "universal_links": UNIVERSAL_LINKS,
    }
    pack = {"meta": meta, "records": all_records}

    json_out.write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    js_out.write_text(
        "window.BEAR_DATA=" +
        json.dumps(pack, ensure_ascii=False, separators=(",", ":")) + ";",
        encoding="utf-8")
    return len(all_records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="akita_bears.json")
    parser.add_argument("--js", default="bear_data.js")
    parser.add_argument("--sources", default="akita,tokyo,tottori,yamagata",
                        help="comma-separated: " + ",".join(ADAPTERS))
    args = parser.parse_args()

    n = build(Path(args.json), Path(args.js),
              [s.strip() for s in args.sources.split(",") if s.strip()])
    print(f"Wrote {n:,} records to {args.json} and {args.js}")
