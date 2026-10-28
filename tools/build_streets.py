"""Cut walkable streets for a list of cities out of an OpenStreetMap extract and write
them as small tiles the game reads from its own site.

    python3 tools/build_streets.py china-latest.osm.pbf tools/cities.json streets

Needs osmium-tool. Runs inside GitHub Actions; the output goes into the Actions cache
and the Pages artifact, never into the repository.

Output:
  streets/coverage.json      [[south, west, north, east], ...] one box per city
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
import tempfile

TILE = 100  # tiles are 1/TILE degree (about 1.1 km north-south)
# keep in sync with fetchOverpass() in index.html
EXCLUDE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "steps",
           "construction", "proposed", "raceway", "bus_guideway", "corridor", "elevator", "escape", "platform"}


def walkable(tags):
    return (tags.get("highway") and tags["highway"] not in EXCLUDE
            and tags.get("access") not in ("private", "no") and tags.get("foot") != "no"
            and tags.get("area") != "yes")


def city_box(c):
    dlat = c["half_km"] / 110.574
    dlon = c["half_km"] / (111.320 * math.cos(math.radians(c["lat"])))
    return [round(c["lat"] - dlat, 4), round(c["lon"] - dlon, 4), round(c["lat"] + dlat, 4), round(c["lon"] + dlon, 4)]


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


def run(*args):
    subprocess.run(args, check=True)


def extract(pbf, boxes, work):
    """Return one GeoJSON-lines file of street linestrings per city box."""
    hw = os.path.join(work, "highways.osm.pbf")
    run("osmium", "tags-filter", "-O", "-o", hw, pbf, "w/highway")
    cfg = {"directory": work,
           "extracts": [{"output": f"city{i}.osm.pbf", "bbox": [b[1], b[0], b[3], b[2]]} for i, b in enumerate(boxes)]}
    with open(os.path.join(work, "extracts.json"), "w") as f:
        json.dump(cfg, f)
    run("osmium", "extract", "-O", "-c", os.path.join(work, "extracts.json"), hw)
    files = []
    for i in range(len(boxes)):
        out = os.path.join(work, f"city{i}.geojsonseq")
        run("osmium", "export", "-O", "-f", "geojsonseq", "--geometry-types=linestring", "-a", "id",
            "-o", out, os.path.join(work, f"city{i}.osm.pbf"))
        files.append(out)
    return files


def build(lines_by_city, boxes, out):
    tiles, seen, per_city = {}, set(), []
    for lines in lines_by_city:
        n = 0
        for line in lines:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            tags = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            wid = tags.get("@id")
            if geom.get("type") != "LineString" or not walkable(tags) or wid in seen:
                continue
            seen.add(wid)
            pts = [(lat, lon) for lon, lat in geom["coordinates"]]
            if len(pts) < 2:
                continue
            item = json.dumps([wid, tags["highway"], encode(pts)], separators=(",", ":"))
            for key in tiles_of_way(pts):
                tiles.setdefault(key, []).append(item)
            n += 1
        per_city.append(n)

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
        json.dump(boxes, f)
    # swap in only when complete, so a failed run never leaves half a tile set behind
    shutil.rmtree(out, ignore_errors=True)
    os.rename(tmp, out)
    return per_city, len(tiles), size


def main():
    pbf, cities_path, out = sys.argv[1:4]
    with open(cities_path) as f:
        cities = json.load(f)
    boxes = [city_box(c) for c in cities]
    with tempfile.TemporaryDirectory() as work:
        files = extract(pbf, boxes, work)
        handles = [open(p) for p in files]
        try:
            per_city, ntiles, size = build(handles, boxes, out)
        finally:
            for h in handles:
                h.close()
    for c, n in zip(cities, per_city):
        print(f"{c['name']}: {n} ways")
    print(f"{sum(per_city)} ways, {ntiles} tiles, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
