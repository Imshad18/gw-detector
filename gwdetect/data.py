"""GWOSC event catalogue and strain data access.

Strain files on GWOSC are 4096 s long (~130 MB). Instead of downloading them whole, only the
needed slice is read over HTTP range requests and cached locally as .npy.
"""
import json
import time
from pathlib import Path

import numpy as np
import requests

CACHE = Path(__file__).resolve().parent.parent / "cache"
EVENTS_URL = "https://gwosc.org/eventapi/json/allevents/"
CONFIDENT = ("GWTC-1-confident", "GWTC-2.1-confident", "GWTC-3-confident", "GWTC-4.0", "GWTC-5.0", "O4_Discovery_Papers")


def catalog(max_age_s=24 * 3600):
    """All published events with key parameters, newest first."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / "events.json"
    if not path.exists() or time.time() - path.stat().st_mtime > max_age_s:
        try:
            r = requests.get(EVENTS_URL, timeout=60)
            r.raise_for_status()
            path.write_text(r.text)
        except requests.RequestException:
            if not path.exists():
                raise
    raw = json.loads(path.read_text())["events"]
    best = {}
    for key, e in raw.items():
        name = e["commonName"]
        # keep the latest version from a confident catalogue
        if e.get("catalog.shortName") not in CONFIDENT and not name.startswith("GW"):
            continue
        if name not in best or e["version"] > best[name]["version"]:
            best[name] = e
    out = []
    for name, e in best.items():
        out.append({
            "name": name, "gps": e["GPS"], "catalog": e.get("catalog.shortName"),
            "m1": e.get("mass_1_source"), "m2": e.get("mass_2_source"),
            "mchirp": e.get("chirp_mass_source"), "mchirp_det": e.get("chirp_mass"),
            "distance": e.get("luminosity_distance"), "redshift": e.get("redshift"),
            "snr": e.get("network_matched_filter_snr"), "far": e.get("far"), "p_astro": e.get("p_astro"),
            "final_mass": e.get("final_mass_source"),
        })
    out.sort(key=lambda e: -e["gps"])
    return out


def event_detectors(gps):
    """Detectors with open data around this time."""
    from gwosc.locate import get_urls
    dets = []
    for d in ("H1", "L1", "V1"):
        try:
            if get_urls(d, gps - 64, gps + 64, sample_rate=4096):
                dets.append(d)
        except Exception:
            pass
    return dets


def strain(det, gps, before=64, after=64, log=print):
    """4096 Hz strain from gps-before to gps+after. Returns (t0, dt, array) or None if gaps."""
    CACHE.mkdir(exist_ok=True)
    key = CACHE / f"{det}_{gps:.1f}_{before}_{after}.npy"
    meta = key.with_suffix(".json")
    if key.exists() and meta.exists():
        m = json.loads(meta.read_text())
        return m["t0"], m["dt"], np.load(key)
    import fsspec
    import h5py
    from gwosc.locate import get_urls
    urls = get_urls(det, gps - before, gps + after, sample_rate=4096)
    urls = [u for u in urls if u.endswith(".hdf5")]
    if not urls:
        return None
    pieces = []
    t_start = gps - before
    for u in urls:
        log(f"{det}: reading {before + after} s slice of {u.rsplit('/', 1)[-1]}")
        with fsspec.open(u, "rb", block_size=2 ** 21) as fh, h5py.File(fh) as f:
            ds = f["strain/Strain"]
            x0, dt = float(ds.attrs["Xstart"]), float(ds.attrs["Xspacing"])
            i0 = max(0, int(round((t_start - x0) / dt)))
            i1 = min(len(ds), int(round((gps + after - x0) / dt)))
            if i1 > i0:
                pieces.append((x0 + i0 * dt, ds[i0:i1]))
    pieces.sort(key=lambda p: p[0])
    t0 = pieces[0][0]
    data = np.concatenate([p[1] for p in pieces]).astype(np.float64)
    if np.isnan(data).any():
        log(f"{det}: data has gaps in this window — skipped")
        return None
    np.save(key, data)
    meta.write_text(json.dumps({"t0": t0, "dt": dt}))
    return t0, dt, data
