"""Turning a candidate into something you can share: checks, report package, Zenodo deposit."""
import io
import json
import textwrap
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

from . import survey

REPO_URL = "https://github.com/Imshad18/gw-detector"


# ---------------------------------------------------------------- checks
def dq_at(det, gps):
    """Data-quality and hardware-injection bits for the second containing gps (reads 2 small arrays)."""
    import fsspec
    import h5py
    from gwosc.locate import get_urls
    try:
        urls = [u for u in get_urls(det, gps, gps + 1, sample_rate=4096) if u.endswith(".hdf5")]
        with fsspec.open(urls[0], "rb", block_size=2 ** 16) as fh, h5py.File(fh) as f:
            t0 = int(f["meta/GPSstart"][()])
            i = int(gps - t0)
            return {"dq": int(f["quality/simple/DQmask"][i]), "inj": int(f["quality/injections/Injmask"][i])}
    except Exception:
        return None


def survey_trigger(gps):
    """The survey trigger (with false-alarm rate) at this time, if any survey found it."""
    if not survey.ROOT.exists():
        return None
    for d in survey.ROOT.iterdir():
        sv = survey.Survey(d.name)
        if not sv.state_path.exists():
            continue
        for t in sv.triggers():
            if abs(t["gps"] - gps) < 0.5:
                far, ul = sv.far(t["net_snr"])
                st = sv.state()
                return {**t, "survey": d.name, "far_per_year": far, "far_upper_limit": ul,
                        "survey_hours": st["analysed_s"] / 3600, "bg_years": st["bg_time_s"] / survey.YEAR}
    return None


def checks(res):
    gps = res["best"]["t_peak"]
    per = res["per_detector"]
    out = []

    def add(name, status, value, detail):
        out.append({"name": name, "status": status, "value": value, "detail": detail})

    n_coinc = sum(p["snr"] >= 4 for p in per)
    add("Coincident in two or more detectors", "pass" if n_coinc >= 2 else "fail", f"{n_coinc} detectors",
        "A real signal reaches each LIGO site within 10 ms; one loud detector alone is usually a glitch.")
    snr = res["best"]["net_snr"]
    add("Network SNR", "pass" if snr >= 9 else "warn" if snr >= 7.5 else "fail", f"{snr:.1f}",
        "Published searches typically need about 8–10 for a confident detection.")
    bad = [p["det"] for p in per if p["snr"] > 5 and p["chi_r"] is not None and p["chi_r"] > 2.5 * p["chi_allow"]]
    add("Signal shape (χ²)", "fail" if bad else "pass", "inconsistent in " + ", ".join(bad) if bad else "consistent",
        "The SNR must build up across frequency like a merger, not appear in one band like a glitch.")
    gated = sum(1 for p in per for g in p.get("gates", []) if abs(g["t_rel"] - (gps - res["gps"])) < 4)
    add("No glitch gating near the candidate", "warn" if gated else "pass", f"{gated} gates",
        "Data near removed glitches is less trustworthy.")

    dq = {d: dq_at(d, gps) for d in res["detectors"]}
    if all(v is not None for v in dq.values()):
        dq_ok = all((v["dq"] & 0b111) == 0b111 for v in dq.values())
        inj = [d for d, v in dq.items() if not (v["inj"] & 1)]
        add("Data quality (CBC categories 1 and 2)", "pass" if dq_ok else "fail", "passes" if dq_ok else "flagged",
            "LIGO's detector-characterisation flags for this second of data.")
        add("Not a hardware injection", "fail" if inj else "pass", "injection in " + ", ".join(inj) if inj else "none",
            "LIGO deliberately injects fake signals to test the instruments. These are flagged in the open data.")
    else:
        add("Data quality / injections", "warn", "unknown", "Could not read the data-quality flags from GWOSC.")

    xm = survey.crossmatch(gps)
    if xm["catalog"]:
        add("Not in a published catalogue", "info", xm["catalog"][0]["name"],
            f"Already published in {xm['catalog'][0]['catalog']}. Re-detecting it validates the method but is not new.")
    else:
        add("Not in a published catalogue", "pass", "no match", "No GWOSC catalogue event (confident or marginal) within 1 s.")
    if xm["gracedb"] is None:
        add("Not a public alert", "warn", "unknown", "GraceDB could not be reached.")
    elif xm["gracedb"]:
        add("Not a public alert", "info", xm["gracedb"][0]["id"], "LIGO/Virgo/KAGRA issued a public alert at this time.")
    else:
        add("Not a public alert", "pass", "no match", "No public GraceDB superevent within 2 s.")

    st = survey_trigger(gps)
    if st and st["far_per_year"] is not None:
        far = st["far_per_year"]
        if st["far_upper_limit"] and far > 1:
            status = "warn"  # louder than all background: the survey is simply too short to say more
        else:
            status = "pass" if far <= 1 else "warn" if far <= 12 else "fail"
        add("False-alarm rate", status, f"{'< ' if st['far_upper_limit'] else ''}{far:.3g} / yr",
            f"From {st['bg_years']:.2f} years of time-slide background in survey {st['survey']}. "
            + ("Louder than all background so far; survey more hours of data to push this limit down. " if st["far_upper_limit"] else "")
            + "Below 1 per year is interesting; published discoveries are usually far below that.")
    else:
        bg = res["background"]
        add("False-alarm rate", "warn", "local only",
            f"Only {bg['live_days']:.2f} days of local background. Run a survey over this period to measure a real false-alarm rate.")

    known = bool(xm["catalog"] or xm["gracedb"])
    fails = [c for c in out if c["status"] == "fail"]
    if known:
        verdict = ("known", "Known event", "This is a published event or public alert. It's good validation, but not a new discovery.")
    elif fails:
        verdict = ("fail", "Not a credible candidate", "Fails: " + ", ".join(c["name"] for c in fails) + ".")
    elif any(c["status"] == "warn" for c in out):
        verdict = ("warn", "Possible candidate, needs more evidence", "Not in any catalogue, but some checks are inconclusive.")
    else:
        verdict = ("pass", "New candidate", "Not in any catalogue or public alert and passes all checks. Worth writing up.")
    return {"checks": out, "crossmatch": xm, "survey": st,
            "verdict": {"tone": verdict[0], "label": verdict[1], "text": verdict[2]}}


# ---------------------------------------------------------------- figures
def _figures(res, chk):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "font.family": "DejaVu Sans", "axes.spines.top": False, "axes.spines.right": False})
    colors = {"H1": "#c0392b", "L1": "#2c6fbb", "V1": "#8e44ad"}
    figs = {}

    def save(name, fig):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        figs[name] = buf.getvalue()

    per = res["per_detector"]
    fig, axes = plt.subplots(len(per), 1, figsize=(6.5, 1.9 * len(per)), sharex=True)
    for ax, p in zip(np.atleast_1d(axes), per):
        ax.plot(p["whitened"]["t"], p["whitened"]["data"], color="0.6", lw=0.6, label="data")
        ax.plot(p["whitened"]["t"], p["whitened"]["model"], color=colors[p["det"]], lw=1.2, label="template")
        ax.set_xlim(-0.35, 0.1)
        ax.set_ylabel(f"{p['det']} whitened")
    np.atleast_1d(axes)[-1].set_xlabel("Time from merger (s)")
    np.atleast_1d(axes)[0].legend(frameon=False, loc="upper left")
    save("strain.png", fig)

    fig, axes = plt.subplots(1, len(per), figsize=(3.3 * len(per), 2.6), sharey=True)
    for ax, p in zip(np.atleast_1d(axes), per):
        q = p["qscan"]
        ax.pcolormesh(q["t"], q["f"], np.array(q["e"]), cmap="Oranges", vmin=0, vmax=25, shading="auto")
        ax.set_yscale("log")
        ax.set_xlim(-0.5, 0.15)
        ax.set_title(p["det"], fontsize=9)
        ax.set_xlabel("Time from merger (s)")
    np.atleast_1d(axes)[0].set_ylabel("Frequency (Hz)")
    save("qscan.png", fig)

    fig, ax = plt.subplots(figsize=(4.5, 2.6))
    for p in per:
        ax.plot(p["snr_series"]["t"], p["snr_series"]["snr"], color=colors[p["det"]], lw=0.8, label=p["det"])
    ax.set_xlabel("Time from peak (s)")
    ax.set_ylabel("Matched-filter SNR")
    ax.legend(frameon=False)
    save("snr.png", fig)

    st = chk.get("survey")
    fig, ax = plt.subplots(figsize=(4.5, 2.6))
    if st:
        sv = survey.Survey(st["survey"])
        bg = sv.background()
        x = np.sort(bg)[::-1]
        years = sv.state()["bg_time_s"] / survey.YEAR
        ax.step(x, np.arange(1, len(x) + 1) / years, color="0.4", where="post", label="time-slide background")
        ax.set_yscale("log")
        ax.set_ylabel("False-alarm rate (per year)")
    elif res["background"]["slides"]:
        ax.hist(res["background"]["slides"], bins=40, color="0.6")
        ax.set_ylabel("Time slides")
    ax.axvline(res["best"]["net_snr"], color="#b4530f", lw=1.5, label="candidate")
    ax.set_xlabel("Network SNR")
    ax.legend(frameon=False)
    save("background.png", fig)
    return figs


# ---------------------------------------------------------------- texts
def _tex_escape(s):
    return str(s).replace("\\", r"\textbackslash{}").replace("&", r"\&").replace("%", r"\%").replace("_", r"\_").replace("#", r"\#")


def paper_tex(res, chk, author):
    b, pe = res["best"], res["pe"]
    st = chk.get("survey")
    utc = datetime.fromtimestamp(b["t_peak"] + 315964800 - 18, timezone.utc)
    far_txt = (f"{'less than ' if st['far_upper_limit'] else ''}{st['far_per_year']:.2g} per year, estimated from "
               f"{st['bg_years']:.2f} years of time-slide background over {st['survey_hours']:.1f} hours of analysed data"
               if st and st["far_per_year"] is not None else
               f"not yet measured beyond a local estimate ({res['background']['louder']} of {len(res['background']['slides'] or [])} time slides louder)")
    rows = "\n".join(f"{p['det']} & {p['snr']:.1f} & {p['dt_ms']:+.1f} & {p['chi_r']:.2f} & {p['deff_mpc']:.0f} \\\\" for p in res["per_detector"])
    checks_tex = "\n".join(f"\\item {_tex_escape(c['name'])}: {_tex_escape(c['value'])}." for c in chk["checks"])
    name = _tex_escape(author.get("name") or "Author Name")
    aff = _tex_escape(author.get("affiliation") or "Independent researcher")
    email = _tex_escape(author.get("email") or "")
    cand = f"GW{utc:%y%m%d}\\_{utc:%H%M%S}"
    xm = chk["crossmatch"]
    known_name = (xm["catalog"][0]["name"] if xm["catalog"] else xm["gracedb"][0]["id"] if xm["gracedb"] else None)
    if known_name:
        title = f"An independent re-detection of {_tex_escape(known_name)} in open LIGO data"
        novelty = (f"The signal coincides with the catalogued event {_tex_escape(known_name)}; this note documents an independent "
                   "re-detection with a from-scratch search pipeline.")
        what = "a known gravitational-wave event"
    else:
        title = f"A candidate binary black hole merger at GPS {b['t_peak']:.3f} in open LIGO data"
        novelty = "The candidate does not coincide with any event in the GWOSC catalogues or with a public LIGO--Virgo--KAGRA alert."
        what = "a candidate gravitational-wave signal"
    tex = rf"""
    \documentclass[11pt]{{article}}
    \usepackage[margin=1in]{{geometry}}
    \usepackage{{graphicx, amsmath, hyperref}}
    \title{{{title}}}
    \author{{{name}\\ \small {aff}{(r' \\ \small \texttt{' + email + '}') if email else ''}}}
    \date{{{datetime.now():%B %Y}}}
    \begin{{document}}
    \maketitle

    \begin{{abstract}}
    We report {what}, {cand}, identified in public strain data from the
    {_tex_escape(', '.join(res['detectors']))} detectors released by the Gravitational Wave Open Science Center (GWOSC).
    A matched-filter search with non-spinning compact binary templates finds a coincident signal with
    network signal-to-noise ratio {b['net_snr']:.1f}, consistent with a binary of detector-frame chirp mass
    {pe['mc']:.1f}~$M_\odot$ ({pe['mc_lo']:.1f}--{pe['mc_hi']:.1f}). Its false-alarm rate is {far_txt}.
    {novelty}
    \end{{abstract}}

    \section{{Introduction}}
    Public release of gravitational-wave strain data has allowed independent searches to identify
    additional compact binary mergers beyond those reported by the LIGO--Virgo--KAGRA collaboration
    \cite{{gwtc3, ias, ogc4}}. Here we describe a candidate found by an independent search of GWOSC data \cite{{gwosc}}.

    \section{{Data and method}}
    We analysed 4096~Hz strain data, restricted to times passing the CBC category 1 and 2 data-quality
    flags and free of hardware injections. Data were high-pass filtered at 15~Hz; loud transients above
    10$\sigma$ in whitened data were removed with tapered gates, and filter output within 2~s of a gate was discarded.
    The noise power spectral density was estimated with Welch's method (4~s segments, median average)
    over 128~s around each 64~s analysis window.

    The template bank contains {len(res['bank']['m1'])} non-spinning templates with component masses 5--100~$M_\odot$, using the
    TaylorF2 3.5PN inspiral phase \cite{{taylorf2}} with a phenomenological merger--ringdown amplitude. Triggers
    require SNR~$\geq 4$ in at least two detectors within the inter-site light travel time. The ranking statistic is the
    quadrature sum of single-detector SNRs. Signal consistency is tested with the $\chi^2$ discriminator of
    Allen \cite{{allen}}. Background is estimated with time slides of one detector relative to the others.
    Code: \url{{{REPO_URL}}}.

    \section{{Results}}
    The candidate peaks at GPS {b['t_peak']:.4f} ({utc:%Y-%m-%d %H:%M:%S} UTC). Table~\ref{{tab:det}} lists single-detector results.
    The best-fitting template has detector-frame component masses {b['m1']:.0f} and {b['m2']:.0f}~$M_\odot$.
    \begin{{table}}[h]\centering
    \begin{{tabular}}{{lrrrr}}
    Detector & SNR & $\Delta t$ (ms) & $\chi^2_r$ & $D_\mathrm{{eff}}$ (Mpc) \\ \hline
    {rows}
    \end{{tabular}}
    \caption{{Single-detector matched-filter results for the best template.}}\label{{tab:det}}
    \end{{table}}

    \begin{{figure}}[h]\centering
    \includegraphics[width=0.85\linewidth]{{figures/strain.png}}
    \caption{{Whitened, band-passed strain (grey) with the best-fit template.}}
    \end{{figure}}
    \begin{{figure}}[h]\centering
    \includegraphics[width=0.95\linewidth]{{figures/qscan.png}}
    \caption{{Constant-Q time--frequency maps of whitened data.}}
    \end{{figure}}
    \begin{{figure}}[h]\centering
    \includegraphics[width=0.48\linewidth]{{figures/snr.png}}\hfill
    \includegraphics[width=0.48\linewidth]{{figures/background.png}}
    \caption{{Left: SNR time series. Right: background distribution with the candidate marked.}}
    \end{{figure}}

    \subsection*{{Checks}}
    \begin{{itemize}}
    {checks_tex}
    \end{{itemize}}

    \section{{Discussion}}
    The search uses non-spinning templates and an approximate merger model, so the recovered SNR and masses
    are conservative estimates. A dedicated analysis with full inspiral--merger--ringdown waveforms, parameter
    estimation and a longer background would be required to establish the astrophysical origin of this candidate.

    \section*{{Acknowledgements}}
    This research has made use of data obtained from the Gravitational Wave Open Science Center (gwosc.org),
    a service of the LIGO Scientific Collaboration, the Virgo Collaboration and KAGRA.
    (Replace this with the full acknowledgement text from gwosc.org/acknowledge before submitting.)

    \begin{{thebibliography}}{{9}}
    \bibitem{{gwosc}} R. Abbott et al. (LIGO Scientific Collaboration and Virgo Collaboration), SoftwareX 13, 100658 (2021).
    \bibitem{{gwtc3}} R. Abbott et al. (LIGO Scientific, Virgo and KAGRA Collaborations), Phys. Rev. X 13, 041039 (2023).
    \bibitem{{ias}} T. Venumadhav, B. Zackay, J. Roulet, L. Dai, M. Zaldarriaga, Phys. Rev. D 101, 083030 (2020).
    \bibitem{{ogc4}} A. H. Nitz et al., Astrophys. J. 946, 59 (2023).
    \bibitem{{taylorf2}} A. Buonanno, B. R. Iyer, E. Ochsner, Y. Pan, B. S. Sathyaprakash, Phys. Rev. D 80, 084043 (2009).
    \bibitem{{allen}} B. Allen, Phys. Rev. D 71, 062001 (2005).
    \end{{thebibliography}}
    \end{{document}}
    """
    # remove the source-code indentation (interpolated multi-line values break textwrap.dedent)
    return "\n".join(line[4:] if line.startswith("    ") else line for line in tex.strip("\n").splitlines()) + "\n"


def email_text(res, chk, author):
    b, pe = res["best"], res["pe"]
    st = chk.get("survey")
    far = (f"{'<' if st['far_upper_limit'] else ''}{st['far_per_year']:.2g}/yr" if st and st["far_per_year"] is not None else "local estimate only")
    return textwrap.dedent(f"""
    Subject: Candidate compact binary signal at GPS {b['t_peak']:.3f} in open {'/'.join(res['detectors'])} data

    Dear colleagues,

    While running an independent matched-filter search of GWOSC open data I found a coincident trigger
    that does not match any catalogued event or public alert:

      GPS time            {b['t_peak']:.4f}
      Detectors           {', '.join(f"{p['det']} SNR {p['snr']:.1f}" for p in res['per_detector'])}
      Network SNR         {b['net_snr']:.1f}
      Chirp mass (det.)   {pe['mc']:.1f} Msun ({pe['mc_lo']:.1f}-{pe['mc_hi']:.1f})
      False-alarm rate    {far}

    Checks: {'; '.join(f"{c['name']}: {c['value']}" for c in chk['checks'])}.

    The analysis code is public ({REPO_URL}) and the full candidate package (figures, data, a short
    write-up and a reproduction script) is attached{' and archived at <DOI>' if True else ''}.

    I would be grateful for any feedback on whether this time is known to be affected by instrumental issues,
    or whether it merits further follow-up.

    Best regards,
    {author.get('name') or '<your name>'}
    {author.get('affiliation') or ''}
    {author.get('email') or ''}
    """).strip() + "\n"


def readme_text(res, chk):
    b = res["best"]
    return textwrap.dedent(f"""
    # Gravitational-wave candidate at GPS {b['t_peak']:.3f}

    Verdict: {chk['verdict']['label']} — {chk['verdict']['text']}

    ## Contents
    - `paper/main.tex` and `paper/figures/` — short write-up (compiles with pdflatex; also ready for arXiv upload)
    - `candidate.json` — full analysis output and checks
    - `email.txt` — draft message to the LIGO-Virgo-KAGRA collaboration or a search group
    - `reproduce.sh` — re-runs the analysis from the public data

    ## Checks
    {chr(10).join(f"- [{c['status']}] {c['name']}: {c['value']}" for c in chk['checks'])}

    Generated by {REPO_URL} on {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.
    """).strip() + "\n"


def reproduce_sh(res):
    return textwrap.dedent(f"""
    #!/usr/bin/env bash
    # Re-run the analysis of this candidate from GWOSC open data
    set -e
    git clone {REPO_URL} gw-detector && cd gw-detector
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python -c "from gwdetect import pipeline; r = pipeline.run('{res['best']['t_peak']:.3f}', lambda p, m: print(m)); print(r['verdict'])"
    """).strip() + "\n"


def build_package(res, author):
    chk = checks(res)
    figs = _figures(res, chk)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", readme_text(res, chk))
        z.writestr("paper/main.tex", paper_tex(res, chk, author))
        for name, data in figs.items():
            z.writestr(f"paper/figures/{name}", data)
        z.writestr("candidate.json", json.dumps({"analysis": res, "checks": chk}, indent=1, default=str))
        z.writestr("email.txt", email_text(res, chk, author))
        z.writestr("reproduce.sh", reproduce_sh(res))
    return buf.getvalue(), chk


# ---------------------------------------------------------------- Zenodo
def _zbase(sandbox):
    return "https://sandbox.zenodo.org/api" if sandbox else "https://zenodo.org/api"


def zenodo_draft(res, author, token, sandbox=True):
    """Create a Zenodo draft with the candidate package. Nothing is public until zenodo_publish."""
    pkg, chk = build_package(res, author)
    base = _zbase(sandbox)
    auth = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{base}/deposit/depositions", json={}, headers=auth, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Zenodo refused the request ({r.status_code}): {r.text[:200]}")
    dep = r.json()
    b = res["best"]
    fname = f"gw_candidate_{b['t_peak']:.0f}.zip"
    r = requests.put(f"{dep['links']['bucket']}/{fname}", data=pkg, headers=auth, timeout=300)
    if r.status_code >= 300:
        raise RuntimeError(f"Upload failed ({r.status_code}): {r.text[:200]}")
    name = author.get("name") or "Unknown"
    parts = name.strip().split()
    creator = {"name": f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name}
    if author.get("affiliation"):
        creator["affiliation"] = author["affiliation"]
    if author.get("orcid"):
        creator["orcid"] = author["orcid"]
    meta = {"metadata": {
        "upload_type": "dataset",
        "title": f"Candidate gravitational-wave signal at GPS {b['t_peak']:.3f} in open LIGO data",
        "creators": [creator],
        "description": (f"<p>Independent matched-filter search result. Network SNR {b['net_snr']:.1f}, "
                        f"detector-frame chirp mass {res['pe']['mc']:.1f} Msun. Verdict: {chk['verdict']['label']}.</p>"
                        f"<p>Contains a short write-up, figures, full analysis output and a reproduction script. "
                        f"Data from the Gravitational Wave Open Science Center (gwosc.org).</p>"),
        "keywords": ["gravitational waves", "LIGO", "compact binary", "open data", "matched filter"],
        "license": "cc-by-4.0",
        "related_identifiers": [{"identifier": REPO_URL, "relation": "isSupplementedBy", "resource_type": "software"}],
    }}
    r = requests.put(f"{base}/deposit/depositions/{dep['id']}", json=meta, headers=auth, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Metadata rejected ({r.status_code}): {r.text[:300]}")
    d = r.json()
    return {"id": d["id"], "html": d["links"].get("html"), "doi": d.get("metadata", {}).get("prereserve_doi", {}).get("doi"),
            "sandbox": sandbox, "verdict": chk["verdict"]}


def zenodo_publish(dep_id, token, sandbox=True):
    base = _zbase(sandbox)
    r = requests.post(f"{base}/deposit/depositions/{dep_id}/actions/publish",
                      headers={"Authorization": f"Bearer {token}"}, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"Publish failed ({r.status_code}): {r.text[:300]}")
    d = r.json()
    return {"doi": d.get("doi"), "url": d.get("doi_url") or d["links"].get("html"), "record": d["links"].get("record_html")}
