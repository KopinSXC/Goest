"""Download walkable streets around a center point from OpenStreetMap (Overpass) and
write them as small tiles the game reads from its own site (streets/<lat>_<lon>.json).

Runs inside GitHub Actions. The center comes from an environment variable (a repository
secret) and is never printed; only counts are logged. Nothing here is committed: the
tiles go straight into the Pages artifact.
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

TILE = 100  # tiles are 1/TILE degree (about 1.1 km north-south)
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# keep in sync with fetchOverpass() in index.html
EXCLUDE = ("motorway|motorway_link|trunk|trunk_link|primary|primary_link|steps|construction|"
           "proposed|raceway|bus_guideway|corridor|elevator|escape|platform")


def fetch(bbox):
    s, w, n, e = bbox
    q = (f'[out:json][timeout:240][maxsize:536870912];way({s:.5f},{w:.5f},{n:.5f},{e:.5f})["highway"]'
         f'["highway"!~"^({EXCLUDE})$"]["access"!~"^(private|no)$"]["foot"!="no"]["area"!="yes"];out geom;')
    body = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(2):
        for url in ENDPOINTS:
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "goest-streets/1"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.load(r)
                print(f"fetched from {url.split('/')[2]}")
                return data.get("elements", [])
            except Exception as ex:  # try the next server
                print(f"{url.split('/')[2]} failed: {type(ex).__name__}")
        time.sleep(30)
    return None


def tiles_of_way(pts):
    keys = set()
    for (a, b), (c, d) in zip(pts, pts[1:] or pts):
        n = max(1, math.ceil(max(abs(c - a), abs(d - b)) / 0.001))
        for k in range(n + 1):
            lat, lon = a + (c - a) * k / n, b + (d - b) * k / n
            keys.add((math.floor(lat * TILE), math.floor(lon * TILE)))
    return keys


def main():
    center = os.environ.get("STREETS_CENTER", "").strip()
    out = sys.argv[1] if len(sys.argv) > 1 else "_site/streets"
    if not center:
        print("STREETS_CENTER not set, skipping streets")
        return
    half_km = float(os.environ.get("STREETS_HALF_KM", "5"))
    lat0, lon0 = (float(v) for v in center.split(","))
    dlat = half_km / 110.574
    dlon = half_km / (111.320 * math.cos(math.radians(lat0)))
    bbox = (lat0 - dlat, lon0 - dlon, lat0 + dlat, lon0 + dlon)
    elements = fetch(bbox)
    if elements is None:
        print("all servers failed, site is deployed without streets")
        return

    lat_range = range(math.floor(bbox[0] * TILE), math.floor(bbox[2] * TILE) + 1)
    lon_range = range(math.floor(bbox[1] * TILE), math.floor(bbox[3] * TILE) + 1)
    # every tile in the box is written, even empty ones, so the game can tell
    # "covered but no streets here" apart from "outside the covered area"
    tiles = {(a, b): [] for a in lat_range for b in lon_range}
    ways = 0
    for el in elements:
        geom = el.get("geometry") or []
        if el.get("type") != "way" or len(geom) < 2:
            continue
        pts = [(round(p["lat"], 5), round(p["lon"], 5)) for p in geom]
        flat = [v for p in pts for v in p]
        item = [el["id"], (el.get("tags") or {}).get("highway", ""), flat]
        for key in tiles_of_way(pts):
            if key in tiles:
                tiles[key].append(item)
        ways += 1

    os.makedirs(out, exist_ok=True)
    for (a, b), items in tiles.items():
        with open(os.path.join(out, f"{a}_{b}.json"), "w") as f:
            json.dump({"w": items}, f, separators=(",", ":"))
    print(f"{ways} ways, {len(tiles)} tiles, {sum(1 for v in tiles.values() if v)} non-empty")


if __name__ == "__main__":
    main()
