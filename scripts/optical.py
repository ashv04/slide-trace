from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache
import json
import re
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely.geometry import box, shape

from earthdata import DataUnavailable, TLS

BASE = "https://planetarycomputer.microsoft.com/api"
COLLECTION = "landsat-c2-l2"
BANDS = ("swir16", "nir08", "green")


def search(target, bounds, synced):
    days = 60 if synced else 35
    bbox = ",".join(f"{value:.6f}" for value in bounds)
    params = urlencode({
        "collections": COLLECTION, "bbox": bbox, "limit": 200,
        "datetime": f"{target - timedelta(days=days)}T00:00:00Z/{target + timedelta(days=days)}T23:59:59Z",
    })
    try:
        with urlopen(f"{BASE}/stac/v1/search?{params}", timeout=20, context=TLS) as response:
            features = json.load(response)["features"]
    except (OSError, ValueError, KeyError) as error:
        raise DataUnavailable("The Landsat catalog is temporarily unavailable.") from error

    crop = box(*bounds)
    scenes = []
    for feature in features:
        properties = feature["properties"]
        cloud = properties.get("eo:cloud_cover")
        if cloud is None or not 0 <= cloud < 95 or not all(band in feature["assets"] for band in BANDS):
            continue
        if not shape(feature["geometry"]).covers(crop):
            continue
        item = feature["id"]
        if not re.fullmatch(r"L[A-Z][0-9]{2}_[A-Z0-9_]+", item):
            continue
        captured = date.fromisoformat(properties["datetime"][:10])
        params = urlencode([
            ("collection", COLLECTION), ("item", item),
            *[("assets", band) for band in BANDS],
            ("rescale", "7000,26000"),
        ])
        scenes.append({
            "date": captured.isoformat(), "id": item,
            "platform": properties["platform"], "sceneCloudPercent": cloud,
            "offsetDays": (captured - target).days,
            "imageUrl": f"{BASE}/data/v1/item/bbox/{bbox}/900x720.png?{params}",
            "sourceUrl": f"{BASE}/stac/v1/collections/{COLLECTION}/items/{item}",
        })
    return scenes


@lru_cache(maxsize=128)
def optical_pair(bounds, before, after, synced):
    with ThreadPoolExecutor(max_workers=2) as workers:
        earlier = workers.submit(search, before, bounds, synced)
        later = workers.submit(search, after, bounds, synced)
        matches = [(a, b) for a in earlier.result() for b in later.result() if a["date"] < b["date"]]
    if not matches:
        raise DataUnavailable("No usable Landsat pair covered this area near the requested dates. Try the seasonal view.")
    cloud_weight = 0.7 if synced else 1.5
    def score(pair):
        return sum(abs(scene["offsetDays"]) + scene["sceneCloudPercent"] * cloud_weight for scene in pair)
    a, b = min(matches, key=score)
    return {"before": a, "after": b, "targets": {"before": before.isoformat(), "after": after.isoformat()}}
