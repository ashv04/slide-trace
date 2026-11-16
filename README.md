# SlideTrace data backend

SlideTrace serves precomputed data from `public/data/`. The frontend can copy that folder into its own `public/` directory and fetch `/data/sites.json`. The browser only needs the exported files. The Python environment and the original NISAR downloads are needed when rebuilding a comparison.

## Frontend integration

| Resource | Contract |
| --- | --- |
| `/data/sites.json` | Version 1 catalog. Each site has an ID, name, region, approximate map center as `[longitude, latitude]`, status, and bundle URL. The ten glacier regions are accompanied by one synthetic fixture. |
| `hubbard/site.json` | Real geographic comparison. Includes dated image URLs, display bounds, patch count, processing parameters, provenance, and reference layer URLs. |
| `patches.geojson` | WGS84 polygons with stable feature IDs for the same inputs and settings. Each feature has rank, area in m², signed median change in dB, and a short measurement-based explanation. |
| `reference-points.geojson` and `reference-boundary.geojson` | Preliminary USGS inventory points with their original attributes and the boundary of the area the USGS assessed. |

- Resolve the bundle URL relative to the catalog URL. Resolve all URLs within a bundle relative to that bundle URL.
- Hubbard has status `analysis-ready`. The other nine regions have status `awaiting-usable-imagery` and a null bundle URL. Keep the `fixture` entry out of the public glacier picker. Its bundle sets `isFixture: true`.
- Bundle bounds are `[west, south, east, north]` in degrees. Both PNGs use the same bounds and Web Mercator projection. Leaflet takes corners in `[latitude, longitude]` order.
- Sort patches by ascending `rank`. Filter by `Math.abs(medianDeltaDb)`. Changing the slider filters the exported patch list. The bundle records the original pixel threshold in `processing.thresholdDb`.
- Each Hubbard patch has `reference.surveyCoverage` (`inside`, `partial`, or `outside`) and `reference.nearbyPoint` (an object or null). A point link contains the point feature ID, the shortest distance to the patch footprint in meters, and the original interpretation code. Use `sources.reference.interpretationLabels` to display the code. A null point link means the search found no inventory point within `reference.searchRadiusM`.
- Keep the review list and CSV export in the browser. Use the site and patch IDs to identify saved entries. Rebuilds with different source scenes, study bounds, or detection settings can change patch geometries and IDs.

## Rebuild and validate

Use Python 3.14 on macOS or Linux. The two main science HDF5 files are listed in [the Hubbard source manifest](inputs/hubbard-sources.json). Keep their original filenames. The two USGS archives are included under `inputs/`.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_bundle.py --input ~/Downloads
.venv/bin/python scripts/validate_data.py
```

The builder checks each file’s embedded granule ID, acquisition date, product type, track, frame, and look direction. It reads a crop from each scene, masks invalid pixels, and aligns the second scene to the first when grids differ. Four-connected increases and decreases are grouped separately. Hubbard uses frequency A HH and starts with a 3 dB per-pixel threshold and a 250-pixel minimum patch size. Rank weights both area and the absolute median change. Area is estimated in an equal-area projection. Display overlays are resized independently of the 10 m analysis grid.

For another region, use `--manifest` with a verified pair and study bounds using the Hubbard manifest as an example. `--threshold-db`, `--min-pixels`, and `--output` can be set for the run. After inspecting the new bundle, point the catalog entry to it and rerun validation. Deploy the complete `public/data/` tree together so the site index, metadata, and assets come from the same build.

For a local preview of the data endpoints:

```sh
.venv/bin/python -m http.server 8000 --bind 127.0.0.1 --directory public
```

## Sources

- NASA–ISRO NISAR Level-2 GCOV Beta v1, distributed by ASF. The exact granule IDs and processing release appear in the Hubbard bundle. Use the beta comparison as a demonstration and label the patches **radar change candidates**. [NASA Open Data record](https://data.nasa.gov/dataset/nisar-beta-geocoded-polarimetric-covariance-product-version-1)
- USGS preliminary Hubbard Glacier inventory, CC0 1.0. Its mapped points were interpreted from a separate Sentinel-1 comparison. Surface changes such as snow, moisture, and ice motion can also affect radar backscatter. The point layer publishes 723 geographic records. Two source records have unusable coordinates and their IDs are listed in the bundle. [USGS data and methods](https://www.usgs.gov/data/preliminary-locations-landslides-and-snow-avalanches-triggered-2025-m70-hubbard-glacier)

## On-demand comparison API

Run the API and static files from the same origin for local integration:

```sh
.venv/bin/python scripts/serve_api.py --input ~/Downloads
```

The server listens at `http://127.0.0.1:8000`. During frontend development, proxy `/api` and `/data` to that address from the frontend dev server. The original Hubbard event comparison is still available at `/data/sites.json`.

| Request | Response |
| --- | --- |
| `GET /api/sites/{id}/dates?start=2025-11-01&end=2025-12-31` | Compatible pass pairs near the selected glacier, plus their acquisition dates. The range may span up to 366 days. `catalogAvailable: false` means NASA discovery failed and only the baseline dates are returned. Candidate dates still need to form a compatible pair. |
| `POST /api/jobs` | Send `{"siteId":"hubbard","before":"2025-11-26","after":"2025-12-08"}` as JSON. Returns HTTP 202 and a job record with `statusUrl`. An existing successful job returns HTTP 200. Invalid inputs return 400, and a full queue returns 429. |
| `GET /api/jobs/{id}` | Poll the returned `statusUrl` every few seconds. States are `queued`, `discovering`, `analyzing`, `succeeded`, and `failed`. On success, fetch `bundleUrl`. On failure, display the job's `error`. |
| `GET /api/jobs/{id}/bundle/site.json` | Same geographic bundle contract as the static comparison. Resolve all overlay and patch URLs relative to this URL. |

The job uses an existing local HDF5 file when available. Otherwise, it uses authenticated HTTPS range requests to read the radar and mask data needed for the glacier crop. It does not fall back to downloading the full scene if ASF declines partial access. The bundle reports the bytes read and full file size for each remotely streamed scene. Generate the token through [NASA Earthdata Login](https://urs.earthdata.nasa.gov/profile) using an account that has accepted ASF's data access agreement. Run the setup command from a Terminal window and paste the token at the hidden prompt.

```sh
.venv/bin/python scripts/configure_earthdata.py
```

Restart the API afterward. Local setup saves the token in the private, Git-ignored `.earthdata-token` file. For hosting, configure the server-side `EARTHDATA_TOKEN` environment secret instead. The environment setting takes precedence.

New date pairs use NASA's catalog and must share a track, frame, orbit direction, acquisition mode, collection, and processing release. Glacier searches use the catalog map centers; apart from the original Hubbard analysis area, results cover an approximately 33 km square around each center. The worker prefers validated products, then provisional, then beta. The USGS event inventory is bundled with the original November–December 2025 comparison. It describes that event specifically.

The local server runs one worker, accepts up to four pending jobs, reuses a successful result for the same pair, and stores job status and results under `.slidetrace-jobs/`. Use `--state` to supply a persistent directory, with one server process per directory. Nonterminal jobs are requeued on restart. For a public deployment, place it behind a host that provides HTTPS and request rate limiting, and configure persistent storage for job results. The NASA token and the hosting configuration belong on the server.

## Run the integrated CryoTrace frontend

The CryoTrace copy used by this workspace is served at the root URL. Start the API with the Hubbard downloads and open [CryoTrace’s NISAR view](http://127.0.0.1:8000/#nisar-analysis).

```sh
.venv/bin/python scripts/serve_api.py --input ~/Downloads
```

The NISAR view opens with the published Hubbard comparison. **Run comparison** sends the chosen dates to the worker and shows its progress in the page. Hubbard's original dates also work as an on-demand job using the downloaded files. Other compatible pairs can use the server's Earthdata token. The Columbia Landsat observatory and the existing model endpoint settings are part of the original CryoTrace frontend.
