"""
Scarica le note OSM aperte (+ chiuse negli ultimi 30 giorni) per tutta l'Italia
dalla Notes API, le assegna alle regioni e scrive:
  data/notes_<regione>.geojson, metadata.json, history.json
Se anche una sola richiesta fallisce, non viene scritto nulla (i dati vecchi restano).
"""
import json
import math
import os
import sys
import time
import datetime

import requests
from shapely.geometry import shape, Point, box
from shapely.strtree import STRtree

API = "https://api.openstreetmap.org/api/0.6/notes.json"
HEADERS = {"User-Agent": "ItaliaNotesBot/1.0 (https://github.com/ilselvaggio/italia-notes)"}
CLOSED_DAYS = 30          # note chiuse negli ultimi N giorni incluse
LIMIT = 10000             # massimo consentito dall'API
CELL = 1.0                # lato cella in gradi (API: max 25 gradi²)
NEAREST_MAX = 0.03        # note in mare vicino alla costa: assegna alla regione più vicina (~3 km)
COMMENT_MAX = 500

REGIONS_FILE = "regions.json"
BOUNDARIES_FILE = "regions_boundaries.geojson"
DATA_DIR = "data"
METADATA_FILE = "metadata.json"
HISTORY_FILE = "history.json"


def warn(msg):
    print(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}")


def fetch_cell(w, s, e, n, depth=0):
    """Scarica una cella; se si raggiunge il limite di 10000, divide in 4."""
    params = {"bbox": f"{w},{s},{e},{n}", "closed": CLOSED_DAYS, "limit": LIMIT}
    for attempt in range(4):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=180)
            if r.status_code in (429, 509, 503, 504):
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            feats = r.json().get("features", [])
            break
        except Exception as ex:
            print(f"   [!] cella {w},{s},{e},{n} tentativo {attempt+1}: {str(ex)[:150]}")
            time.sleep(15 * (attempt + 1))
    else:
        raise RuntimeError(f"Cella {w},{s},{e},{n} non scaricabile")

    time.sleep(1)  # rispetto per l'API OSM
    if len(feats) >= LIMIT and depth < 6:
        mx, my = (w + e) / 2, (s + n) / 2
        out = []
        for c in [(w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)]:
            out += fetch_cell(*c, depth=depth + 1)
        return out
    return feats


def parse_date(s):
    # "2026-10-20 12:34:56 UTC"
    return datetime.datetime.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc) if s else None


def area_km2(geom):
    lat = geom.centroid.y
    return geom.area * 111.32 * 111.32 * math.cos(math.radians(lat))


def main():
    with open(REGIONS_FILE, encoding="utf-8") as f:
        regions = json.load(f)
    with open(BOUNDARIES_FILE, encoding="utf-8") as f:
        bounds = json.load(f)

    rel_to_key = {str(int(v["osm"]) - 3600000000): k for k, v in regions.items()}
    keys, geoms = [], []
    for feat in bounds["features"]:
        rid = str(feat.get("id") or feat["properties"].get("@id", "")).replace("relation/", "")
        key = rel_to_key.get(rid)
        if not key:  # es. Sardegna: ID diverso, nome "Sardigna/Sardegna"
            name = feat["properties"].get("name", "")
            key = next((k for k, v in regions.items() if v["name"] in name), None)
        if key:
            keys.append(key)
            geoms.append(shape(feat["geometry"]).buffer(0))
    missing = set(regions) - set(keys)
    if missing:
        warn(f"Regioni senza confine: {', '.join(sorted(missing))}")
    tree = STRtree(geoms)

    # griglia di celle che toccano l'Italia
    minx = min(g.bounds[0] for g in geoms); miny = min(g.bounds[1] for g in geoms)
    maxx = max(g.bounds[2] for g in geoms); maxy = max(g.bounds[3] for g in geoms)
    cells = []
    x = math.floor(minx)
    while x < maxx:
        y = math.floor(miny)
        while y < maxy:
            c = box(x, y, x + CELL, y + CELL)
            if any(g.intersects(c) for g in geoms):
                cells.append((x, y, x + CELL, y + CELL))
            y += CELL
        x += CELL
    print(f"Celle da scaricare: {len(cells)}")

    raw = {}
    try:
        for i, c in enumerate(cells, 1):
            for nf in fetch_cell(*c):
                raw[nf["properties"]["id"]] = nf
            if i % 10 == 0:
                print(f"   {i}/{len(cells)} celle, {len(raw)} note")
    except RuntimeError as ex:
        warn(f"Download fallito, dati vecchi mantenuti: {ex}")
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=CLOSED_DAYS)
    per_region = {k: [] for k in regions}
    stats = {k: {"open": 0, "closed30": 0, "opened30": 0} for k in regions}
    outside = 0

    for nid, nf in raw.items():
        lon, lat = nf["geometry"]["coordinates"]
        pt = Point(lon, lat)
        key = None
        for idx in tree.query(pt):
            if geoms[idx].contains(pt):
                key = keys[idx]; break
        if key is None:
            idx = tree.nearest(pt)
            if geoms[idx].distance(pt) <= NEAREST_MAX:
                key = keys[idx]
        if key is None:
            outside += 1
            continue

        p = nf["properties"]
        comments = p.get("comments") or []
        first = comments[0] if comments else {}
        created = parse_date(p.get("date_created"))
        closed = parse_date(p.get("closed_at"))
        status = "open" if p.get("status") == "open" else "closed"
        if status == "closed" and (not closed or closed < since):
            continue  # riaperta/chiusa più vecchia: fuori finestra

        text = (first.get("text") or "").strip()
        per_region[key].append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {
                "id": nid,
                "status": status,
                "created": created.strftime("%Y-%m-%d") if created else None,
                "closed": closed.strftime("%Y-%m-%d") if closed else None,
                "user": first.get("user") or "anonimo",
                "comments": len(comments),
                "text": text[:COMMENT_MAX] + ("…" if len(text) > COMMENT_MAX else ""),
            },
        })
        st = stats[key]
        if status == "open":
            st["open"] += 1
        else:
            st["closed30"] += 1
        if created and created >= since:
            st["opened30"] += 1

    total_open = sum(s["open"] for s in stats.values())
    print(f"Note aperte in Italia: {total_open} (fuori dalle regioni: {outside})")

    old = {}
    if os.path.exists(METADATA_FILE):
        with open(METADATA_FILE, encoding="utf-8") as f:
            old = json.load(f)
    old_open = sum(r.get("open", 0) for r in old.get("regions", {}).values())
    if old_open and total_open < old_open * 0.7:
        warn(f"Solo {total_open} note aperte invece di ~{old_open}: risultato sospetto, dati vecchi mantenuti")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    for k, feats in per_region.items():
        with open(os.path.join(DATA_DIR, f"notes_{k}.geojson"), "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, separators=(",", ":"))

    areas = {k: round(area_km2(g)) for k, g in zip(keys, geoms)}
    meta_regions = {k: {**stats[k], "area_km2": areas.get(k, 0)} for k in regions}
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": now.isoformat(), "closed_days": CLOSED_DAYS, "regions": meta_regions}, f)

    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    today = now.strftime("%Y-%m-%d")
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "data": stats})
    history.sort(key=lambda h: h["date"])
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f)
    print("Fatto.")


if __name__ == "__main__":
    main()
