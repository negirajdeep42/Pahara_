# PAHARA

**Predictive Analytics for Hazard Assessment and Rapid Alerts**
Landslide early warning and risk monitoring across the eight states of
**Northeast India** — Arunachal Pradesh, Assam, Manipur, Meghalaya, Mizoram,
Nagaland, Sikkim and Tripura.

Smart India Hackathon 2026 · Problem statement **SIH26001**.

---

## The idea in one line

```
risk = susceptibility × trigger
```

**Susceptibility** is static. It is computed offline from terrain by a
gradient-boosted model and shipped as a GeoJSON grid. It does not change with
the weather. The predictors are listed in the file's own `metadata.features`, so
the dashboard renders whatever the grid was actually trained on rather than a
hardcoded list.

**Trigger** is live. It is computed in the browser from rainfall, either fetched
from Open-Meteo or set by an operator running a what-if scenario. Rainfall comes
from a coarse 0.5° lattice — 121 nodes for the current grid, two batched calls —
and each cell reads the node containing it.

A hillside that cannot fail does not fail however hard it rains. Rain that never
comes cannot bring down even the worst slope. Only the product is risk. The
dashboard shows both halves separately and their product on the map, and the
cell inspector spells the multiplication out for whichever cell you click.

---

## Running it

```bash
npm install
npm run dev          # http://localhost:5173
```

```bash
npm run build        # production bundle into dist/
npm run preview      # serve the built bundle locally
npm test             # all four suites below, 12,199 assertions
npm run test:engine  # the risk engine
npm run test:factors # inspector factors, against both real grid files
npm run test:search  # fuzzy matching, geo maths, place resolution
npm run test:render  # components rendered in node, all three locales
```

Node 18+ is required. No backend, no database, no API keys to run.

### Telegram (optional)

Alert dispatch needs two environment variables. Copy `.env.example` to
`.env.local` and fill them in:

```
VITE_TELEGRAM_TOKEN=123456789:AAExampleTokenFromBotFather
VITE_TELEGRAM_CHAT_ID=-1001234567890
```

Without them the app runs exactly as normal and the alerts panel shows a
**Configure Telegram** notice in place of the dispatch button.

> **Security.** `VITE_`-prefixed variables are inlined into the JavaScript bundle
> at build time and are readable by anyone who opens the deployed site. That is
> an accepted trade-off for a no-backend demo with a throwaway bot. A real
> deployment must move the send behind a server or serverless function that
> holds the token.

---

## Deploying to Vercel

1. Push this repository to GitHub.
2. Import it in Vercel. The Vite preset is detected automatically, and
   `vercel.json` pins the build command, output directory and SPA rewrite.
3. Add `VITE_TELEGRAM_TOKEN` and `VITE_TELEGRAM_CHAT_ID` under
   **Settings → Environment Variables** if you want alert dispatch. Redeploy
   after adding them — they are read at build time, not at runtime.

Nothing else is needed: no serverless functions, no database, no secrets beyond
the optional bot credentials.

---

## The data file

The app fetches **`/risk_grid.geojson`** from `public/` on load. It is a
`FeatureCollection` of square polygon cells — currently **5,922 cells at 0.05°,
3.17 MB**, covering the eight surveyed states.

| property | meaning |
| --- | --- |
| `cell_id` | stable identifier, `lat_lon` |
| `lat`, `lon` | cell centre |
| `susceptibility` | 0–1, static |
| `suscept_class` | Very Low … Very High, quantile bins |
| `state`, `district` | from OpenStreetMap admin boundaries |
| `nearest_village`, `population` | nearest OSM settlement; `population` is `null` when untagged |
| `dist_village_km` | distance to that settlement |
| `elevation_m` | metres |
| `slope_deg`, `slope_mean_deg` | max and mean slope within the cell |
| `relief_m` | elevation range over a 3×3 cell window |
| `tpi_m` | topographic position: cell minus its neighbourhood mean |
| `roughness_m` | elevation spread within the cell |
| `curvature` | laplacian; positive is a convex ridge, negative a concave hollow |
| `aspect_sin`, `aspect_cos` | aspect encoded so 359° and 1° are adjacent |

A top-level `metadata` object (31 keys) feeds the methodology modal: `method`,
`dem_source`, `auc_spatial_cv`, `auc_random_cv`, `auc_logreg_baseline`,
`cell_count`, `landslide_records`, `landslide_date_range`, `features`,
`feature_importance`, `class_distribution`, `data_sources` and `caveats`.

Nothing in the UI assumes a property is present. A grid missing a field shows
*"Not present in this grid file"*; a grid with no `metadata` falls back to
whatever known predictors its cells carry. The previous district grid is kept at
`public/risk_grid_dimahasao.geojson` and still renders correctly — both files
are exercised by `npm run test:factors`.

### Regenerating the grid

```bash
python scripts/run_all.py
cp out/risk_grid_ne.geojson public/risk_grid.geojson
```

That runs four stages, each cached and resumable:

| Stage | What it does |
| --- | --- |
| `fetch_dem.py` | SRTM 30 m elevation on a 0.025° lattice, masked to the eight surveyed states (407 requests instead of 1,258 for the full rectangle) |
| `fetch_osm.py` | OSM state/district boundaries and settlements via Overpass |
| `build_grid.py` | 2×2 aggregation to 0.05° cells and terrain derivatives |
| `train_susceptibility.py` | Labelling, negative sampling, validation, export to `out/` |

`scripts/inspect_data.py` prints the shape of every source CSV without touching
the model. `SIH_DEM_SOURCE=open-meteo` switches the elevation provider.

Alongside the grid, the pipeline writes `out/places.json` (5,196 settlements
with the **maximum** susceptibility of the cells near each) and
`out/metrics.json` (the metadata block alone, for slides).

**How well does it work?** Spatial-block CV ROC-AUC **0.6244** — the honest
figure — against 0.6298 from a random split, which is inflated by spatial
autocorrelation between neighbouring cells. A logistic baseline under the same
spatial CV scores 0.5893. More usefully: **82.6% of recorded landslides fall in
the High + Very High classes, which cover 40.2% of the area — 2.06× better than
flagging land at random.** See `CHANGELOG.md` for how the negatives were sampled
and why that choice decided the sign of the model.

---

## What is on screen

| Panel | Position | What it does |
| --- | --- | --- |
| **Location search** | top left, and the header on phones | Fuzzy search over 5,196 settlements. Type a village or town, get its live risk, static susceptibility, terrain and rainfall breakdown, and the nearest higher-risk zone within 10 km. Handles romanisation variants; falls back to a geocoder for anything not in the file. |
| **Map** | full screen | MapLibre GL + OpenFreeMap Liberty basemap, dimmed so hazard colour is the brightest thing on screen. Two colour modes: static susceptibility (5 classes) and live risk (4 classes). Hover highlights, click selects. |
| **Rainfall & trigger** | top left | Two sliders (24 h, 0–250 mm · 7 d, 0–500 mm), a live-fetch button, a LIVE/SIMULATED badge, and the trigger broken into its weighted components. |
| **Priority zones** | left, below | The ten highest-risk cells right now. Clicking one flies the map there and opens the inspector. |
| **Cell inspector** | right drawer | The rating, the multiplication that produced it, and every contributing factor as a bar with its real measured value. |
| **Alerts** | top right | Live count of zones at Warning or Evacuate, one dispatch button, and a session log of what was sent. |
| **Simulated sensor node** | bottom left | Three drifting gauges — soil moisture, tilt, battery — that scale with the rainfall inputs. Border pulses red above 80 % moisture. |
| **Report a hazard** | bottom centre | Citizen report: photo, GPS or manual coordinates, type, description. Drops a violet marker on the map. |
| **Methodology** | header | Grid provenance from the file's own metadata, the formula, and the sensor disclosure. |

Language switches between **English / हिन्दी / অসমীয়া** from the header; the
choice is remembered. The connectivity chip turns amber when the browser goes
offline, and the app shell, the risk grid and any basemap tiles already seen
stay available.

> **The sensor panel is a simulation.** Values are generated in the browser and
> scale with the rainfall inputs. Field hardware is in development. The panel
> header, the panel body and the methodology modal all say so — please keep it
> that way.

---

## Free services used

| Service | Used for | Key needed |
| --- | --- | --- |
| [Open-Meteo](https://open-meteo.com) | live 24 h / 168 h rainfall | none |
| [Photon](https://photon.komoot.io) | geocoder fallback for unindexed names | none |
| [OpenFreeMap](https://openfreemap.org) (Liberty) | basemap tiles | none |
| [Telegram Bot API](https://core.telegram.org/bots/api) | alert dispatch | bot token, optional |

---

## Layout

```
src/
  App.jsx                  state, layout, panel wiring
  components/
    Header.jsx             identity, layer switch, connectivity, language
    MapView.jsx            MapLibre lifecycle, layers, hover/click, popups
    Legend.jsx             switches with the colour mode
    RainfallPanel.jsx      sliders, live fetch, trigger breakdown
    PriorityList.jsx       top ten by live risk
    CellInspector.jsx      per-cell breakdown drawer
    AlertsPanel.jsx        counts, dispatch, session log
    ReportModal.jsx        citizen hazard report
    SensorPanel.jsx        simulated node gauges
    MethodologyModal.jsx   provenance and disclosure
    PlaceSearch.jsx        fuzzy settlement search + dropdown
    PlaceResultCard.jsx    the searched location's assessment
    Panel.jsx  Toasts.jsx  shared shells
  lib/
    riskEngine.js          computeTrigger, computeRisk, riskClass, ranking
    riskEngine.test.mjs    assertions (npm run test:engine)
    colors.js              class palettes + MapLibre paint expressions
    factors.js             inspector bars, driven by metadata.features
    factors.test.mjs       assertions (npm run test:factors)
    geo.js                 haversine, bearing, nearest-cell search
    placeSearch.js         Fuse config, place resolution, geocoder fallback
    placeSearch.test.mjs   assertions (npm run test:search)
    render.test.jsx        component render checks (npm run test:render)
    spatialRisk.js         applies the rainfall field to the grid, per cell
    openMeteo.js           single-point rainfall + the 0.5 deg lattice fetch
    telegram.js            message builder + dispatch
  hooks/
    useRiskGrid.js         loads and normalises the grid
    usePlaces.js           loads places.json once
  i18n/                    i18next setup, en / hi / as
public/
  places.json                  5,196 settlements, for the search
  risk_grid.geojson            the Northeast grid, 5,922 cells
  risk_grid_dimahasao.geojson  the previous district grid, kept
  icon.svg                     app + PWA icon
scripts/                   offline pipeline (see "Regenerating the grid")
out/                       pipeline output: grid, places.json, metrics.json
```

### Two implementation notes worth knowing

**There are two recolouring paths, and the fast one is still the default.**
Operator what-if scenarios are one scalar for the whole region, so the live-risk
fill stays a style expression — `['step', ['*', ['get','susceptibility'],
trigger], …]` — and a slider move is one `setPaintProperty` call. No source
reload, no React re-render of 5,922 polygons.

Live rainfall varies across the map, so it cannot collapse to a scalar. Per-cell
risk is written into `feature-state` once per fetch, and the expression reads
`['coalesce', ['feature-state','risk'], …the scalar form…]`. Feature-state is
only ever walked on a fetch, never on a drag. This is why the source uses
`promoteId: 'cell_id'` rather than `generateId` — the risk for a cell has to be
addressable by name from outside `MapView`.

**The basemap veil is a fill, not a background layer.** MapLibre always paints
`background` layers in the background pass regardless of their position in the
layer order, so dimming the basemap needs a `fill` layer over a regional
polygon. A polygon spanning the full −180…180 range does not draw at all, hence
the regional box.

---

## Translations

Hindi and Assamese strings were written for this dashboard and cover all 151
keys, verified against the English file. They would benefit from a native
speaker's review before any real deployment — particularly the Assamese
technical vocabulary (*ট্ৰিগাৰ*, *সংবেদনশীলতা*).
