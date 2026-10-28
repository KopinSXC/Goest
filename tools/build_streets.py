"""Turn an OpenStreetMap extract into small street tiles the game reads from its own site.

    python3 tools/build_streets.py china-latest.osm.pbf tools/cities.json streets

Tries the whole country first. If that is larger than SIZE_LIMIT (GitHub Pages sites
must stay under 1 GB) it falls back to the boxes in cities.json. Needs osmium-tool.
Runs inside GitHub Actions; the output goes into the Actions cache and the Pages
artifact, never into the repository.

Output:
  streets/coverage.json      {"cells": ["lat_lon", ...]}  whole degrees that have streets, or
                             {"boxes": [[south, west, north, east], ...]}  one box per city
  streets/<lat>_<lon>.json   tiles of 1/TILE degree, only where there are streets:
                             {"w": [[way id, highway, [lat0, lon0, dlat1, dlon1, ...]], ...]}
                             coordinates are integers in 1e-5 degrees, after the first
                             point each one is the difference from the previous point
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time

TILE = 100              # tiles are 1/TILE degree (about 1.1 km north-south)
SIZE_LIMIT = 900e6      # bytes of tiles allowed for the whole-country build
SIMPLIFY = 1.5e-5       # degrees (about 1.5 m) of shape detail dropped per way
# keep in sync with fetchOverpass() in index.html; tracks are farm roads and add a lot of size
EXCLUDE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "steps",
           "construction", "proposed", "raceway", "bus_guideway", "corridor", "elevator", "escape",
           "platform", "track"}
EXCLUDE_SERVICE = {"parking_aisle", "driveway", "drive-through"}

T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.0f}s] {msg}", flush=True)


def walkable(tags):
    return (tags.get("highway") and tags["highway"] not in EXCLUDE
            and tags.get("service") not in EXCLUDE_SERVICE
            and tags.get("access") not in ("private", "no") and tags.get("foot") != "no"
            and tags.get("area") != "yes")


def city_box(c):
    dlat = c["half_km"] / 110.574
    dlon = c["half_km"] / (111.320 * math.cos(math.radians(c["lat"])))
    return [round(c["lat"] - dlat, 4), round(c["lon"] - dlon, 4), round(c["lat"] + dlat, 4), round(c["lon"] + dlon, 4)]


def simplify(pts, tol=SIMPLIFY):
    """Douglas-Peucker on lat/lon, iterative so long ways cannot hit the recursion limit."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (y1, x1), (y2, x2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-12
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            d = abs(dy * (pts[i][1] - x1) - dx * (pts[i][0] - y1)) / norm
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def tiles_of_way(pts):
    keys = set()
    for (a, b), (c, d) in zip(pts, pts[1:] or pts):
        n = max(1, math.ceil(max(abs(c - a), abs(d - b)) / 0.001))
        for k in range(n + 1):
            keys.add((math.floor((a + (c - a) * k / n) * TILE), math.floor((b + (d - b) * k / n) * TILE)))
    return keys


def encode(pts):
    ints = [(round(lat * 1e5), round(lon * 1e5)) for lat, lon in pts]
    out = list(ints[0])
    for (a, b), (c, d) in zip(ints, ints[1:]):
        out += [c - a, d - b]
    return out


def stream_ways(hw):
    """Yield (id, highway, points) for every walkable street in the highway-only extract."""
    p = subprocess.Popen(["osmium", "export", "-f", "geojsonseq", "--geometry-types=linestring", "-a", "id", hw],
                         stdout=subprocess.PIPE, text=True, bufsize=1 << 20)
    for line in p.stdout:
        line = line.strip().lstrip("\x1e")
        if not line:
            continue
        feat = json.loads(line)
        tags = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString" or not walkable(tags):
            continue
        pts = [(lat, lon) for lon, lat in geom["coordinates"]]
        if len(pts) >= 2:
            yield tags.get("@id"), tags["highway"], pts
    if p.wait():
        raise RuntimeError(f"osmium export exited with {p.returncode}")


def collect(ways, inside=None):
    """Group encoded ways by tile; inside(pts) limits which ways are kept."""
    tiles, n, size = {}, 0, 0
    for wid, hw, pts in ways:
        if inside and not inside(pts):
            continue
        pts = simplify(pts)
        item = json.dumps([wid, hw, encode(pts)], separators=(",", ":"))
        for key in tiles_of_way(pts):
            tiles.setdefault(key, []).append(item)
            size += len(item) + 1
        n += 1
        if n % 500000 == 0:
            log(f"{n} ways, {len(tiles)} tiles, {size / 1e6:.0f} MB so far")
    return tiles, n, size


def write(tiles, coverage, out):
    tmp = out + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    size = 0
    for (a, b), items in tiles.items():
        path = os.path.join(tmp, f"{a}_{b}.json")
        with open(path, "w") as f:
            f.write('{"w":[' + ",".join(items) + "]}")
        size += os.path.getsize(path)
    with open(os.path.join(tmp, "coverage.json"), "w") as f:
        json.dump(coverage, f, separators=(",", ":"))
    # swap in only when complete, so a failed run never leaves half a tile set behind
    shutil.rmtree(out, ignore_errors=True)
    os.rename(tmp, out)
    return size


def main():
    pbf, cities_path, out = sys.argv[1:4]
    hw = os.path.join(os.path.dirname(os.path.abspath(pbf)), "highways.osm.pbf")
    log("filtering highways")
    subprocess.run(["osmium", "tags-filter", "-O", "-o", hw, pbf, "w/highway"], check=True)
    log(f"highways: {os.path.getsize(hw) / 1e6:.0f} MB")

    log("whole country")
    tiles, n, size = collect(stream_ways(hw))
    log(f"whole country: {n} ways, {len(tiles)} tiles, {size / 1e6:.0f} MB")
    if size <= SIZE_LIMIT:
        cells = sorted({f"{math.floor(a / TILE)}_{math.floor(b / TILE)}" for a, b in tiles})
        coverage = {"cells": cells}
    else:
        log(f"over the {SIZE_LIMIT / 1e6:.0f} MB limit, keeping only the cities in {cities_path}")
        with open(cities_path) as f:
            boxes = [city_box(c) for c in json.load(f)]

        def inside(pts):
            la = [p[0] for p in pts]
            lo = [p[1] for p in pts]
            return any(min(la) <= b[2] and max(la) >= b[0] and min(lo) <= b[3] and max(lo) >= b[1] for b in boxes)
        tiles = None
        tiles, n, size = collect(stream_ways(hw), inside)
        coverage = {"boxes": boxes}
    written = write(tiles, coverage, out)
    log(f"done: {'cells' if 'cells' in coverage else 'cities'} mode, {n} ways, {len(tiles)} tiles, {written / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
