#!/usr/bin/env python3
"""Bygger upp ett arkiv av DMI:s WAM-data för södra Östersjön.

Körs schemalagt i repot dyning-arkiv. För varje komplett DMI-körning som
fortfarande finns i API:t och inte redan är arkiverad sparas:

  falt/ÅÅÅÅ/MM/DD/HHz.bin.gz    timme 0–5 av körningen, alla fält, hela utsnittet
                                (uint8, [fält][timme][rad][kolumn], 255 = land)
  punkter/ÅÅÅÅ-MM.json          utfall per plats och timme (timme 0–5 av varje körning)
  prognoser/ÅÅÅÅ/MM/DD/HHz.json hela prognosen (0–132 h) för platserna
  statistik.json                snittfel per plats och prognoslängd, senaste 30 dygnen
  index.json                    vilka körningar som finns
  meta.json                     rutnät, fält, skalor, platsernas rutor

Filer skrivs aldrig om (utom punkter/statistik/index), så git-historiken växer
bara med det som faktiskt läggs till.
"""
import argparse
import datetime as dt
import glob
import gzip
import json
import os

import numpy as np

import fetch

HERE = os.path.dirname(os.path.abspath(__file__))
OUTCOME_HOURS = 6                    # timme 0–5 av varje körning blir "utfall"
SPOT_FC_FIELDS = ("shts", "swh", "mpts")  # sparas i prognosfilerna
STAT_FIELDS = ("shts", "swh")
STAT_DAYS = 30
ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(t):
    return t.strftime(ISO)


def parse(s):
    return dt.datetime.strptime(s, ISO).replace(tzinfo=dt.timezone.utc)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def spot_cells(spots, land):
    """Närmaste havsruta för varje plats – samma regel som kartsidan."""
    nj, ni = land.shape
    g = SPOT_GRID
    cells = {}
    for name, lat, lon in spots:
        j0 = round((g["lat0"] - lat) / g["dlat"])
        i0 = round((lon - g["lon0"]) / g["dlon"])
        best = None
        for r in range(5):
            bd = None
            for j in range(j0 - r, j0 + r + 1):
                for i in range(i0 - r, i0 + r + 1):
                    if 0 <= j < nj and 0 <= i < ni and not land[j, i]:
                        d = (j - j0) ** 2 + (i - i0) ** 2
                        if bd is None or d < bd:
                            bd, best = d, [j, i]
            if best:
                break
        if best:
            cells[name] = best
    return cells


SPOT_GRID = None


def archive_run(root, run, feats, meta, workers):
    global SPOT_GRID
    print(f"Arkiverar {run:%Y-%m-%d %HZ}")
    times, grid, cube = fetch.load_run(feats, workers)
    q = fetch.quantize_cube(cube)
    order = list(fetch.FIELDS)

    if not meta:
        SPOT_GRID = grid
        spots = load_json(os.path.join(HERE, "site", "spots.json"), [])
        cells0 = spot_cells(spots, q["shts"][0] == 255)
        meta.update({
            "source": "DMI WAM (wam_nsb), DMI Open Data",
            "grid": grid,
            "fields": fetch.fields_meta(),
            "order": order,
            "outcomeHours": OUTCOME_HOURS,
            "spotFcFields": list(SPOT_FC_FIELDS),
            "spots": [[n, la, lo, cells0.get(n)] for n, la, lo in spots],
        })
        save_json(os.path.join(root, "meta.json"), meta)
    elif meta["grid"] != grid:
        raise SystemExit(f"Rutnätet har ändrats ({meta['grid']} -> {grid}); arkivet behöver ses över")

    cells = {s[0]: s[3] for s in meta["spots"] if s[3]}
    t0 = parse(times[0])
    assert t0 == run, (t0, run)

    # 1) fält för timme 0–5
    path = os.path.join(root, "falt", f"{run:%Y/%m/%d}", f"{run:%H}z.bin.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    raw = b"".join(q[k][:OUTCOME_HOURS].tobytes() for k in order)
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as f:
        f.write(raw)

    # 2) utfall per plats i månadsfil
    mpath = os.path.join(root, "punkter", f"{run:%Y-%m}.json")
    month_start = run.replace(day=1, hour=0)
    nxt = (month_start + dt.timedelta(days=32)).replace(day=1)
    hours = int((nxt - month_start).total_seconds() // 3600)
    mon = load_json(mpath, None) or {
        "start": iso(month_start), "hours": hours, "fields": order,
        "values": {n: {k: [None] * hours for k in order} for n in cells},
    }
    for h in range(OUTCOME_HOURS):
        idx = int((run + dt.timedelta(hours=h) - month_start).total_seconds() // 3600)
        for n, (j, i) in cells.items():
            vals = mon["values"].setdefault(n, {k: [None] * hours for k in order})
            for k in order:
                v = int(q[k][h, j, i])
                vals[k][idx] = None if v == 255 else v
    save_json(mpath, mon)

    # 3) hela prognosen för platserna
    fpath = os.path.join(root, "prognoser", f"{run:%Y/%m/%d}", f"{run:%H}z.json")
    save_json(fpath, {
        "run": iso(run), "n": len(times),
        "values": {n: {k: [None if v == 255 else int(v) for v in q[k][:, j, i]] for k in SPOT_FC_FIELDS}
                   for n, (j, i) in cells.items()},
    })


def outcome_lookup(root, meta):
    """{(plats, fält): {timme-sedan-epok: värde}} från månadsfilerna."""
    out = {}
    for p in sorted(glob.glob(os.path.join(root, "punkter", "*.json"))):
        mon = load_json(p, None)
        start = parse(mon["start"])
        base = int(start.timestamp() // 3600)
        for n, fv in mon["values"].items():
            for k in STAT_FIELDS:
                d = out.setdefault((n, k), {})
                for h, v in enumerate(fv[k]):
                    if v is not None:
                        d[base + h] = v
    return out


def statistics(root, meta, runs):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=STAT_DAYS)
    obs = outcome_lookup(root, meta)
    acc = {}  # (plats, fält, dag) -> [summa|fel|, summa fel, antal]
    for r in runs:
        rt = parse(r)
        if rt < since - dt.timedelta(days=6):
            continue
        fc = load_json(os.path.join(root, "prognoser", f"{rt:%Y/%m/%d}", f"{rt:%H}z.json"), None)
        if not fc:
            continue
        base = int(rt.timestamp() // 3600)
        for n, fv in fc["values"].items():
            for k in STAT_FIELDS:
                o = obs.get((n, k), {})
                for lead, v in enumerate(fv[k]):
                    if v is None or lead < 1:
                        continue
                    vt = base + lead
                    if vt * 3600 < since.timestamp() or vt not in o:
                        continue
                    day = min(5, (lead + 23) // 24)
                    a = acc.setdefault((n, k, day), [0.0, 0.0, 0])
                    err = (v - o[vt]) * fetch.FIELDS[k][0]
                    a[0] += abs(err); a[1] += err; a[2] += 1
    res = {}
    for (n, k, day), (sa, se, c) in acc.items():
        res.setdefault(n, {}).setdefault(k, {})[str(day)] = {
            "mae": round(sa / c, 3), "bias": round(se / c, 3), "n": c}
    save_json(os.path.join(root, "statistik.json"), {
        "updated": iso(now), "days": STAT_DAYS, "spots": res})


def main():
    global SPOT_GRID
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="arkiv", help="arkivets rotkatalog")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-runs", type=int, default=8)
    args = ap.parse_args()
    os.makedirs(args.root, exist_ok=True)

    meta = load_json(os.path.join(args.root, "meta.json"), {})
    if meta:
        SPOT_GRID = meta["grid"]
    index = load_json(os.path.join(args.root, "index.json"), {"runs": []})
    have = set(index["runs"])

    added = 0
    for run, feats in fetch.complete_runs(back=args.max_runs):
        if iso(run) in have:
            continue
        archive_run(args.root, run, feats, meta, args.workers)
        have.add(iso(run))
        added += 1
        index["runs"] = sorted(have)
        save_json(os.path.join(args.root, "index.json"), index)

    statistics(args.root, meta, index["runs"])
    print(f"Klart: {added} nya körningar, totalt {len(index['runs'])}")


if __name__ == "__main__":
    main()
