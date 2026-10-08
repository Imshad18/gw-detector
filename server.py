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

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gwdetect import data, pipeline

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


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")

if __name__ == "__main__":
    print("GW Detector → http://localhost:8002")
    uvicorn.run(app, host="127.0.0.1", port=8002, log_level="warning")
