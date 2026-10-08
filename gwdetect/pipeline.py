"""Analyse one event (or any GPS time) end to end."""
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import data, search
from . import waveform as W

RESULTS = Path(__file__).resolve().parent.parent / "results"


def _r(x, n=4):
    return [round(float(v), n) for v in x]


def run(target, progress=None):
    t_start = time.time()
    log = []

    def step(pct, msg):
        log.append(msg)
        if progress:
            progress(pct, msg)

    # 1. What and when ----------------------------------------------------------
    cat = None
    try:
        gps = float(target)
        name = f"GPS {gps:.1f}"
        events = data.catalog()
        near = [e for e in events if abs(e["gps"] - gps) < 1]
        cat = near[0] if near else None
    except ValueError:
        events = data.catalog()
        match = [e for e in events if e["name"].lower() == str(target).strip().lower()]
        if not match:
            match = [e for e in events if e["name"].lower().startswith(str(target).strip().lower())]
        if not match:
            raise ValueError(f"{target} is not in the GWOSC event list")
        cat = match[0]
        gps, name = cat["gps"], cat["name"]
    step(2, f"{name}: GPS {gps:.2f}" + (f" (catalogue {cat['catalog']})" if cat else " (no catalogued event)"))

    # 2. Data ---------------------------------------------------------------------
    step(4, "Checking which detectors have open data…")
    names = data.event_detectors(gps)
    if not names:
        raise ValueError("No open strain data from LIGO or Virgo at this time")
    step(6, "Open data from " + ", ".join(names))
    dets = []
    for k, dn in enumerate(names):
        step(8 + 22 * k / len(names), f"Downloading {dn} strain (128 s at 4096 Hz)…")
        s = data.strain(dn, gps, log=lambda m: step(8 + 22 * k / len(names), m))
        if s is None:
            continue
        dets.append(search.Detector(dn, s[0], s[1], s[2], gps))
    if not dets:
        raise ValueError("All detectors had data gaps around this time")
    for d in dets:
        for g in d.gates:
            step(30, f"{d.name}: gated a glitch at {g['t'] - gps:+.2f} s ({g['peak_sigma']:.0f}σ whitened, {g['width_s']:.2f} s removed)")
    step(30, "Estimated noise spectra (Welch, 4 s segments, median)")

    # 3. Template bank search -------------------------------------------------------
    bank = search.template_bank()
    step(32, f"Matched filtering {len(bank)} templates (5–100 M☉) in {len(dets)} detector(s)…")
    net, peak, pooled = search.search(dets, bank, progress=lambda f, m: step(32 + 38 * f, m))
    bi = int(np.argmax(net))
    m1, m2 = bank[bi]
    ref = dets[0]
    t_peak = ref.t0 + peak[bi] * ref.dt
    step(70, f"Loudest: network SNR {net[bi]:.1f} for {m1:.1f} + {m2:.1f} M☉ at GPS {t_peak:.3f}")

    # 4. Per-detector measurements ---------------------------------------------------
    per = []
    for d in dets:
        h = W.template(d.f, m1, m2, f_low=search.F_LOW)
        z, sigma = d.filter(h)
        i_ref = int(round((t_peak - d.t0) / d.dt))
        j = int(search.TRAVEL_S.get(d.name, 0.03) / d.dt) + int(0.002 / d.dt)
        if d is ref:
            j = int(0.002 / d.dt)
        w0, w1 = max(0, i_ref - j), i_ref + j + 1
        i = w0 + int(np.argmax(np.abs(z[w0:w1])))
        rho = float(np.abs(z[i]))
        chi_r, _ = search.chisq(d, h, i)
        tc = d.t0 + i * d.dt
        # best-fit signal in this detector, for the whitened overlay
        A = z[i] / sigma
        model_f = A * h * np.exp(-2j * np.pi * d.f * (i * d.dt))
        data_w = d.whiten(d.d)
        model_w = d.whiten(model_f)
        span = int(0.6 / d.dt)
        sl = slice(i - span, i + span)
        aud = slice(i - int(1.6 / d.dt), i + int(0.4 / d.dt))
        scale = np.abs(data_w[aud]).max() or 1
        qf, qt, qe = search.qscan(d, i * d.dt)
        zs = np.abs(z[i - int(1 / d.dt):i + int(1 / d.dt):8])
        asd_mask = (d.psd_f >= 10) & (d.psd_f <= 2000)
        af, aa = d.psd_f[asd_mask], np.sqrt(d.psd[asd_mask])
        pick = np.unique(np.geomspace(1, len(af) - 1, 500).astype(int))
        per.append({
            "det": d.name, "snr": rho, "time": tc, "dt_ms": (tc - t_peak) * 1000,
            "gates": [{**g, "t_rel": g["t"] - gps} for g in d.gates], "phase": float(np.angle(z[i])), "chi_r": chi_r, "chi_allow": search.chi_allowance(rho), "rw_snr": search.reweighted(rho, chi_r),
            "deff_mpc": float(sigma / rho) if rho > 0 else None,
            "asd": {"f": _r(af[pick], 2), "a": [float(f"{v:.3e}") for v in aa[pick]]},
            "whitened": {"t": _r((np.arange(-span, span) * d.dt), 5), "data": _r(data_w[sl], 3), "model": _r(model_w[sl], 3)},
            "snr_series": {"t": _r(np.arange(len(zs)) * 8 * d.dt - 1, 4), "snr": _r(zs, 2)},
            "qscan": {"f": _r(qf, 1), "t": _r(qt, 4), "e": np.clip(qe, 0, 60).round(1).tolist()},
            "audio": {"rate": int(round(1 / d.dt)), "data": _r(data_w[aud] / scale, 3), "model": _r(model_w[aud] / scale, 3)},
        })
        step(72, f"{d.name}: SNR {rho:.1f}, Δt {(tc - t_peak) * 1000:+.1f} ms, χ²/dof {chi_r:.2f}, "
             f"effective distance {sigma / rho if rho else 0:.0f} Mpc")

    net_snr = float(np.sqrt(sum(p["snr"] ** 2 for p in per)))
    net_rw = float(np.sqrt(sum(p["rw_snr"] ** 2 for p in per)))

    # 5. Background -----------------------------------------------------------------
    bg = None
    if len(dets) >= 2:
        step(76, "Measuring background with 400 time slides…")
        bg = search.background(pooled, [d.name for d in dets])
        louder = int((bg >= net[bi]).sum())
        live = (ref.n * ref.dt - 2 * search.EDGE_S) * len(bg)
        fap = (louder + 1) / (len(bg) + 1)
        step(80, f"Background: {louder} of {len(bg)} slides as loud as the candidate → "
             f"false-alarm probability ≤ {fap:.3g} ({live / 86400:.2f} days of background)")
    else:
        louder, fap, live = None, None, 0

    # 6. Chirp mass ------------------------------------------------------------------
    mc_det = W.chirp_mass(m1, m2)
    step(82, "Estimating chirp mass with inspiral-only templates…")
    pe = search.chirp_mass_scan(dets, t_peak, mc_det, progress=lambda f, m: step(82 + 14 * f, m))
    z_red = cat.get("redshift") if cat else None
    pe["mc_src"] = pe["mc"] / (1 + z_red) if z_red else None
    step(96, f"Detector-frame chirp mass {pe['mc']:.1f} M☉ ({pe['mc_lo']:.1f}–{pe['mc_hi']:.1f})")

    # 7. Verdict ---------------------------------------------------------------------
    chi_ok = all(p["chi_r"] < 2.5 * search.chi_allowance(p["snr"]) for p in per if p["snr"] > 5 and np.isfinite(p["chi_r"]))
    coinc = sum(p["snr"] >= 5 for p in per)
    if len(dets) >= 2 and net_snr >= 9 and louder == 0 and chi_ok and coinc >= 2:
        label, tone = "Detected", "pass"
        text = (f"Coincident signal in {coinc} detectors, louder than all {len(bg)} background trials, "
                "and the signal shape matches a binary merger.")
    elif net_snr >= 9 and not chi_ok:
        label, tone = "Glitch-like", "warn"
        text = "Loud, but the χ² test says the power isn't distributed like a merger signal (likely an instrumental glitch)."
    elif len(dets) >= 2 and net_snr >= 7.5 and louder is not None and fap < 0.05:
        label, tone = "Marginal", "warn"
        text = "Above most background, but not by a wide margin."
    elif len(dets) < 2 and net_snr >= 9 and chi_ok:
        label, tone = "Single-detector trigger", "warn"
        text = "Strong and signal-like, but with one detector significance can't be measured by time slides."
    else:
        label, tone = "No significant signal", "fail"
        text = "Nothing stands out above the noise with these templates."
    if cat and tone in ("fail", "warn"):
        text += " Quieter catalogued events and events with spin, precession or very unequal masses can be missed by this simple bank."

    result = {
        "name": name, "gps": gps, "catalog": cat, "detectors": [d.name for d in dets],
        "created": datetime.now().isoformat(timespec="seconds"),
        "best": {"m1": m1, "m2": m2, "mc": mc_det, "t_peak": t_peak, "net_snr": net_snr, "net_rw": net_rw,
                 "offset_ms": (t_peak - gps) * 1000},
        "per_detector": per,
        "background": {"slides": None if bg is None else _r(np.sort(bg), 2), "louder": louder, "fap": fap,
                       "live_days": live / 86400},
        "pe": pe,
        "bank": {"m1": _r([b[0] for b in bank], 2), "m2": _r([b[1] for b in bank], 2), "snr": _r(net, 2)},
        "verdict": {"label": label, "tone": tone, "text": text},
        "runtime_s": round(time.time() - t_start, 1), "log": log,
    }
    RESULTS.mkdir(exist_ok=True)
    fname = f"{name.replace(' ', '_')}_{datetime.now():%Y%m%d_%H%M%S}.json"
    (RESULTS / fname).write_text(json.dumps(_clean(result)))
    result["file"] = fname
    step(100, f"Done in {result['runtime_s']} s: {label}")
    return result


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o
