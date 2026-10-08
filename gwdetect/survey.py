"""Blind survey of long stretches of open data.

Works through 4096 s GWOSC files, analyses every 64 s window where both LIGO detectors pass
the CBC data-quality categories 1 and 2, keeps the loudest coincident trigger per window,
and accumulates time-slide background so each trigger gets a false-alarm rate.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import h5py
import numpy as np
import requests

from . import search
from . import waveform as W

ROOT = Path(__file__).resolve().parent.parent / "surveys"
BLOCKS = Path(__file__).resolve().parent.parent / "cache" / "blocks"
DETS = ("H1", "L1")
SEG_S, STEP_S, PSD_S = 64.0, 48.0, 128.0
TRIGGER_SNR = 7.5
SLIDES = 300
YEAR = 365.25 * 86400
DQ_OK = 0b111     # DATA, CBC_CAT1, CBC_CAT2
INJ_CBC_FREE = 0b1  # NO_CBC_HW_INJ

RUNS = {"O1": (1126051217, 1137254417), "O2": (1164556817, 1187733618), "O3a": (1238166018, 1253977218),
        "O3b": (1256655618, 1269363618), "O4a": (1368975618, 1389456018)}


def _all_events():
    """Every GWOSC event, including marginal catalogues, for cross-matching."""
    path = Path(__file__).resolve().parent.parent / "cache" / "allevents.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        r = requests.get("https://gwosc.org/eventapi/json/allevents/", timeout=60)
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r.text)
    ev = json.loads(path.read_text())["events"]
    return [(e["GPS"], e["commonName"], e.get("catalog.shortName")) for e in ev.values()]


def gracedb_near(gps, window=2.0):
    """Public LIGO/Virgo/KAGRA alerts (superevents) around a GPS time."""
    try:
        r = requests.get("https://gracedb.ligo.org/api/superevents/",
                         params={"query": f"gpstime: {gps - window} .. {gps + window}", "format": "json"}, timeout=30)
        r.raise_for_status()
        return [{"id": s["superevent_id"], "t0": s["t_0"], "far": s.get("far"),
                 "url": f"https://gracedb.ligo.org/superevents/{s['superevent_id']}/view/"} for s in r.json().get("superevents", [])]
    except Exception:
        return None  # unknown (offline)


def crossmatch(gps, events=None):
    events = events if events is not None else _all_events()
    cat = [{"name": n, "catalog": c, "dt": round(g - gps, 3)} for g, n, c in events if abs(g - gps) < 1.0]
    cat.sort(key=lambda x: (("confident" not in (x["catalog"] or "")) and not (x["catalog"] or "").startswith("GWTC"), x["catalog"] or ""))
    return {"catalog": cat, "gracedb": gracedb_near(gps)}


def plan(start, end, dets=DETS):
    """4096 s blocks inside [start, end) where every detector has some data."""
    from gwosc.timeline import get_segments
    segs = None
    for d in dets:
        s = get_segments(f"{d}_DATA", start, end)
        segs = s if segs is None else [(max(a, c), min(b, e)) for a, b in segs for c, e in s if min(b, e) - max(a, c) > 0]
    blocks = set()
    for a, b in segs or []:
        if b - a < PSD_S:
            continue
        k = int(a // 4096) * 4096
        while k < b:
            blocks.add(k)
            k += 4096
    return sorted(blocks), [(int(a), int(b)) for a, b in (segs or [])]


def _download(det, block):
    from gwosc.locate import get_urls
    urls = [u for u in get_urls(det, block + 1, block + 4095, sample_rate=4096) if u.endswith(".hdf5")]
    if not urls:
        return None
    BLOCKS.mkdir(parents=True, exist_ok=True)
    path = BLOCKS / urls[0].rsplit("/", 1)[-1]
    if not path.exists():
        tmp = path.with_suffix(".part")
        with requests.get(urls[0], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        tmp.rename(path)
    with h5py.File(path) as f:
        ds = f["strain/Strain"]
        out = {"t0": float(ds.attrs["Xstart"]), "dt": float(ds.attrs["Xspacing"]), "x": ds[()].astype(np.float64),
               "dq": f["quality/simple/DQmask"][()], "inj": f["quality/injections/Injmask"][()], "path": path}
    return out


class Survey:
    def __init__(self, sid):
        self.dir = ROOT / sid
        self.sid = sid
        self.stop = threading.Event()

    # ---- persistence ----
    @property
    def state_path(self):
        return self.dir / "state.json"

    def state(self):
        return json.loads(self.state_path.read_text())

    def save(self, st):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.state_path)

    def triggers(self):
        p = self.dir / "triggers.json"
        return json.loads(p.read_text()) if p.exists() else []

    def background(self):
        p = self.dir / "background.npy"
        return np.load(p) if p.exists() else np.zeros(0, np.float32)

    @classmethod
    def create(cls, run, start, hours):
        start = int(start)
        end = int(start + hours * 3600)
        sid = f"{run}_{start}_{int(hours)}h"
        sv = cls(sid)
        sv.dir.mkdir(parents=True, exist_ok=True)
        if not sv.state_path.exists():
            blocks, segs = plan(start, end)
            sv.save({"id": sid, "run": run, "start": start, "end": end, "hours": hours, "dets": list(DETS),
                     "blocks": blocks, "done": [], "status": "paused", "analysed_s": 0.0, "bg_time_s": 0.0,
                     "segments": 0, "skipped_dq_s": 0.0, "created": time.time(), "log": [],
                     "data_segments": segs})
        return sv

    def far(self, snr, bg=None, st=None):
        bg = self.background() if bg is None else bg
        st = st or self.state()
        years = st["bg_time_s"] / YEAR
        if years <= 0:
            return None, None
        n = int((bg >= snr).sum())
        return (n / years if n else 1 / years), n == 0

    # ---- main loop ----
    def run(self, progress=None):
        self.progress = progress
        st = self.state()
        st["status"] = "running"
        self.save(st)
        events = _all_events()
        bank = search.template_bank()
        todo = [b for b in st["blocks"] if b not in st["done"]]

        def log(msg):
            st["log"] = (st["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]
            if progress:
                progress(msg)

        with ThreadPoolExecutor(1) as pre:
            fut = pre.submit(lambda b: {d: _download(d, b) for d in DETS}, todo[0]) if todo else None
            for bi, block in enumerate(todo):
                if self.stop.is_set():
                    break
                log(f"Block {block}: downloading {len(DETS)} × 4096 s of strain")
                try:
                    data = fut.result()
                except Exception as exc:
                    log(f"Block {block}: download failed ({exc}); will retry later")
                    data = None
                # prefetch the next block while this one is analysed
                fut = pre.submit(lambda b: {d: _download(d, b) for d in DETS}, todo[bi + 1]) if bi + 1 < len(todo) else None
                if data is None:
                    continue
                if any(v is None for v in data.values()):
                    log(f"Block {block}: a detector has no file; skipped")
                    st["done"].append(block)
                    self.save(st)
                    continue
                self._block(block, data, bank, events, st, log)
                for v in data.values():
                    try:
                        Path(v["path"]).unlink()
                    except OSError:
                        pass
                if not self.stop.is_set():
                    st["done"].append(block)
                self.save(st)
        st["status"] = "paused" if self.stop.is_set() else "finished"
        log("Paused" if self.stop.is_set() else "Survey finished")
        self.save(st)

    def _block(self, block, data, bank, events, st, log):
        h, l = data["H1"], data["L1"]
        n_sec = min(len(h["dq"]), len(l["dq"]))
        good = ((h["dq"][:n_sec] & DQ_OK) == DQ_OK) & ((l["dq"][:n_sec] & DQ_OK) == DQ_OK)
        inj_free = ((h["inj"][:n_sec] & INJ_CBC_FREE) == INJ_CBC_FREE) & ((l["inj"][:n_sec] & INJ_CBC_FREE) == INJ_CBC_FREE)
        st["skipped_dq_s"] += float(n_sec - good.sum())
        # contiguous good stretches, in seconds from block start
        edges = np.flatnonzero(np.diff(np.concatenate([[0], good.astype(int), [0]])))
        runs = list(zip(edges[::2], edges[1::2]))
        centers = []
        for a, b in runs:
            c = a + PSD_S / 2
            while c + PSD_S / 2 <= b:
                centers.append(c)
                c += STEP_S
        log(f"Block {block}: {good.sum()} s pass data quality → {len(centers)} analysis windows")
        trig = self.triggers()
        bg_new = []
        fs = int(round(1 / h["dt"]))
        for k, c in enumerate(centers):
            if self.stop.is_set():
                break
            gps_c = block + c
            dets = []
            for name, dd in (("H1", h), ("L1", l)):
                i0 = int((gps_c - PSD_S / 2 - dd["t0"]) * fs)
                x = dd["x"][i0:i0 + int(PSD_S * fs)]
                if len(x) < PSD_S * fs or not np.isfinite(x).all():
                    break
                dets.append(search.Detector(name, dd["t0"] + i0 / fs, 1 / fs, x, gps_c, seg=SEG_S))
            if len(dets) < 2:
                continue
            net, peak, pooled = search.search(dets, bank)
            bg_new.append(search.background(pooled, list(DETS), n_slides=SLIDES))
            st["bg_time_s"] += SLIDES * STEP_S
            st["analysed_s"] += STEP_S
            st["segments"] += 1
            bi = int(np.argmax(net))
            if net[bi] >= TRIGGER_SNR:
                t = self._describe(dets, bank[bi], peak[bi], float(net[bi]), events, block, inj_free)
                # keep the loudest trigger within 1 s (windows overlap)
                dup = [x for x in trig if abs(x["gps"] - t["gps"]) < 1.0]
                if not dup or dup[0]["net_snr"] < t["net_snr"]:
                    trig = [x for x in trig if abs(x["gps"] - t["gps"]) >= 1.0] + [t]
                    log(f"Trigger at GPS {t['gps']:.2f}: network SNR {t['net_snr']:.1f}"
                        + (f" — matches {t['known']}" if t["known"] else ""))
            if k % 10 == 0 and getattr(self, "progress", None):
                self.progress(f"Block {block}: window {k + 1}/{len(centers)} · {st['analysed_s'] / 3600:.2f} h analysed")
        if bg_new:
            bg = np.concatenate([self.background(), np.concatenate(bg_new).astype(np.float32)])
            np.save(self.dir / "background.npy", bg)
        (self.dir / "triggers.json").write_text(json.dumps(trig))

    def _describe(self, dets, mm, peak_idx, net, events, block, inj_free):
        m1, m2 = mm
        ref = dets[0]
        t_peak = ref.t0 + peak_idx * ref.dt
        h = search.cached_template(ref.f, m1, m2)
        per = []
        for d in dets:
            z, sigma = d.filter(h)
            i_ref = int(round((t_peak - d.t0) / d.dt))
            j = int((search.TRAVEL_S[d.name] + 0.002) / d.dt) if d is not ref else int(0.002 / d.dt)
            i = max(0, i_ref - j) + int(np.argmax(np.abs(z[max(0, i_ref - j):i_ref + j + 1])))
            rho = float(np.abs(z[i]))
            chi_r, _ = search.chisq(d, h, i)
            per.append({"det": d.name, "snr": round(rho, 2), "dt_ms": round((d.t0 + i * d.dt - t_peak) * 1000, 2),
                        "chi_r": round(chi_r, 2), "chi_allow": round(search.chi_allowance(rho), 2)})
        cat = [n for g, n, c in events if abs(g - t_peak) < 1.0]
        sec = int(t_peak - block)
        inj = not bool(inj_free[min(max(sec, 0), len(inj_free) - 1)])
        glitchy = any(p["chi_r"] > 2.5 * p["chi_allow"] for p in per if p["snr"] > 5)
        return {"gps": round(t_peak, 4), "net_snr": round(net, 2), "m1": round(m1, 2), "m2": round(m2, 2),
                "mchirp": round(W.chirp_mass(m1, m2), 2), "per": per, "known": cat[0] if cat else None,
                "injection": inj, "glitch_like": glitchy, "gates": sum(len(d.gates) for d in dets),
                "found": time.time()}
