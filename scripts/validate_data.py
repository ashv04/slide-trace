"""Verify the published SlideTrace geographic bundles and their references."""
import argparse
from datetime import date
import json
import math
from pathlib import Path

from PIL import Image
from shapely.geometry import box, shape


def require(condition, message):
    if not condition:
        raise ValueError(message)


def asset(directory, url):
    require(isinstance(url, str) and url and not any(char in url for char in ":?#\\"),
            f"Invalid relative asset URL: {url}")
    path = (directory / url).resolve()
    require(path.is_relative_to(directory.resolve()) and path.is_file(), f"Missing or invalid asset: {path}")
    return path


def read_json(path):
    return json.loads(path.read_text())


def feature_collection(path, types, bounds):
    collection = read_json(path)
    require(collection["type"] == "FeatureCollection", f"{path}: expected a FeatureCollection")
    items = {}
    for feature in collection["features"]:
        key = feature["id"]
        require(feature["type"] == "Feature" and isinstance(key, str) and key and key not in items,
                f"{path}: invalid or duplicate feature ID {key}")
        geometry = shape(feature["geometry"])
        require(geometry.geom_type in types and geometry.is_valid and not geometry.is_empty and bounds.covers(geometry),
                f"{path}: invalid geometry for {key}")
        items[key] = feature["properties"]
    return items


def validate_bundle(path, site):
    bundle = read_json(path)
    require(bundle["schemaVersion"] == 1 and bundle["siteId"] == site["id"], f"{path}: incorrect version or site ID")
    require(bundle["mode"] == "geo" and bundle["isFixture"] is (site["status"] == "fixture"),
            f"{path}: unexpected bundle mode or fixture flag")
    west, south, east, north = bundle["bounds"]
    require(-180 <= west < east <= 180 and -90 <= south < north <= 90, f"{path}: invalid bounds")
    directory = path.parent
    dates, sizes = [], []
    for key in ("before", "after"):
        scene = bundle[key]
        dates.append(date.fromisoformat(scene["date"]))
        with Image.open(asset(directory, scene["imageUrl"])) as image:
            sizes.append(image.size)
            image.verify()
    require(dates[0] < dates[1] and sizes[0] == sizes[1], f"{path}: incompatible image pair")

    layers = bundle.get("referenceLayers")
    points = {}
    if layers:
        world = box(-180, -90, 180, 90)
        points = feature_collection(asset(directory, layers["pointsUrl"]), {"Point"}, world)
        boundary = feature_collection(asset(directory, layers["boundaryUrl"]), {"Polygon", "MultiPolygon"}, world)
        require(len(points) == layers["pointCount"] and boundary, f"{path}: invalid reference layers")
    patches = feature_collection(asset(directory, bundle["patchesUrl"]),
                                 {"Polygon", "MultiPolygon"}, box(west, south, east, north))
    require(bundle.get("patchCount", len(patches)) == len(patches), f"{path}: incorrect patch count")
    threshold = bundle["processing"]["thresholdDb"]
    require(math.isfinite(threshold) and threshold > 0, f"{path}: invalid detection threshold")
    for rank, (key, patch) in enumerate(patches.items(), start=1):
        area, change = patch["areaM2"], patch["medianDeltaDb"]
        require(patch["rank"] == rank and math.isfinite(area) and area > 0
                and math.isfinite(change) and abs(change) + 0.0005 >= threshold and patch["explanation"].strip(),
                f"{path}: invalid patch measurements or rank for {key}")
        if layers:
            reference = patch["reference"]
            require(reference["surveyCoverage"] in {"inside", "partial", "outside"}
                    and reference["searchRadiusM"] == layers["searchRadiusM"], f"{path}: invalid survey context for {key}")
            nearby = reference["nearbyPoint"]
            if nearby is not None:
                point = points.get(nearby["id"])
                require(point is not None and point["interpreta"] == nearby["interpretationCode"]
                        and 0 <= nearby["distanceM"] <= reference["searchRadiusM"], f"{path}: invalid point link for {key}")
    return len(patches)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1] / "public/data/sites.json")
    args = parser.parse_args()
    try:
        index = read_json(args.index)
        require(index["schemaVersion"] == 1, "Unsupported site index version")
        seen = set()
        for site in index["sites"]:
            key = site["id"]
            require(isinstance(key, str) and key and key not in seen, f"Invalid or duplicate site ID: {key}")
            seen.add(key)
            lon, lat = site["center"]
            require(-180 <= lon <= 180 and -90 <= lat <= 90, f"Invalid center for {key}")
            if site["status"] == "awaiting-usable-imagery":
                require(site["bundleUrl"] is None, f"Unexpected bundle URL for unavailable site {key}")
                continue
            require(site["status"] in {"analysis-ready", "fixture"}, f"Unknown status for {key}")
            count = validate_bundle(asset(args.index.parent, site["bundleUrl"]), site)
            print(f"{key}: validated {count} patches")
        print(f"Validated site index with {len(seen)} entries")
    except (OSError, TypeError, KeyError, ValueError) as error:
        parser.exit(1, f"Validation failed: {error}\n")


if __name__ == "__main__":
    main()
