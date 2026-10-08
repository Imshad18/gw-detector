"""GW Detector web server.

Run:  python server.py   then open http://localhost:8002
"""
import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gwdetect import contribute, data, pipeline, survey

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="GW Detector")
app.add_middleware(GZipMiddleware, minimum_size=2000)
jobs = {}
job_queue = queue.Queue()


class JobRequest(BaseModel):
    target: str


def worker():
    while True:
        jid = job_queue.get()
        job = jobs[jid]
        job["status"] = "running"

        def progress(pct, msg):
            job["pct"] = round(pct, 1)
            job["log"].append(msg)

        try:
            res = pipeline.run(job["target"], progress)
            job.update(status="done", file=res["file"], verdict=res["verdict"]["label"], tone=res["verdict"]["tone"])
        except Exception as exc:
            traceback.print_exc()
            job.update(status="error", error=str(exc))
            job["log"].append(f"ERROR: {exc}")


threading.Thread(target=worker, daemon=True).start()


@app.get("/api/events")
def events():
    return data.catalog()


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if not req.target.strip():
        raise HTTPException(400, "Empty target")
    jid = uuid.uuid4().hex[:10]
    jobs[jid] = {"id": jid, "target": req.target.strip(), "status": "queued", "pct": 0, "log": [], "created": time.time()}
    job_queue.put(jid)
    return jobs[jid]


@app.get("/api/jobs")
def list_jobs():
    return sorted(({k: v for k, v in j.items() if k != "log"} for j in jobs.values()), key=lambda j: -j["created"])


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    if jid not in jobs:
        raise HTTPException(404)
    return jobs[jid]


@app.get("/api/results")
def list_results():
    out = []
    for p in sorted(pipeline.RESULTS.glob("*.json"), key=lambda p: -p.stat().st_mtime):
        try:
            r = json.loads(p.read_text())
            out.append({"file": p.name, "name": r["name"], "created": r["created"], "label": r["verdict"]["label"],
                        "tone": r["verdict"]["tone"], "snr": r["best"]["net_snr"]})
        except Exception:
            continue
    return out


@app.get("/api/results/{name}")
def get_result(name: str):
    p = pipeline.RESULTS / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/json")


# ------------------------------------------------------------------ surveys
runner = {"survey": None, "thread": None, "msg": ""}


class SurveyRequest(BaseModel):
    run: str
    start: int | None = None
    hours: float = 6


def _survey_view(sid):
    sv = survey.Survey(sid)
    st = sv.state()
    bg = sv.background()
    trig = sorted(sv.triggers(), key=lambda t: -t["net_snr"])
    for t in trig:
        t["far_per_year"], t["far_upper_limit"] = sv.far(t["net_snr"], bg, st)
    hist = np.histogram(bg, bins=np.arange(5, 30.5, 0.5))[0].tolist() if len(bg) else []
    running = runner["survey"] is not None and runner["survey"].sid == sid and runner["thread"] and runner["thread"].is_alive()
    return {**{k: v for k, v in st.items() if k not in ("data_segments",)}, "running": running, "triggers": trig,
            "bg_hist": hist, "bg_years": st["bg_time_s"] / survey.YEAR, "now_msg": runner["msg"] if running else ""}


@app.get("/api/runs")
def runs():
    return [{"run": k, "start": a, "end": b} for k, (a, b) in survey.RUNS.items()]


@app.get("/api/surveys")
def list_surveys():
    out = []
    if survey.ROOT.exists():
        for d in sorted(survey.ROOT.iterdir()):
            if (d / "state.json").exists():
                v = _survey_view(d.name)
                out.append({k: v[k] for k in ("id", "run", "start", "hours", "status", "running", "analysed_s", "bg_years")}
                           | {"n_triggers": len(v["triggers"]), "blocks": len(v["blocks"]), "done": len(v["done"])})
    return out


@app.post("/api/surveys")
def create_survey(req: SurveyRequest):
    if req.run not in survey.RUNS:
        raise HTTPException(400, "Unknown observing run")
    a, b = survey.RUNS[req.run]
    start = req.start or a
    if not (a <= start < b) or not (0.5 <= req.hours <= 24 * 30):
        raise HTTPException(400, f"Start must be inside {req.run} ({a}–{b}); 0.5 h to 30 days")
    try:
        sv = survey.Survey.create(req.run, start, req.hours)
    except Exception as exc:
        raise HTTPException(502, f"Could not plan the survey: {exc}")
    return _survey_view(sv.sid)


@app.get("/api/surveys/{sid}")
def get_survey(sid: str):
    if not (survey.ROOT / Path(sid).name / "state.json").exists():
        raise HTTPException(404)
    return _survey_view(Path(sid).name)


@app.post("/api/surveys/{sid}/start")
def start_survey(sid: str):
    sid = Path(sid).name
    if runner["thread"] and runner["thread"].is_alive():
        if runner["survey"].sid == sid:
            return {"ok": True}
        raise HTTPException(409, f"Survey {runner['survey'].sid} is running; pause it first")
    sv = survey.Survey(sid)

    def go():
        try:
            sv.run(progress=lambda m: runner.__setitem__("msg", m))
        except Exception as exc:
            traceback.print_exc()
            st = sv.state()
            st["status"] = "error"
            st["log"].append(f"ERROR: {exc}")
            sv.save(st)

    runner.update(survey=sv, thread=threading.Thread(target=go, daemon=True))
    runner["thread"].start()
    return {"ok": True}


@app.post("/api/surveys/{sid}/pause")
def pause_survey(sid: str):
    if runner["survey"] and runner["survey"].sid == Path(sid).name:
        runner["survey"].stop.set()
    return {"ok": True}


# ------------------------------------------------------------------ contribution
class Author(BaseModel):
    name: str = ""
    affiliation: str = ""
    email: str = ""
    orcid: str = ""


class ZenodoRequest(BaseModel):
    author: Author
    token: str
    sandbox: bool = True


class PublishRequest(BaseModel):
    token: str
    sandbox: bool = True
    id: int


def _result(name):
    p = pipeline.RESULTS / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return json.loads(p.read_text())


@app.get("/api/checks/{name}")
def get_checks(name: str):
    return contribute.checks(_result(name))


@app.post("/api/package/{name}")
def get_package(name: str, author: Author):
    res = _result(name)
    pkg, _ = contribute.build_package(res, author.model_dump())
    fname = f"gw_candidate_{res['best']['t_peak']:.0f}.zip"
    return Response(pkg, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/email/{name}")
def get_email(name: str, author: Author):
    res = _result(name)
    return {"text": contribute.email_text(res, contribute.checks(res), author.model_dump())}


@app.post("/api/zenodo/{name}")
def zenodo_draft(name: str, req: ZenodoRequest):
    try:
        return contribute.zenodo_draft(_result(name), req.author.model_dump(), req.token, req.sandbox)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/zenodo-publish")
def zenodo_publish(req: PublishRequest):
    try:
        return contribute.zenodo_publish(req.id, req.token, req.sandbox)
    except Exception as exc:
        raise HTTPException(502, str(exc))


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

if __name__ == "__main__":
    print("GW Detector → http://localhost:8002")
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="warning")
