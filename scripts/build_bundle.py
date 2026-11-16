"""Build a browser-ready change bundle from a matched NISAR L-band GCOV pair."""
import argparse
from contextlib import contextmanager
from datetime import date
from fcntl import LOCK_EX, flock
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import h5py
import numpy as np
from PIL import Image
from rasterio.crs import CRS
from rasterio.features import shapes
from rasterio.transform import Affine, array_bounds, from_bounds
from rasterio.warp import reproject, transform_bounds, transform_geom
from scipy import ndimage
from earthdata import DataUnavailable, RadarRanges
from reference import add_reference, read_layer
from validate_data import validate_bundle

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value, compact=False):
    options = {"separators": (",", ":")} if compact else {"indent": 2}
    path.write_text(json.dumps(value, allow_nan=False, **options) + "\n")


def polygon_area(polygon, crs):
    equal_area = transform_geom(crs, "EPSG:6933", polygon)
    ring_areas = []
    for ring in equal_area["coordinates"]:
        points = np.asarray(ring)
        x, y = (points - points[0]).T
        ring_areas.append(abs(np.dot(x[:-1], y[1:]) - np.dot(y[:-1], x[1:])) / 2)
    return round(ring_areas[0] - sum(ring_areas[1:]))


@contextmanager
def open_scene(path, config, scene, stream_missing):
    if path.is_file():
        with h5py.File(path) as file:
            yield file, None
        return
    if not stream_missing:
        raise ValueError(f"Missing radar file: {path}")
    with RadarRanges(scene, config["collection"]) as source:
        with h5py.File(source) as file:
            yield file, source


def read_scene(path, config, scene, stream_missing=False):
    with open_scene(path, config, scene, stream_missing) as (file, source):
        identity = file["/science/LSAR/identification"]
        if "lookDirection" not in config:
            config["lookDirection"] = identity["lookDirection"][()].decode()
        expected = {
            "missionId": "NISAR", "productType": "GCOV", "radarBand": "L",
            "granuleId": scene["granuleId"], "trackNumber": config["track"],
            "frameNumber": config["frame"], "orbitPassDirection": config["direction"],
            "lookDirection": config["lookDirection"],
        }
        for field, value in expected.items():
            actual = identity[field][()]
            if isinstance(actual, bytes):
                actual = actual.decode().strip()
            if str(actual).casefold() != str(value).casefold():
                raise ValueError(f"{path.name}: expected {field}={value}, got {actual}")
        if identity["zeroDopplerStartTime"][()].decode()[:10] != scene["date"]:
            raise ValueError(f"{path.name}: acquisition date differs from the manifest")
        metadata = {
            "processingRelease": identity["compositeReleaseId"][()].decode(),
            "productVersion": identity["productVersion"][()].decode(),
            "productDoi": identity["productDoi"][()].decode() if "productDoi" in identity else config.get("collectionDoi", ""),
        }
        group = file[f"/science/LSAR/GCOV/grids/frequency{config['frequency']}"]
        crs = CRS.from_epsg(int(group["projection"][()]))
        west, south, east, north = transform_bounds("EPSG:4326", crs, *config["studyBounds"])
        x = group["xCoordinates"][:]
        y = group["yCoordinates"][:]
        dx = float(group["xCoordinateSpacing"][()])
        dy = float(group["yCoordinateSpacing"][()])
        if dx <= 0 or dy >= 0 or not crs.is_projected or crs.linear_units != "metre":
            raise ValueError(f"Expected a north-up metric grid in {path}")
        cols = np.flatnonzero((x >= west - dx) & (x <= east + dx))
        rows = np.flatnonzero((y >= south + dy) & (y <= north - dy))
        if not len(rows) or not len(cols):
            raise ValueError(f"Scene has no pixels over the study area: {path}")
        window = np.s_[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        power = group[config["channel"]][window].astype("float32")
        mask = group["mask"][window]
        valid = np.isfinite(power) & (power > 0) & (mask > 0) & (mask < 255)
        if "inputDataExceptionMask" in group:
            quality = group["inputDataExceptionMask"]
            flags = quality[window]
            description = quality.attrs.get("description", "")
            if "validity mask bits" in str(description).lower():
                # The low byte encodes anomalies; upper bits indicate fully focused channels.
                channel_bit = {"HHHH": 256, "HVHV": 512, "VHVH": 1024, "VVVV": 2048}[config["channel"]]
                valid &= ((flags & 255) == 0) & ((flags & channel_bit) != 0)
            else:
                valid &= flags == 0
        db = np.full(power.shape, np.nan, dtype="float32")
        db[valid] = 10 * np.log10(power[valid])
        if source is not None:
            metadata["retrieval"] = {"bytesRead": source.transferred, "wholeFileBytes": source.object_size}
    transform = Affine(dx, 0, x[cols[0]] - dx / 2, 0, dy, y[rows[0]] - dy / 2)
    return db, transform, crs, metadata


def project(pixels, src_transform, src_crs, dst_transform, dst_crs, shape):
    destination = np.full(shape, np.nan, dtype="float32")
    reproject(
        pixels, destination, src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs, src_nodata=np.nan, dst_nodata=np.nan,
    )
    return destination


def extract_patches(delta, transform, crs, threshold, min_pixels, site_id):
    candidates = []
    for sign in (1, -1):
        labels, _ = ndimage.label(sign * delta >= threshold)
        for index, window in enumerate(ndimage.find_objects(labels), start=1):
            if window is None:
                continue
            region = labels[window] == index
            count = int(region.sum())
            if count < min_pixels:
                continue
            median = float(np.median(delta[window][region]))
            origin = transform * Affine.translation(window[1].start, window[0].start)
            polygon, _ = next(shapes(region.astype("uint8"), mask=region, transform=origin))
            area = polygon_area(polygon, crs)
            row, col = np.argwhere(region)[0]
            patch_id = f"{site_id}-{'gain' if sign == 1 else 'loss'}-{row + window[0].start}-{col + window[1].start}"
            feature = {
                "type": "Feature", "id": patch_id,
                "geometry": transform_geom(crs, "EPSG:4326", polygon, precision=6),
                "properties": {
                    "areaM2": area, "medianDeltaDb": round(median, 3),
                    "explanation": (
                        f"Radar backscatter {'increased' if median > 0 else 'decreased'} "
                        f"by a median of {abs(median):.2f} dB across {area:,.0f} m²."
                    ),
                },
            }
            candidates.append((area * abs(median), feature))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]["id"]))
    features = [feature for _, feature in candidates]
    for rank, feature in enumerate(features, start=1):
        feature["properties"]["rank"] = rank
    return features


def render(path, pixels, low, high):
    valid = np.isfinite(pixels)
    gray = np.zeros(pixels.shape, dtype="uint8")
    gray[valid] = np.clip((pixels[valid] - low) * 255 / (high - low), 0, 255)
    Image.fromarray(np.stack([gray, valid.astype("uint8") * 255], axis=-1)).save(path)


def build(args, config, output):
    site_id = config["siteId"]
    scenes = []
    for key in ("before", "after"):
        scene = config[key]
        path = args.input / (scene["granuleId"] + ".h5")
        scenes.append(read_scene(path, config, scene, args.stream_missing))
    before, grid, crs, before_metadata = scenes[0]
    after, after_grid, after_crs, after_metadata = scenes[1]
    if after_crs != crs or after_grid != grid or after.shape != before.shape:
        after = project(after, after_grid, after_crs, grid, crs, before.shape)
    common = np.isfinite(before) & np.isfinite(after)
    if not common.any():
        raise ValueError("The two scenes have no valid overlapping pixels in the study area.")
    before[~common] = after[~common] = np.nan
    features = extract_patches(after - before, grid, crs, args.threshold_db, args.min_pixels, site_id)

    # Leaflet's default map projection is Web Mercator.
    mercator = CRS.from_epsg(3857)
    bounds = transform_bounds(crs, mercator, *array_bounds(*before.shape, grid))
    scale = min(1, 1800 / max(before.shape))
    height, width = (max(1, round(size * scale)) for size in before.shape)
    display = from_bounds(*bounds, width, height)
    low, high = np.percentile(np.concatenate((before[common], after[common])), [2, 98])
    high = max(high, low + 0.01)
    for name, pixels in (("before", before), ("after", after)):
        render(output / f"{name}.png", project(pixels, grid, crs, display, mercator, (height, width)), low, high)
    sources = {key: config[key] for key in (
        "collection", "maturity", "track", "frame", "direction", "lookDirection",
    )}
    sources["collectionDoi"] = config.get("collectionDoi", before_metadata["productDoi"])
    sources["before"] = config["before"] | before_metadata
    sources["after"] = config["after"] | after_metadata
    bundle = {
        "schemaVersion": 1, "siteId": site_id, "mode": "geo", "isFixture": False,
        "before": {"date": sources["before"]["date"], "imageUrl": "before.png"},
        "after": {"date": sources["after"]["date"], "imageUrl": "after.png"},
        "bounds": transform_bounds(mercator, "EPSG:4326", *bounds),
        "patchesUrl": "patches.geojson",
        "patchCount": len(features),
        "sources": sources,
        "processing": {
            "frequency": config["frequency"], "channel": config["channel"],
            "thresholdDb": args.threshold_db, "minPixels": args.min_pixels,
            "studyBounds": config["studyBounds"], "analysisCrs": crs.to_string(),
            "pixelSpacingM": [abs(grid.a), abs(grid.e)],
            "validOverlapPixels": int(common.sum()),
            "delta": "after minus before", "areaCrs": "EPSG:6933",
            "ranking": "areaM2 times absolute medianDeltaDb",
        },
    }
    if "referenceManifest" in config:
        manifest = args.manifest.parent / config["referenceManifest"]
        source = json.loads(manifest.read_text())
        points, excluded = read_layer(manifest.parent / source["points"])
        boundary, _ = read_layer(manifest.parent / source["boundary"])
        add_reference(features, points, boundary, source)
        bundle["referenceLayers"] = {
            "pointsUrl": "reference-points.geojson", "boundaryUrl": "reference-boundary.geojson",
            "searchRadiusM": source["searchRadiusM"],
            "pointCount": len(points["features"]), "excludedPointIds": excluded,
        }
        sources["reference"] = {key: source[key] for key in (
            "publisher", "title", "url", "license", "note", "interpretationLabels",
        )}
        write_json(output / "reference-points.geojson", points, compact=True)
        write_json(output / "reference-boundary.geojson", boundary, compact=True)
    write_json(output / "patches.geojson", {"type": "FeatureCollection", "features": features}, compact=True)
    write_json(output / "site.json", bundle)
    return len(features)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "inputs/hubbard-sources.json")
    parser.add_argument("--input", type=Path, default=ROOT / "inputs")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stream-missing", action="store_true", help="Read glacier subsets from ASF when a local granule is unavailable.")
    parser.add_argument("--threshold-db", type=float, default=3.0)
    parser.add_argument("--min-pixels", type=int, default=250)
    args = parser.parse_args()
    if not np.isfinite(args.threshold_db) or args.threshold_db <= 0 or args.min_pixels < 1:
        parser.error("Use a finite, positive threshold and at least one pixel per patch.")
    try:
        config = json.loads(args.manifest.read_text())
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", config["siteId"]):
            raise ValueError("Invalid site ID")
        if date.fromisoformat(config["before"]["date"]) >= date.fromisoformat(config["after"]["date"]):
            raise ValueError("The before date must precede the after date")
        if config["frequency"] not in ("A", "B") or config["channel"] not in ("HHHH", "HVHV", "VHVH", "VVVV"):
            raise ValueError("Choose frequency A or B and a linear-polarization power channel")
        output = args.output or ROOT / "public/data" / config["siteId"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=output.parent, prefix=".build-") as folder:
            staged = Path(folder)
            count = build(args, config, staged)
            validate_bundle(staged / "site.json", {"id": config["siteId"], "status": "analysis-ready"})
            output.mkdir(exist_ok=True)
            # Serialize publishers targeting the same directory.
            descriptor = os.open(output, os.O_RDONLY)
            try:
                flock(descriptor, LOCK_EX)
                for file in sorted(staged.iterdir(), key=lambda path: path.name == "site.json"):
                    file.replace(output / file.name)
            finally:
                os.close(descriptor)
    except (DataUnavailable, OSError, KeyError, ValueError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(f"Wrote {count} radar change candidates to {output}")


if __name__ == "__main__":
    main()
