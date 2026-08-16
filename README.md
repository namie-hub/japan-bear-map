# Japan Bear Activity Map — universal edition

A self-maintaining, Japan-wide map of reported bear activity built from
official prefectural public data sources. Licence status is disclosed for
every source; public-map sources without a stated reuse licence must be
reviewed before republication. Coverage is always visible and blank never
silently means "no bears."

## Data sources (enabled)

| Source | Records | Coverage | Freshness pattern |
|---|---|---|---|
| Akita Kumadas (CC BY 4.0) | 22,043 | Akita, 2022– | CSV republished ~weekly, trails live system |
| Tokyo TOKYO Kumap (CC BY) | 995 | Tama + 3 Yamanashi border towns, 2023– | Batch file, weeks between updates |
| Tottori bear map (CC BY 4.0) | 113 | Tottori, current fiscal year | GIS platform, ~weekly, freshest source |
| Yamagata Kemonote (official map source data) | 824 | Yamagata, current calendar year | CSV republished ~weekly |
| Toyama Kumappu (licence not stated) | 4,599 | Toyama, 2017– | Official ArcGIS layer, near-real-time |
| Gunma bear map (licence not stated) | 1,707 | Gunma, 2024– | Official ArcGIS layer, near-real-time |
| Niigata bear map (licence not stated) | 4,132 | Niigata, current and prior fiscal year | Official ArcGIS layer |
| Fukui Bear Information (licence not stated) | 235 | Fukui, rolling recent window | Official embedded public-map payload |

## The coverage overlay

Every one of the 47 prefectures is shaded on the map:

- **Green** — covered: its data is on this map (check per-source licence)
- **Amber** — data exists but the adapter isn't enabled (each has a note
  explaining why: unclear licence, unbuilt, no export, etc.)
- **Brown** — special case (Shikoku's ~20–25 critically endangered bears:
  deliberate conservation-driven data scarcity)
- **Gray** — no machine-readable open dataset found (as of August 2026)

Click any prefecture for its status, note, and the reminder that blank ≠ safe.
The panel links an all-prefecture directory of official bear pages (Yahoo!
disaster notebook) and the Ministry of the Environment's nationwide page, so
every prefecture has a checkable official source even without a data layer.

## How it stays universal without asking anyone

1. **Registry, not one-off patches.** `ingest.py` has a `COVERAGE` registry
   for all 47 prefectures and an `ADAPTERS` dict. Adding a prefecture =
   writing one adapter function and flipping its registry entry to
   "covered". Nothing else changes.
2. **Self-refreshing.** `.github/workflows/update-data.yml` runs the
   ingestion daily on GitHub Actions and commits only real changes. When
   nothing changed, `ingest.py` leaves the output files untouched (run
   timestamps are excluded from the comparison), so no empty commits pile
   up. Records are written one per line so git stores small diffs, not
   whole-file rewrites. With GitHub Pages enabled, the public URL always
   serves fresh data.
3. **Failure-isolated.** If a portal fails, the build retains that source's
   last-known-good records and marks it as a stale fallback. A temporary
   outage therefore cannot make a prefecture disappear from the map.
4. **Fragile URLs are discovered, not hard-coded.** Tokyo's and Yamagata's
   CSV filenames are date-stamped, so the adapters locate the current link
   on the official page at each run.
5. **History survives source resets.** Tottori, Yamagata, Niigata and Fukui
   only publish a rolling or fiscal/calendar-year window, which would erase
   history every April (or continuously, for Fukui). Records that drop out
   of a source's window are retained in the bundle, marked `archived` and
   labeled "retained history" in their popups. Record IDs for Yamagata and
   Fukui are content hashes, so retention dedupes reliably even when a
   source republishes or renumbers its rows.
6. **Partial runs are safe.** `python ingest.py --sources tottori` refreshes
   only Tottori and carries every other source through unchanged instead of
   silently dropping them from the bundle.
7. **No runtime CDN.** Leaflet and its plugins are vendored in `vendor/`,
   so the map works on flaky connections; only the basemap tiles need the
   network.

## Run it

Keep `index.html`, `bear_data.js`, `prefectures.js` and the `vendor/`
folder together and open `index.html`. To refresh manually:

```bash
pip install -r requirements.txt
python ingest.py                       # all enabled sources
python ingest.py --sources tottori     # one source
python validate_data.py                # validate before publishing
```

## Adapter candidates (registry status: possible)

- **Yamanashi** — own open-data platform publishes sightings (403 from this
  build environment; likely works elsewhere)
- **Ishikawa** — official current-year and analysis maps; export unverified
- **Aomori** — くまログあおもり; public submissions need a verification tier
- **Miyagi** — annual sighting map (format unverified)
- **Shimane/Tottori news map** — machine-readable and current, but
  news-compiled with unclear reuse licence; adapter deliberately not enabled
- **Iwate** — LINE-app only, no export, police data excluded (see
  `fetch_iwate` notes)

Toyama, Gunma, Niigata and Fukui are enabled because their official public
maps expose structured records. Their source metadata does not state a reuse
licence, so obtain permission or a formal licence assessment before publicly
republishing their copied records.

## Important limitation

This map displays reported activity, not attack probability. Heat shows
reporting density (which also reflects where people are). Category
definitions, verification and precision differ per prefecture — that's why
sources are never merged into fake uniformity. Even the freshest source
lags its prefecture's live system; the panel's official-map links are the
final check before going out.

## Interface safeguards

- The initial view shows Japan rather than zooming only to covered sources.
- Coverage and per-source freshness are displayed before the filters.
- Source-specific municipality choices cannot be combined accidentally.
- Report timestamps are interpreted and displayed in Japan Standard Time.
- Combined heatmaps carry an explicit cross-source comparability warning.
- Incident filters use broad common groups while popups preserve the
  original prefectural terminology.
