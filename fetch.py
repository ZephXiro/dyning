#!/usr/bin/env python3
"""Hämtar DMI:s senaste kompletta WAM-prognos (wam_nsb) och packar ett utsnitt
över södra Östersjön till kompakta binärfiler som kartsidan läser.

Utdata (i --out, standard site/data):
  meta.json        rutnät, tider, skalor
  <fält>.bin       uint8, [tid][rad][kolumn], 255 = land/saknas
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.request

import eccodes
import numpy as np

API = "https://opendataapi.dmi.dk/v1/forecastdata/collections/wam_nsb/items"

# Utsnitt: Kattegatt/Öresund till Gotland, Polens kust till Kalmarsund
LON_MIN, LON_MAX = 10.0, 21.5
LAT_MIN, LAT_MAX = 53.8, 58.0

# fält -> (skala, beskrivning). Värde = round(v / skala), 0..254, 255 = saknas
FIELDS = {
    "shts": (0.05, "Dyning, signifikant höjd (m)"),
    "mpts": (0.1, "Dyning, medelperiod (s)"),
    "mdts": (360 / 250, "Dyning, riktning (från, °)"),
    "swh": (0.05, "Total våghöjd (m)"),
    "pp1d": (0.1, "Toppperiod (s)"),
    "mwd": (360 / 250, "Våg, medelriktning (från, °)"),
    "shww": (0.05, "Vindvågor, höjd (m)"),
    "wind": (0.2, "Vind 10 m (m/s)"),
    "dwi": (360 / 250, "Vindriktning (från, °)"),
}
EXPECTED_STEPS = 133  # 0..132 h


def get_json(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"  försöker igen ({e})", file=sys.stderr)
            time.sleep(5 * (i + 1))


def items_for_run(run):
    d = get_json(f"{API}?modelRun={run:%Y-%m-%dT%H:%M:%SZ}&limit=500")
    return d["features"]


def latest_complete_run():
    now = dt.datetime.now(dt.timezone.utc)
    run = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6)
    for _ in range(8):
        feats = items_for_run(run)
        print(f"Körning {run:%Y-%m-%d %HZ}: {len(feats)} filer")
        if len(feats) >= EXPECTED_STEPS:
            return run, feats
        run -= dt.timedelta(hours=6)
    raise SystemExit("Hittade ingen komplett WAM-körning")


def download(url, dest, tries=4):
    for i in range(tries):
        try:
            urllib.request.urlretrieve(url, dest)
            return dest
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def read_grib(path, crop):
    """Returnerar {shortName: 2D-array} för utsnittet, rader från norr till söder."""
    out = {}
    j0, j1, i0, i1 = crop
    with open(path, "rb") as f:
        while True:
            h = eccodes.codes_grib_new_from_file(f)
            if h is None:
                break
            try:
                name = eccodes.codes_get(h, "shortName")
                if name in FIELDS:
                    ni = eccodes.codes_get(h, "Ni")
                    nj = eccodes.codes_get(h, "Nj")
                    mv = eccodes.codes_get(h, "missingValue")
                    v = eccodes.codes_get_values(h).reshape(nj, ni)
                    v = v[j0:j1, i0:i1].astype(np.float32)
                    v[v == mv] = np.nan
                    out[name] = v
            finally:
                eccodes.codes_release(h)
    return out


def grid_info(path):
    with open(path, "rb") as f:
        h = eccodes.codes_grib_new_from_file(f)
        g = {k: eccodes.codes_get(h, k) for k in (
            "Ni", "Nj", "latitudeOfFirstGridPointInDegrees",
            "longitudeOfFirstGridPointInDegrees",
            "latitudeOfLastGridPointInDegrees", "longitudeOfLastGridPointInDegrees",
            "jScansPositively")}
        eccodes.codes_release(h)
    assert g["jScansPositively"] == 0, "väntade rader från norr till söder"
    lat0, lon0 = g["latitudeOfFirstGridPointInDegrees"], g["longitudeOfFirstGridPointInDegrees"]
    # GRIB1 avrundar stegen till tusendels grad; räkna exakt från hörnen
    dlat = (lat0 - g["latitudeOfLastGridPointInDegrees"]) / (g["Nj"] - 1)
    dlon = (g["longitudeOfLastGridPointInDegrees"] - lon0) / (g["Ni"] - 1)
    j0 = int(np.floor((lat0 - LAT_MAX) / dlat))
    j1 = int(np.ceil((lat0 - LAT_MIN) / dlat)) + 1
    i0 = int(np.floor((LON_MIN - lon0) / dlon))
    i1 = int(np.ceil((LON_MAX - lon0) / dlon)) + 1
    return (j0, j1, i0, i1), {
        "lat0": round(lat0 - j0 * dlat, 6),  # nordligaste raden
        "lon0": round(lon0 + i0 * dlon, 6),  # västligaste kolumnen
        "dlat": round(dlat, 8), "dlon": round(dlon, 8),
        "nj": j1 - j0, "ni": i1 - i0,
    }


def quantize(a, scale, is_dir):
    q = np.full(a.shape, 255, dtype=np.uint8)
    ok = ~np.isnan(a)
    v = a[ok]
    if is_dir:
        q[ok] = (np.round((v % 360) / scale).astype(np.int32) % 250).astype(np.uint8)
    else:
        q[ok] = np.clip(np.round(v / scale), 0, 254).astype(np.uint8)
    return q


DIR_FIELDS = ("mdts", "mwd", "dwi")


def fields_meta():
    return {k: {"scale": sc, "label": lb, "dir": k in DIR_FIELDS} for k, (sc, lb) in FIELDS.items()}


def complete_runs(back=8):
    """Kompletta körningar som fortfarande finns i API:t, äldst först."""
    now = dt.datetime.now(dt.timezone.utc)
    run = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 6 * 6)
    out = []
    for _ in range(back):
        feats = items_for_run(run)
        if len(feats) >= EXPECTED_STEPS:
            out.append((run, sorted(feats, key=lambda x: x["properties"]["datetime"])))
        run -= dt.timedelta(hours=6)
    return out[::-1]


def load_run(feats, workers=8):
    """Laddar ner en körning och returnerar (tider, rutnät, {fält: float32[t, j, i]})."""
    times = [x["properties"]["datetime"] for x in feats]
    with tempfile.TemporaryDirectory() as tmp:
        paths = [os.path.join(tmp, f"{k:03d}.grib") for k in range(len(feats))]
        print(f"  laddar ner {len(feats)} filer …")
        with cf.ThreadPoolExecutor(workers) as ex:
            list(ex.map(download, [x["asset"]["data"]["href"] for x in feats], paths))
        crop, grid = grid_info(paths[0])
        cube = {k: np.full((len(paths), grid["nj"], grid["ni"]), np.nan, np.float32) for k in FIELDS}
        for t, p in enumerate(paths):
            for k, v in read_grib(p, crop).items():
                cube[k][t] = v
    return times, grid, cube


def quantize_cube(cube):
    return {k: quantize(cube[k], FIELDS[k][0], k in DIR_FIELDS) for k in FIELDS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    run, feats = latest_complete_run()
    feats.sort(key=lambda x: x["properties"]["datetime"])
    times, grid, cube = load_run(feats, args.workers)
    print("Rutnät:", grid)
    for k, q in quantize_cube(cube).items():
        q.tofile(os.path.join(args.out, f"{k}.bin"))

    meta = {
        "source": "DMI WAM (wam_nsb), DMI Open Data",
        "modelRun": f"{run:%Y-%m-%dT%H:%M:%SZ}",
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "times": times,
        "grid": grid,
        "fields": fields_meta(),
    }
    with open(os.path.join(args.out, "meta.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False)
    print(f"Klart: {len(times)} tidssteg, körning {meta['modelRun']}")


if __name__ == "__main__":
    main()
