"""Serve SlideTrace bundles and queue on-demand glacier comparisons."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from configure_earthdata import load_token
from earthdata import DataUnavailable, available_pairs, compatible_pair, discover
from optical import optical_pair

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "inputs/hubbard-sources.json"
OPTICAL_PATH = re.compile(r"/api/sites/([a-z0-9-]+)/optical")
DATES_PATH = re.compile(r"/api/sites/([a-z0-9-]+)/dates")
JOB_PATH = re.compile(r"/api/jobs/([a-f0-9]{32})(?:/bundle/([a-z.-]+))?")
ASSET_TYPES = {
    "site.json": "application/json",
    "before.png": "image/png",
    "after.png": "image/png",
    "patches.geojson": "application/geo+json",
    "reference-points.geojson": "application/geo+json",
    "reference-boundary.geojson": "application/geo+json",
}
TERMINAL = {"succeeded", "failed"}
MAX_PENDING = 4


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD.")
    return date.fromisoformat(value)


class Jobs:
    def __init__(self, state, local_inputs):
        self.state = state
        self.inputs = local_inputs
        self.baseline = json.loads(SOURCE.read_text())
        index = json.loads((ROOT / "public/data/sites.json").read_text())
        self.configs = {}
        for site in index["sites"]:
            if site["status"] == "fixture":
                continue
            lon, lat = site["center"]
            half_lat = 0.15
            half_lon = half_lat / math.cos(math.radians(lat))
            self.configs[site["id"]] = {
                "siteId": site["id"], "center": site["center"], "frequency": "A",
                "studyBounds": [lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat],
            }
        self.configs["hubbard"]["studyBounds"] = self.baseline["studyBounds"]
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.records = {
            file.parent.name: json.loads(file.read_text())
            for file in state.glob("*/job.json")
        }
        for record in self.records.values():
            if record["status"] not in TERMINAL:
                self.save(record | {"status": "queued"})
                self.executor.submit(self.run, record["id"])

    def save(self, record):
        record = record | {"updatedAt": timestamp()}
        folder = self.state / record["id"]
        folder.mkdir(exist_ok=True)
        temp = folder / "job.tmp"
        temp.write_text(json.dumps(record) + "\n")
        temp.replace(folder / "job.json")
        self.records[record["id"]] = record
        return record

    def get(self, job_id):
        with self.lock:
            return self.records.get(job_id)

    def update(self, job_id, status, **fields):
        with self.lock:
            return self.save(self.records[job_id] | {"status": status} | fields)

    def submit(self, site_id, before, after):
        with self.lock:
            pending = 0
            for record in self.records.values():
                if record["status"] not in TERMINAL:
                    pending += 1
                if (record["siteId"], record["before"], record["after"]) == (site_id, before, after) and record["status"] != "failed":
                    if record["status"] != "succeeded" or (self.state / record["id"] / "bundle/site.json").is_file():
                        return record
            if pending >= MAX_PENDING:
                raise DataUnavailable("The analysis queue is full. Try again after a job finishes.")
            job_id = uuid4().hex
            record = self.save({
                "id": job_id, "siteId": site_id, "before": before, "after": after,
                "status": "queued", "createdAt": timestamp(), "statusUrl": f"/api/jobs/{job_id}",
            })
            self.executor.submit(self.run, job_id)
            return record

    def run(self, job_id):
        record = self.get(job_id)
        folder = self.state / job_id
        try:
            self.update(job_id, "discovering")
            config = self.configs[record["siteId"]].copy()
            if record["siteId"] == "hubbard" and all(
                record[key] == self.baseline[key]["date"] for key in ("before", "after")
            ):
                config.update(self.baseline)
                config["referenceManifest"] = str(SOURCE.parent / config["referenceManifest"])
            else:
                scenes = compatible_pair(config, record["before"], record["after"])
                config.update({field: scenes[0][field] for field in
                               ("collection", "maturity", "track", "direction", "frame", "channel")})
                for key, scene in zip(("before", "after"), scenes):
                    config[key] = {field: scene[field] for field in ("date", "granuleId", "downloadUrl")}
            with TemporaryDirectory(prefix="inputs-", dir=folder) as temp:
                directory = Path(temp)
                manifest = directory / "pair.json"
                manifest.write_text(json.dumps(config))
                self.update(job_id, "analyzing")
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/build_bundle.py"), "--manifest", str(manifest),
                     "--input", str(self.inputs), "--output", str(folder / "bundle"), "--stream-missing"],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode:
                    logging.error("Build %s failed: %s", job_id, result.stderr.strip())
                    message = result.stderr.strip()
                    if message.startswith("Build failed: "):
                        raise DataUnavailable(message.removeprefix("Build failed: "))
                    raise DataUnavailable("The selected radar scenes could not be analyzed.")
            bundle = json.loads((folder / "bundle/site.json").read_text())
            self.update(job_id, "succeeded", patchCount=bundle["patchCount"],
                        bundleUrl=f"/api/jobs/{job_id}/bundle/site.json")
        except DataUnavailable as error:
            self.update(job_id, "failed", error=str(error))
        except Exception:
            logging.exception("Job %s failed", job_id)
            self.update(job_id, "failed", error="The analysis could not finish. Try again.")


class Handler(SimpleHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def reply(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/jobs":
            return self.reply(404, {"error": "Unknown endpoint."})
        if self.headers.get_content_type() != "application/json":
            return self.reply(415, {"error": "Send application/json."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("Send a JSON request up to 4 KB.")
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict) or set(request) != {"siteId", "before", "after"}:
                raise ValueError("Provide siteId, before, and after.")
            if not isinstance(request["siteId"], str) or request["siteId"] not in self.server.jobs.configs:
                raise ValueError("Choose a listed glacier.")
            before, after = parse_date(request["before"]), parse_date(request["after"])
            if before >= after:
                raise ValueError("The before date must precede the after date.")
        except ValueError as error:
            return self.reply(400, {"error": str(error)})
        try:
            record = self.server.jobs.submit(request["siteId"], before.isoformat(), after.isoformat())
        except DataUnavailable as error:
            return self.reply(429, {"error": str(error)}, {"Retry-After": "30"})
        code = 200 if record["status"] == "succeeded" else 202
        return self.reply(code, record, {"Location": record["statusUrl"]})

    def do_GET(self):
        url = urlsplit(self.path)
        if optical := OPTICAL_PATH.fullmatch(url.path):
            return self.get_optical(optical[1], url.query)
        if dates := DATES_PATH.fullmatch(url.path):
            return self.get_dates(dates[1], url.query)
        if match := JOB_PATH.fullmatch(url.path):
            job_id, filename = match.groups()
            record = self.server.jobs.get(job_id)
            if record:
                if filename is None:
                    return self.reply(200, record)
                if record["status"] == "succeeded" and filename in ASSET_TYPES:
                    path = self.server.jobs.state / job_id / "bundle" / filename
                    if path.is_file():
                        with path.open("rb") as stream:
                            self.send_response(200)
                            self.send_header("Content-Type", ASSET_TYPES[filename])
                            self.send_header("Content-Length", str(path.stat().st_size))
                            self.end_headers()
                            shutil.copyfileobj(stream, self.wfile)
                        return
        if url.path.startswith("/api/"):
            return self.reply(404, {"error": "Job, result, or endpoint not found."})
        return super().do_GET()

    def get_optical(self, site_id, query):
        config = self.server.jobs.configs.get(site_id)
        if not config:
            return self.reply(404, {"error": "Unknown glacier."})
        params = parse_qs(query)
        synced = bool(params)
        try:
            if synced:
                before, after = (parse_date(params.get(key, [None])[0]) for key in ("before", "after"))
                if before >= after or before.year < 1982 or after.year > 2090:
                    raise ValueError("Choose two ordered Landsat target dates.")
            else:
                month = 7 if config["center"][1] >= 0 else 1
                before, after = date(2024, month, 20), date(2025, month, 20)
        except ValueError as error:
            return self.reply(400, {"error": str(error)})
        try:
            return self.reply(200, optical_pair(tuple(config["studyBounds"]), before, after, synced))
        except DataUnavailable as error:
            return self.reply(404, {"error": str(error)})

    def get_dates(self, site_id, query):
        config = self.server.jobs.configs.get(site_id)
        if not config:
            return self.reply(404, {"error": "Unknown glacier."})
        params = parse_qs(query)
        try:
            start, end = (parse_date(params.get(key, [None])[0]) for key in ("start", "end"))
            if not 0 <= (end - start).days <= 366:
                raise ValueError("Choose a date range of at most 366 days.")
        except ValueError as error:
            return self.reply(400, {"error": str(error)})
        pairs = set()
        if site_id == "hubbard":
            before, after = (self.server.jobs.baseline[key]["date"] for key in ("before", "after"))
            if start.isoformat() <= before and after <= end.isoformat():
                pairs.add((before, after))
        try:
            pairs.update(available_pairs(discover(config, start.isoformat(), end.isoformat())))
            catalog_available = True
        except DataUnavailable:
            catalog_available = False
        return self.reply(200, {
            "dates": sorted({date for pair in pairs for date in pair}),
            "pairs": [{"before": before, "after": after} for before, after in
                      sorted(pairs, key=lambda pair: (pair[1], pair[0]), reverse=True)],
            "catalogAvailable": catalog_available,
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--input", type=Path, default=ROOT / "inputs")
    parser.add_argument("--state", type=Path, default=ROOT / ".slidetrace-jobs")
    args = parser.parse_args()
    try:
        load_token()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "server.lock").open("a") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            parser.error(f"Another SlideTrace server is using {state}")
        handler = lambda *values: Handler(*values, directory=str(ROOT / "public"))
        with ThreadingHTTPServer((args.bind, args.port), handler) as server:
            server.jobs = Jobs(state, args.input.resolve())
            print(f"SlideTrace API listening at http://{args.bind}:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.jobs.executor.shutdown(cancel_futures=True)


if __name__ == "__main__":
    main()
