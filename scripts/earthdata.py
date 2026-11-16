"""Discover compatible GCOV scenes and retrieve their science files."""
from collections import OrderedDict
from datetime import date
from io import RawIOBase
from itertools import combinations
import json
import os
import re
import ssl
from time import monotonic
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

import certifi

CATALOG = "https://cmr.earthdata.nasa.gov/search/granules.json"
ASF_HOST = "nisar.asf.earthdatacloud.nasa.gov"
COLLECTIONS = (
    ("NISAR_L2_GCOV_V1", "validated"),
    ("NISAR_L2_GCOV_PROVISIONAL_V1", "provisional"),
    ("NISAR_L2_GCOV_BETA_V1", "beta"),
)
GRANULE = re.compile(
    r"NISAR_L2_PR_GCOV_\d{3}_(\d{3})_([AD])_(\d{3})_"
    r"([\dA-Z]+_[A-Z]+_A)_(\d{8})T\d{6}_\d{8}T\d{6}_([A-Z]\d{5})_[A-Z_]+_\d{3}"
)
PAIR_FIELDS = ("collection", "track", "direction", "frame", "acquisitionMode", "processingRelease")
CHANNELS = {"SH": "HHHH", "DH": "HHHH", "QP": "HHHH", "SV": "VVVV", "DV": "VVVV"}
MAX_SUBSET_BYTES = 1024**3
BLOCK_SIZE = 2 * 1024**2
CACHE_BLOCKS = 16
TLS = ssl.create_default_context()
TLS.load_verify_locations(certifi.where())


class DataUnavailable(Exception):
    """A message that is safe to return to the viewer."""


def discover(config, start, end):
    found = []
    for collection, maturity in COLLECTIONS:
        params = urlencode({
            "short_name": collection,
            "point": ",".join(map(str, config["center"])),
            "temporal": f"{start}T00:00:00Z,{end}T23:59:59.999Z",
            "page_size": 2000,
        })
        try:
            with urlopen(f"{CATALOG}?{params}", timeout=20, context=TLS) as response:
                entries = json.load(response)["feed"]["entry"]
        except (OSError, ValueError, KeyError) as error:
            raise DataUnavailable("NASA’s scene catalog is temporarily unavailable.") from error
        for entry in entries:
            granule = entry.get("producer_granule_id") or entry.get("title", "")
            match = GRANULE.fullmatch(granule)
            if not match:
                continue
            track, direction, frame, acquisition_mode, ymd, release = match.groups()
            channel = CHANNELS.get(acquisition_mode.split("_")[1][:2])
            if channel is None:
                continue
            download = next(
                (link["href"] for link in entry.get("links", [])
                 if valid_science_url(link.get("href", ""), granule, collection)), None
            )
            if not download:
                continue
            found.append({
                "date": date.fromisoformat(ymd).isoformat(),
                "granuleId": granule,
                "downloadUrl": download,
                "collection": collection,
                "maturity": maturity,
                "track": int(track),
                "direction": {"A": "ascending", "D": "descending"}[direction],
                "frame": int(frame),
                "channel": channel,
                "acquisitionMode": acquisition_mode,
                "processingRelease": release,
            })
    return found


def valid_science_url(url, granule, collection):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == ASF_HOST
            and parsed.path == f"/NISAR/{collection}/{granule}/{granule}.h5"
            and not parsed.query and not parsed.fragment)


def available_pairs(scenes):
    groups = {}
    for scene in scenes:
        key = tuple(scene[field] for field in PAIR_FIELDS)
        groups.setdefault(key, set()).add(scene["date"])
    pairs = set()
    for dates in groups.values():
        pairs.update(combinations(sorted(dates), 2))
    return pairs


def compatible_pair(config, before, after):
    earlier = discover(config, before, before)
    later = discover(config, after, after)
    for collection, _ in COLLECTIONS:
        matches = [
            (first, second)
            for first in earlier if first["date"] == before and first["collection"] == collection
            for second in later if second["date"] == after
            and all(first[key] == second[key] for key in PAIR_FIELDS)
        ]
        if matches:
            return max(matches, key=lambda pair: tuple(scene["granuleId"] for scene in pair))
    raise DataUnavailable("No compatible radar pair was found for those dates at this glacier. Choose another available comparison.")


class RadarRanges(RawIOBase):
    """Seekable view of an ASF granule that never requests the whole file."""

    def __init__(self, scene, collection):
        super().__init__()
        self.token = os.environ.get("EARTHDATA_TOKEN", "").strip()
        if not self.token:
            raise DataUnavailable("A server Earthdata token is required to retrieve these dates.")
        self.url = scene["downloadUrl"]
        if not valid_science_url(self.url, scene["granuleId"], collection):
            raise DataUnavailable("NASA’s catalog did not return a recognized ASF science-file URL.")
        self.position = 0
        self.transferred = 0
        self.object_size = None
        self.deadline = monotonic() + 240
        self.blocks = OrderedDict()
        self.signed_url = None
        self._range(0, 0)

    def _range(self, first, last):
        length = last - first + 1
        remaining = self.deadline - monotonic()
        if self.transferred + length > MAX_SUBSET_BYTES or remaining <= 0:
            raise DataUnavailable("Reading this radar subset exceeded the server’s transfer limit.")
        request = Request(self.signed_url or self.url, headers={"Range": f"bytes={first}-{last}"})
        if not self.signed_url:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(request, timeout=min(25, remaining), context=TLS) as response:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if response.status != 206 or not match or urlsplit(response.url).scheme != "https":
                    raise DataUnavailable("ASF did not offer partial access to this radar file.")
                start, end, size = map(int, match.groups())
                if (start, end) != (first, last) or size <= end or size != (self.object_size or size):
                    raise DataUnavailable("ASF returned an unexpected radar byte range.")
                data = response.read(length + 1)
                if len(data) != length:
                    raise DataUnavailable("ASF returned an incomplete radar subset.")
                self.object_size = size
                self.transferred += length
                if urlsplit(response.url).hostname != ASF_HOST and urlsplit(response.url).query:
                    self.signed_url = response.url
                return data
        except HTTPError as error:
            if error.code in (401, 403):
                raise DataUnavailable("ASF denied access. Check your Earthdata token and ASF agreement.") from None
            raise DataUnavailable(f"ASF returned HTTP {error.code} while reading the radar subset.") from None
        except (OSError, HTTPException):
            raise DataUnavailable("The connection to ASF stopped while reading the radar subset. Try again.") from None

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=os.SEEK_SET):
        position = {os.SEEK_SET: 0, os.SEEK_CUR: self.position, os.SEEK_END: self.object_size}[whence] + offset
        if position < 0:
            raise ValueError("Negative radar file offset")
        self.position = position
        return position

    def readinto(self, buffer):
        output = memoryview(buffer).cast("B")
        copied = 0
        while copied < len(output) and self.position < self.object_size:
            start = self.position // BLOCK_SIZE * BLOCK_SIZE
            if start not in self.blocks:
                end = min(start + BLOCK_SIZE, self.object_size) - 1
                self.blocks[start] = self._range(start, end)
                if len(self.blocks) > CACHE_BLOCKS:
                    self.blocks.popitem(last=False)
            self.blocks.move_to_end(start)
            block = self.blocks[start]
            offset = self.position - start
            count = min(len(output) - copied, len(block) - offset)
            output[copied:copied + count] = block[offset:offset + count]
            copied += count
            self.position += count
        return copied
