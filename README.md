# Japan Bear Activity Map — universal edition

A self-maintaining, Japan-wide map of reported bear activity built from
official prefecture open data. Designed so coverage is always visible and
honest: every prefecture is shaded by data status, and blank never silently
means "no bears."

## Data sources (enabled)

| Source | Records | Coverage | Freshness pattern |
|---|---|---|---|
| Akita Kumadas (CC BY 4.0) | ~22,000 | Akita, 2022– | CSV republished ~weekly, trails live system ~1 week |
| Tokyo TOKYO Kumap (CC BY) | ~1,000 | Tama + 3 Yamanashi border towns, 2023– | Batch file, weeks between updates |
| Tottori bear map (CC BY 4.0) | ~70 | Tottori, current fiscal year | GIS platform, ~weekly, freshest source |
| Yamagata Kemonote (official map source data) | ~630 | Yamagata, current calendar year | CSV republished ~weekly |

## The coverage overlay

Every one of the 47 prefectures is shaded on the map:

- **Green** — covered: its data is on this map
- **Amber** — data exists but the adapter isn't enabled (each has a note
  explaining why: unclear licence, unbuilt, no export, etc.)
- **Brown** — special case (Shikoku's ~20–25 critically endangered bears:
  deliberate conservation-driven data scarcity)
- **Gray** — no machine-readable open dataset found (as of July 2026)

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
   ingestion daily on GitHub Actions and commits only real changes. With
   GitHub Pages enabled, the public URL always serves fresh data. Bookmark
   it; never rebuild anything by hand.
3. **Failure-isolated.** A broken portal skips that source with a warning;
   the other prefectures keep updating.
4. **Fragile URLs are discovered, not hard-coded.** Tokyo's and Yamagata's
   CSV filenames are date-stamped, so the adapters locate the current link
   on the official page at each run.

## Run it

Keep `index.html`, `bear_data.js`, and `prefectures.js` together and open
`index.html`. To refresh manually:

```bash
pip install -r requirements.txt
python ingest.py                       # all enabled sources
python ingest.py --sources tottori     # one source
```

## Adapter candidates (registry status: possible)

- **Yamanashi** — own open-data platform publishes sightings (403 from this
  build environment; likely works elsewhere)
- **Toyama** — official クマっぷ via Google My Maps (KML fetchable)
- **Gunma** — official ArcGIS dashboard (FeatureServer likely queryable)
- **Aomori** — くまログあおもり (export unverified)
- **Miyagi** — annual sighting map (format unverified)
- **Shimane/Tottori news map** — machine-readable and current, but
  news-compiled with unclear reuse licence; adapter deliberately not enabled
- **Iwate** — LINE-app only, no export, police data excluded (see
  `fetch_iwate` notes)

## Important limitation

This map displays reported activity, not attack probability. Heat shows
reporting density (which also reflects where people are). Category
definitions, verification and precision differ per prefecture — that's why
sources are never merged into fake uniformity. Even the freshest source
lags its prefecture's live system; the panel's official-map links are the
final check before going out.
