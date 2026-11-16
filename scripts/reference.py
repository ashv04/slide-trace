"""Convert USGS shapefiles to GeoJSON and attach proximity context."""
import io
import math
from pathlib import Path
from zipfile import ZipFile

import shapefile
from rasterio.crs import CRS
from rasterio.warp import transform, transform_geom
from shapely import STRtree, make_valid, union_all
from shapely.geometry import shape


def read_layer(path):
    with ZipFile(path) as archive:
        shp = next(name for name in archive.namelist() if Path(name).suffix.lower() == ".shp")
        stem = shp[:-4]
        crs = CRS.from_wkt(archive.read(stem + ".prj").decode("utf-8"))
        features = []
        excluded = []
        with shapefile.Reader(shp=io.BytesIO(archive.read(shp)),
                              dbf=io.BytesIO(archive.read(stem + ".dbf"))) as reader:
            for index, record in enumerate(reader.iterShapeRecords(), start=1):
                geometry = record.shape.__geo_interface__
                geometry = transform_geom(crs, "EPSG:4326", geometry, precision=7)
                if geometry["type"] == "Point":
                    lon, lat = geometry["coordinates"][:2]
                    if not (math.isfinite(lon) and math.isfinite(lat) and abs(lon) <= 180 and abs(lat) <= 90):
                        excluded.append(str(index))
                        continue
                features.append({
                    "type": "Feature", "id": str(index),
                    "geometry": geometry,
                    "properties": record.record.as_dict(),
                })
    if not features:
        raise ValueError(f"Empty reference layer: {path}")
    return {"type": "FeatureCollection", "features": features}, excluded


def add_reference(patches, points, boundary, source):
    crs = source["distanceCrs"]
    radius = source["searchRadiusM"]
    point_features = points["features"]
    xy = [point["geometry"]["coordinates"] for point in point_features]
    x, y = transform("EPSG:4326", crs, *zip(*xy))
    tree = STRtree([shape({"type": "Point", "coordinates": pair}) for pair in zip(x, y)])
    survey = union_all([
        make_valid(shape(transform_geom("EPSG:4326", crs, feature["geometry"])))
        for feature in boundary["features"]
    ])
    for feature in patches:
        polygon = make_valid(shape(transform_geom("EPSG:4326", crs, feature["geometry"])))
        coverage = "inside" if survey.covers(polygon) else "outside"
        if coverage == "outside" and survey.intersects(polygon):
            coverage = "partial"
        reference = {"surveyCoverage": coverage, "searchRadiusM": radius, "nearbyPoint": None}
        indices, distances = tree.query_nearest(polygon, max_distance=radius, return_distance=True)
        if len(indices):
            index, distance = min(zip(indices, distances), key=lambda pair: (pair[1], pair[0]))
            point = point_features[index]
            reference["nearbyPoint"] = {
                "id": point["id"], "distanceM": round(float(distance)),
                "interpretationCode": point["properties"]["interpreta"],
            }
        feature["properties"]["reference"] = reference
