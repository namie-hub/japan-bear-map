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
import hashlib
import html as html_lib
import json
import re
import sys
import time
from urllib.parse import urlencode

import pandas as pd
import requests

UA = {"User-Agent": "japan-bear-activity-map/0.3 (open-data aggregation)"}


def clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = str(value).strip()
    return "" if s.lower() == "nan" else s


def jst_iso(value) -> str:
    """Return an explicit ISO-8601 timestamp in Japan Standard Time."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Tokyo")
    else:
        ts = ts.tz_convert("Asia/Tokyo")
    return ts.isoformat()


def content_id(source_key: str, seen: dict, *parts) -> str:
    """Stable record ID derived from the record's own content.

    Survives row reordering across republished files and source-side ID
    recycling (e.g. Fukui's rolling 'Num'). `seen` tracks collisions — two
    otherwise-identical reports — and suffixes them deterministically.
    """
    digest = hashlib.sha1(
        "|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]
    base = f"{source_key}-{digest}"
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n + 1}"


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


def fetch_arcgis_geojson(layer_url: str, page_size: int = 1000) -> list[dict]:
    """Read every public feature from an ArcGIS FeatureServer layer."""
    features, offset = [], 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "orderByFields": "objectid",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "geojson",
        }
        payload = json.loads(download(layer_url + "/query?" + urlencode(params)))
        if payload.get("error"):
            raise RuntimeError(f"ArcGIS query failed: {payload['error']}")
        page = payload.get("features", [])
        features.extend(page)
        if not page or not payload.get("properties", {}).get("exceededTransferLimit"):
            break
        offset += len(page)
    return features


def arcgis_datetime(milliseconds, time_text: str = "") -> str:
    """Convert an ArcGIS epoch-millisecond date to an explicit JST value."""
    ts = pd.to_datetime(milliseconds, unit="ms", utc=True, errors="coerce")
    if pd.isna(ts):
        return ""
    ts = ts.tz_convert("Asia/Tokyo")
    match = re.match(r"^(\d{1,2}):(\d{2})", clean(time_text))
    if match:
        ts = ts.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                        second=0, microsecond=0)
    return ts.isoformat()


def feature_point(feature: dict, bounds: tuple[float, float, float, float]):
    """Return a validated (lat, lon) pair, or None."""
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        return None
    try:
        lon, lat = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    lat_min, lat_max, lon_min, lon_max = bounds
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        return None
    return round(lat, 6), round(lon, 6)


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
        "reported_at": jst_iso(r["reported_at"]),
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
        "reported_at": jst_iso(r["reported_at"]),
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
        iso = jst_iso(ts)
        if time_str and re.match(r"^\d{1,2}:\d{2}", time_str):
            hh, mm = time_str.split(":")[:2]
            try:
                iso = jst_iso(ts.replace(hour=int(hh), minute=int(mm)))
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
    seen_ids: dict = {}
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
            "id": content_id("yamagata", seen_ids, jst_iso(ts),
                             round(float(r["latitude"]), 6),
                             round(float(r["longitude"]), 6),
                             clean(r.get("地名等")), desc),
            "source_key": "yamagata",
            "incident_type": "目撃",
            "municipality": clean(r.get("ユーザ名")),
            "location": clean(r.get("地名等")),
            "reported_at": jst_iso(ts),
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


# --------------------------------------------------------------- Toyama ----

TOYAMA_LAYER = ("https://services7.arcgis.com/pUdPpUsq83Kw8pWi/arcgis/rest/"
                "services/survey123_3f07f1f9864d43368d48b5f373d6cd68_results/"
                "FeatureServer/0")
TOYAMA_PAGE = ("https://www.pref.toyama.jp/1709/kurashi/kankyoushizen/"
               "shizen/yaseiseibutsu/kumap.html")
TOYAMA_MAP = ("https://pref-toyama-1709.maps.arcgis.com/apps/dashboards/"
              "daffbc92f82342339aa6bf3c83ab4742")
TOYAMA_BOX = (36.2, 37.1, 136.6, 138.0)


def fetch_toyama() -> tuple[list[dict], dict]:
    features = fetch_arcgis_geojson(TOYAMA_LAYER)
    records = []
    for feature in features:
        point = feature_point(feature, TOYAMA_BOX)
        p = feature.get("properties") or {}
        reported_at = arcgis_datetime(p.get("HasseiDateTime"))
        if not point or not reported_at:
            continue
        adult = clean(p.get("BearAdult"))
        young = clean(p.get("BearYoung"))
        unknown = clean(p.get("BearUnknown"))
        count_parts = [f"成獣{adult}" if adult else "",
                       f"幼獣{young}" if young else "",
                       f"不明{unknown}" if unknown else ""]
        lat, lon = point
        records.append({
            "id": f"toyama-{clean(p.get('globalid')) or clean(p.get('objectid'))}",
            "source_key": "toyama",
            "incident_type": clean(p.get("HoukokuType")) or "その他",
            "municipality": clean(p.get("HasseiCity")),
            "location": clean(p.get("HasseiArea")),
            "reported_at": reported_at,
            "species": "Asian black bear",
            "sex": "",
            "family_status": "親子" if young else "",
            "count": " / ".join(x for x in count_parts if x),
            "description": clean(p.get("TsuhoInfo")),
            "accuracy": "official public map point",
            "latitude": lat,
            "longitude": lon,
        })
    dates = [r["reported_at"] for r in records]
    return records, {
        "key": "toyama",
        "name": "Toyama Prefecture Kumappu",
        "url": TOYAMA_PAGE,
        "live_map": TOYAMA_MAP,
        "license": "Official public ArcGIS layer; reuse licence not stated",
        "record_count": len(records),
        "raw_row_count": len(features),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": max(dates)[:10] if dates else "",
        "update_cadence": "Near-real-time entries by municipalities",
        "quality": ("Official prefectural public map with incident type, time, "
                    "municipality and bear counts. Public ArcGIS item metadata "
                    "does not state a reuse licence; verify before republication."),
    }


# ---------------------------------------------------------------- Gunma ----

GUNMA_LAYER = ("https://services7.arcgis.com/DkC6f6v0YUQX0rke/arcgis/rest/"
               "services/survey123_a77f33a9b9f649cfada5c7983c67874b_results/"
               "FeatureServer/0")
GUNMA_PAGE = "https://www.pref.gunma.jp/page/7141.html"
GUNMA_MAP = ("https://pref-gunma.maps.arcgis.com/apps/dashboards/"
             "5276d2ebf02a42da8595ed2a51a334c8")
GUNMA_BOX = (35.8, 37.1, 138.2, 139.8)


def fetch_gunma() -> tuple[list[dict], dict]:
    features = fetch_arcgis_geojson(GUNMA_LAYER)
    records = []
    for feature in features:
        point = feature_point(feature, GUNMA_BOX)
        p = feature.get("properties") or {}
        reported_at = arcgis_datetime(p.get("field_18") or p.get("field_7"),
                                      p.get("field_20"))
        if not point or not reported_at:
            continue
        place = clean(p.get("field_11") or p.get("field_14"))
        municipality_match = re.match(r"^(.{1,10}?[市町村])", place)
        municipality = municipality_match.group(1) if municipality_match else ""
        injury = clean(p.get("field_22"))
        incident_type = "人身被害" if injury and injury not in {"なし", "無し", "無"} else "目撃"
        description = " / ".join(x for x in [clean(p.get("field10")), injury] if x)
        lat, lon = point
        records.append({
            "id": f"gunma-{clean(p.get('globalid')) or clean(p.get('objectid'))}",
            "source_key": "gunma",
            "incident_type": incident_type,
            "municipality": municipality,
            "location": place,
            "reported_at": reported_at,
            "species": "Asian black bear",
            "sex": "",
            "family_status": clean(p.get("field_21")),
            "count": clean(p.get("field_8")),
            "description": description,
            "accuracy": "official public map point",
            "latitude": lat,
            "longitude": lon,
        })
    dates = [r["reported_at"] for r in records]
    return records, {
        "key": "gunma",
        "name": "Gunma Prefecture bear occurrence map",
        "url": GUNMA_PAGE,
        "live_map": GUNMA_MAP,
        "license": "Official public ArcGIS layer; reuse licence not stated",
        "record_count": len(records),
        "raw_row_count": len(features),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": max(dates)[:10] if dates else "",
        "update_cadence": "Near-real-time entries by municipalities",
        "quality": ("Official prefectural public map with location, time and "
                    "bear count. Municipality is parsed from the published "
                    "place field. Reuse licence is not stated in item metadata."),
    }


# -------------------------------------------------------------- Niigata ----

NIIGATA_LAYER = ("https://services6.arcgis.com/SKz58fvdFlaEB35q/arcgis/rest/"
                 "services/survey123_08d14b98657b47309b868f49602375c8_results/"
                 "FeatureServer/0")
NIIGATA_PAGE = ("https://www.pref.niigata.lg.jp/site/tyoujyutaisakusienn/"
                "1319666477308.html")
NIIGATA_MAP = ("https://www.arcgis.com/apps/dashboards/"
               "20b4d06fb3b34776959a4e69c7a8511a")
NIIGATA_BOX = (36.6, 38.8, 137.5, 139.95)


def fetch_niigata() -> tuple[list[dict], dict]:
    features = fetch_arcgis_geojson(NIIGATA_LAYER)
    records = []
    for feature in features:
        point = feature_point(feature, NIIGATA_BOX)
        p = feature.get("properties") or {}
        reported_at = arcgis_datetime(p.get("field_20"), p.get("field_21"))
        if not point or not reported_at:
            continue
        reason = clean(p.get("field_19"))
        other_reason = clean(p.get("field_19_other"))
        description = " / ".join(x for x in [clean(p.get("field_9")), reason,
                                                other_reason] if x)
        lat, lon = point
        records.append({
            "id": f"niigata-{clean(p.get('globalid')) or clean(p.get('objectid'))}",
            "source_key": "niigata",
            "incident_type": clean(p.get("field_8")) or "その他",
            "municipality": clean(p.get("field_7")),
            "location": clean(p.get("field_17")),
            "reported_at": reported_at,
            "species": "Asian black bear",
            "sex": "",
            "family_status": "",
            "count": clean(p.get("field_26")),
            "description": description,
            "accuracy": "official public map point",
            "latitude": lat,
            "longitude": lon,
        })
    dates = [r["reported_at"] for r in records]
    return records, {
        "key": "niigata",
        "name": "Niigata Prefecture bear occurrence map",
        "url": NIIGATA_PAGE,
        "live_map": NIIGATA_MAP,
        "license": "Official public ArcGIS layer; reuse licence not stated",
        "record_count": len(records),
        "raw_row_count": len(features),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": max(dates)[:10] if dates else "",
        "update_cadence": "Current fiscal-year map, updated continuously",
        "quality": ("Official prefectural current-fiscal-year layer with type, "
                    "municipality, time, count and narrative. Reuse licence is "
                    "not stated in the ArcGIS item metadata."),
    }


# ---------------------------------------------------------------- Fukui ----

FUKUI_PAGE = "https://tsukinowaguma.pref.fukui.lg.jp/KUMA/Top.aspx"
FUKUI_INFO = ("https://www.pref.fukui.lg.jp/doc/shizen/tixyouzixyuu/"
              "tukinowaguma2.html")
FUKUI_BOX = (35.2, 36.4, 135.3, 137.0)


def fetch_fukui() -> tuple[list[dict], dict]:
    page = download(FUKUI_PAGE).decode("utf-8", errors="replace")
    match = re.search(r'id="HeaderPlace_hdnKumaData"\s+value="(.*?)"\s*/>',
                      page, flags=re.S)
    if not match:
        raise RuntimeError("Could not find the Fukui public map data payload")
    rows = json.loads(html_lib.unescape(match.group(1)))
    records = []
    seen_ids: dict = {}
    for row in rows:
        try:
            lat, lon = float(row.get("LAT")), float(row.get("LON"))
        except (TypeError, ValueError):
            continue
        if not (FUKUI_BOX[0] <= lat <= FUKUI_BOX[1] and
                FUKUI_BOX[2] <= lon <= FUKUI_BOX[3]):
            continue
        date = pd.to_datetime(clean(row.get("HIDUKE")), errors="coerce")
        if pd.isna(date):
            continue
        time_text = clean(row.get("JIKAN"))
        time_match = re.match(r"^(\d{1,2}):(\d{2})", time_text)
        if time_match:
            date = date.replace(hour=int(time_match.group(1)),
                                minute=int(time_match.group(2)))
        adult, young = clean(row.get("KOTAISEI")), clean(row.get("KOTAIYOU"))
        records.append({
            "id": content_id("fukui", seen_ids, jst_iso(date),
                             round(lat, 6), round(lon, 6),
                             clean(row.get("SHUBETU")), clean(row.get("SICHO")),
                             clean(row.get("BASHO"))),
            "source_key": "fukui",
            "incident_type": clean(row.get("SHUBETU")) or "その他",
            "municipality": clean(row.get("SICHO")),
            "location": clean(row.get("BASHO")),
            "reported_at": jst_iso(date),
            "species": "Asian black bear",
            "sex": "",
            "family_status": "親子" if young and young != "0" else "",
            "count": clean(row.get("TOSU")),
            "description": "",
            "accuracy": "official public map point",
            "latitude": round(lat, 6),
            "longitude": round(lon, 6),
        })
    dates = [r["reported_at"] for r in records]
    return records, {
        "key": "fukui",
        "name": "Fukui Prefecture Bear Information",
        "url": FUKUI_INFO,
        "live_map": FUKUI_PAGE,
        "license": "Official public website; reuse licence not stated",
        "record_count": len(records),
        "raw_row_count": len(rows),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "published": max(dates)[:10] if dates else "",
        "update_cadence": "Rolling recent records, updated by municipalities",
        "quality": ("Official prefectural public map payload. The public page "
                    "currently exposes a rolling recent window, not full "
                    "history; reuse licence is not stated."),
    }


ADAPTERS = {
    "akita": fetch_akita, "tokyo": fetch_tokyo,
    "tottori": fetch_tottori, "yamagata": fetch_yamagata,
    "toyama": fetch_toyama, "gunma": fetch_gunma,
    "niigata": fetch_niigata, "fukui": fetch_fukui,
}


# ------------------------------------------------- nationwide coverage -----

# Status of every prefecture, so the map can show coverage honestly.
#   covered  - adapter enabled above
#   possible - machine-readable data identified but adapter not enabled
#              (see note for why)
#   none     - no machine-readable open dataset found as of August 2026
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
    "富山県": {"status": "covered", "source_key": "toyama",
             "note": "Official public ArcGIS layer; reuse licence is not "
                     "stated in the item metadata."},
    "群馬県": {"status": "covered", "source_key": "gunma",
             "note": "Official public ArcGIS layer; reuse licence is not "
                     "stated in the item metadata."},
    "青森県": {"status": "possible",
             "note": "Official くまログあおもり real-time system; public "
                     "submissions require a separate verification tier."},
    "岩手県": {"status": "possible",
             "note": "Bears LINE app only; no public export; police data "
                     "not included. See fetch_iwate notes."},
    "宮城県": {"status": "possible",
             "note": "Annual 目撃等情報マップ published; format unverified."},
    "新潟県": {"status": "covered", "source_key": "niigata",
             "note": "Official current-fiscal-year ArcGIS layer; reuse "
                     "licence is not stated in the item metadata."},
    "福井県": {"status": "covered", "source_key": "fukui",
             "note": "Official public recent-record map payload; rolling "
                     "window and reuse licence not stated."},
    "石川県": {"status": "possible",
             "note": "Official current-year sightings and analysis maps; "
                     "machine export and reuse terms need review."},
    "岐阜県": {"status": "possible",
             "note": "Official GIS map exists, but its terms prohibit "
                     "secondary reuse without permission; link only."},
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

    As of August 2026, Iwate Prefecture has no machine-readable open dataset
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

def load_previous_pack(path: Path) -> dict:
    """Load the last published bundle for per-source failure fallback."""
    if not path.exists():
        return {"meta": {"sources": []}, "records": []}
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(pack.get("records"), list):
            raise ValueError("records is not a list")
        return pack
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Previous bundle unavailable for fallback: {exc}", file=sys.stderr)
        return {"meta": {"sources": []}, "records": []}


def write_pack_atomic(path: Path, text: str) -> None:
    """Replace an output only after its complete new contents are written."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# Sources whose portals only expose a rolling or fiscal/calendar-year window.
# For these, records seen in an earlier run but absent from the latest fetch
# are retained (marked "archived": true) so history survives source resets —
# e.g. Niigata/Tottori emptying out each April, Fukui's rolling window.
ARCHIVE_SOURCES = {"tottori", "yamagata", "niigata", "fukui"}


def merge_with_history(key: str, recs: list[dict],
                       previous_records: list[dict]) -> tuple[list[dict], int]:
    """Append previously-seen records that dropped out of the source window.

    Dedupes both by id and by a content key (time+place+type), so a record
    survives even if the source republishes it under a different id — and so
    an id-scheme change never duplicates records.
    """
    new_ids = {r["id"] for r in recs}
    new_content = {(r["reported_at"], r["latitude"], r["longitude"],
                    r["incident_type"]) for r in recs}
    retained = []
    for r in previous_records:
        if r.get("source_key") != key or r["id"] in new_ids:
            continue
        if (r["reported_at"], r["latitude"], r["longitude"],
                r["incident_type"]) in new_content:
            continue
        kept = dict(r)
        kept["archived"] = True
        retained.append(kept)
    return recs + retained, len(retained)


def comparable(pack: dict) -> dict:
    """The pack minus run-timestamp churn, for change detection.

    generated_at and last_fetch_attempt change on every run even when no
    report changed; comparing without them lets an unchanged build skip
    writing, so the CI 'commit only if changed' guard actually skips.
    """
    meta = {k: v for k, v in pack.get("meta", {}).items()
            if k != "generated_at"}
    meta["sources"] = [
        {k: v for k, v in s.items() if k != "last_fetch_attempt"}
        for s in meta.get("sources", [])]
    return {"meta": meta, "records": pack.get("records", [])}


def serialize_pack(pack: dict) -> str:
    """JSON with one record per line, so git diffs/deltas stay small."""
    meta_json = json.dumps(pack["meta"], ensure_ascii=False,
                           separators=(",", ":"))
    records_json = ",\n".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":"))
        for r in pack["records"])
    return '{"meta":' + meta_json + ',\n"records":[\n' + records_json + '\n]}'


def build(json_out: Path, js_out: Path, sources: list[str]) -> int:
    previous = load_previous_pack(json_out)
    previous_records = previous.get("records", [])
    previous_metas = {
        item.get("key"): item
        for item in previous.get("meta", {}).get("sources", [])
        if item.get("key")
    }
    all_records, source_metas, failures = [], [], []
    for key in sources:
        if key not in ADAPTERS:
            print(f"Unknown source '{key}' (available: {', '.join(ADAPTERS)})",
                  file=sys.stderr)
            continue
        print(f"Fetching {key}…")
        try:
            recs, meta = ADAPTERS[key]()
        except Exception as e:
            fallback = [r for r in previous_records if r.get("source_key") == key]
            previous_meta = previous_metas.get(key)
            failures.append({"source_key": key, "message": str(e)[:240]})
            if not fallback or not previous_meta:
                print(f"  {key} FAILED, no fallback available: {e}", file=sys.stderr)
                continue
            recs = fallback
            meta = dict(previous_meta)
            meta["fetch_status"] = "stale_fallback"
            meta["last_fetch_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            print(f"  {key} FAILED; retaining {len(recs):,} last-known-good records",
                  file=sys.stderr)
        else:
            meta["fetch_status"] = "current"
            meta["last_fetch_attempt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if key in ARCHIVE_SOURCES:
                recs, kept = merge_with_history(key, recs, previous_records)
                meta["retained_history_count"] = kept
                if kept:
                    print(f"  +{kept:,} archived records retained from "
                          f"earlier snapshots")
                meta["record_count"] = len(recs)
                dates = [r["reported_at"] for r in recs]
                meta["date_min"] = min(dates) if dates else None
                meta["date_max"] = max(dates) if dates else None
        print(f"  {len(recs):,} records")
        all_records.extend(recs)
        source_metas.append(meta)

    # A partial run (--sources) must not silently drop the other sources:
    # carry previous records and metadata through for anything not selected.
    fetched_keys = {m.get("key") for m in source_metas}
    for key, previous_meta in previous_metas.items():
        if key in fetched_keys:
            continue
        carried = [r for r in previous_records if r.get("source_key") == key]
        if not carried:
            continue
        all_records.extend(carried)
        source_metas.append(dict(previous_meta))
        print(f"Carrying {key} through unchanged ({len(carried):,} records)")

    if not all_records:
        raise SystemExit("No source produced data; refusing to write empty output.")

    all_records.sort(key=lambda x: (x["reported_at"], x["id"]), reverse=True)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "record_count": len(all_records),
        "date_min": all_records[-1]["reported_at"],
        "date_max": all_records[0]["reported_at"],
        "sources": source_metas,
        "coverage": COVERAGE,
        "universal_links": UNIVERSAL_LINKS,
        "ingestion_failures": failures,
    }
    pack = {"meta": meta, "records": all_records}

    if comparable(pack) == comparable(previous):
        print("No effective data changes; outputs left untouched.")
        return len(all_records)

    write_pack_atomic(json_out, serialize_pack(pack))
    write_pack_atomic(js_out, "window.BEAR_DATA=" + serialize_pack(pack) + ";")
    return len(all_records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="akita_bears.json")
    parser.add_argument("--js", default="bear_data.js")
    parser.add_argument("--sources", default=("akita,tokyo,tottori,yamagata,"
                                              "toyama,gunma,niigata,fukui"),
                        help="comma-separated: " + ",".join(ADAPTERS))
    args = parser.parse_args()

    n = build(Path(args.json), Path(args.js),
              [s.strip() for s in args.sources.split(",") if s.strip()])
    print(f"Wrote {n:,} records to {args.json} and {args.js}")
