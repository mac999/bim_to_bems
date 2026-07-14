"""BIM-to-BEM web application.

Thin Flask layer over the CLI pipeline: every analysis is executed by spawning
``python -m pipeline run ...`` as a subprocess (the pipeline stays independently
usable from the command line), and the app only manages job folders, progress
polling and static viewer assets.
"""
from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = Path(__file__).resolve().parent / "jobs"
DATASETS_DIR = PROJECT_ROOT / "datasets"
JOBS_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT))
from pipeline.config import list_weather_files, load_config  # noqa: E402

# Windows registry may map .js to text/plain, which breaks ES module loading
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()

ALLOWED_UPLOADS = {".ifc": "ifc", ".idf": "idf", ".epw": "epw"}


def _job_dir(job_id: str) -> Path:
    safe = "".join(c for c in job_id if c.isalnum() or c in "-_")
    return JOBS_DIR / safe


def _read_job(job_id: str) -> dict | None:
    p = _job_dir(job_id) / "job.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_job(job_id: str, data: dict) -> None:
    with open(_job_dir(job_id) / "job.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _log_tail(job_id: str, lines: int = 40) -> list[str]:
    p = _job_dir(job_id) / "pipeline.log"
    if not p.exists():
        return []
    content = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return content[-lines:]


def _current_stage(job_id: str) -> str:
    stage = ""
    p = _job_dir(job_id) / "pipeline.log"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("[stage] "):
                stage = line[8:].strip()
    return stage


# progress estimation ---------------------------------------------------------
# stage spans of the whole run; within "simulate" the EnergyPlus console dates
# (streamed into pipeline.log as "[e+] ..." lines) give a fine-grained fraction
_STAGE_SPANS = {
    "": (0.0, 0.02),
    "convert": (0.02, 0.14),
    "geometry": (0.14, 0.18),
    "simulate": (0.18, 0.94),
    "results": (0.94, 0.99),
    "done": (1.0, 1.0),
}
_SIM_DATE = re.compile(r"(?:Starting|Continuing) Simulation at (\d{2})/(\d{2})")
_WARMUP = re.compile(r"Warming up \{(\d+)\}")
_CUM_DAYS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def _progress_info(job_id: str, stage: str) -> tuple[float, str]:
    """Return (progress 0..1, short human-readable detail) for a running job."""
    lo, hi = _STAGE_SPANS.get(stage, (0.0, 0.02))
    if stage == "done":
        return 1.0, "completed"
    if stage != "simulate":
        return lo, {"": "starting", "convert": "converting IFC to IDF",
                    "geometry": "exporting viewer geometry",
                    "results": "parsing EnergyPlus results"}.get(stage, stage)

    log_path = _job_dir(job_id) / "pipeline.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    sim_part = log.split("[stage] simulate", 1)[-1]
    frac, detail = 0.0, "EnergyPlus initializing"
    last_date = None
    for m in _SIM_DATE.finditer(sim_part):
        last_date = m
    if last_date:
        mm, dd = int(last_date.group(1)), int(last_date.group(2))
        doy = _CUM_DAYS[max(0, min(11, mm - 1))] + dd
        frac = 0.15 + 0.85 * min(1.0, doy / 365.0)
        detail = f"EnergyPlus simulating {mm:02d}/{dd:02d}"
    else:
        warm = 0
        for m in _WARMUP.finditer(sim_part):
            warm = max(warm, int(m.group(1)))
        if warm:
            frac = min(0.12, warm * 0.02)
            detail = f"EnergyPlus warm-up pass {warm}"
    return lo + (hi - lo) * frac, detail


def _resolve_weather(job_id: str, requested: str | None) -> str | None:
    """Only allow EPWs from the known lists or the job's own upload."""
    if not requested:
        return None
    job_epw = _job_dir(job_id) / "input" / requested
    if job_epw.exists() and job_epw.suffix.lower() == ".epw":
        return str(job_epw)
    for f in list_weather_files(load_config()):
        if Path(f).name == requested:
            return f
    return None


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# job lifecycle
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
def create_job():
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    input_dir = _job_dir(job_id) / "input"
    input_dir.mkdir(parents=True)
    saved: dict[str, str] = {}

    demo = request.form.get("demo", "")
    if demo:
        src = DATASETS_DIR / Path(demo).name
        if not src.exists() or src.suffix.lower() not in ALLOWED_UPLOADS:
            return jsonify({"error": f"unknown demo dataset: {demo}"}), 400
        dst = input_dir / src.name
        dst.write_bytes(src.read_bytes())
        saved[ALLOWED_UPLOADS[src.suffix.lower()]] = src.name

    for field in ("ifc", "idf", "epw"):
        f = request.files.get(field)
        if f and f.filename:
            ext = Path(f.filename).suffix.lower()
            if ALLOWED_UPLOADS.get(ext) != field:
                return jsonify({"error": f"field '{field}' expects a {field} file"}), 400
            name = Path(f.filename).name
            f.save(input_dir / name)
            saved[field] = name

    if "ifc" not in saved and "idf" not in saved:
        return jsonify({"error": "upload an .ifc and/or .idf file (or pick a demo)"}), 400

    job = {"id": job_id, "state": "created", "files": saved,
           "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _write_job(job_id, job)
    return jsonify(job)


@app.post("/api/jobs/<job_id>/run")
def run_job(job_id: str):
    job = _read_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.get("state") == "running":
        return jsonify({"error": "job already running"}), 409

    opts = request.get_json(silent=True) or {}
    input_dir = _job_dir(job_id) / "input"
    args = [sys.executable, "-m", "pipeline", "run",
            "-o", str(_job_dir(job_id))]
    if "ifc" in job["files"]:
        args += ["-i", str(input_dir / job["files"]["ifc"])]
        if "idf" in job["files"]:
            args += ["--idf", str(input_dir / job["files"]["idf"])]
    else:
        args += ["-i", str(input_dir / job["files"]["idf"])]

    weather = _resolve_weather(job_id, opts.get("weather") or job["files"].get("epw"))
    if weather:
        args += ["-w", weather]
    if opts.get("wwr") not in (None, ""):
        args += ["--wwr", str(float(opts["wwr"]))]
    if opts.get("window_mode") in ("auto", "wwr", "none"):
        args += ["--window-mode", opts["window_mode"]]

    log_path = _job_dir(job_id) / "pipeline.log"
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=str(PROJECT_ROOT), stdout=log_f,
                            stderr=subprocess.STDOUT, text=True)
    with _lock:
        _procs[job_id] = proc
    job.update({"state": "running", "started_ts": time.time(),
                "weather": Path(weather).name if weather else "auto"})
    _write_job(job_id, job)

    def waiter():
        rc = proc.wait()
        log_f.close()
        j = _read_job(job_id) or job
        j["state"] = "done" if rc == 0 else "error"
        j["returncode"] = rc
        results_path = _job_dir(job_id) / "results.json"
        if rc == 0 and results_path.exists():
            with open(results_path, "r", encoding="utf-8") as f:
                j["totals"] = json.load(f).get("totals", {})
        _write_job(job_id, j)
        with _lock:
            _procs.pop(job_id, None)

    threading.Thread(target=waiter, daemon=True).start()
    return jsonify(job)


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = _read_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    stage = _current_stage(job_id)
    job["stage"] = stage
    job["log_tail"] = _log_tail(job_id)
    if job.get("state") == "done":
        job["progress"], job["detail"] = 1.0, "completed"
    else:
        prog, detail = _progress_info(job_id, stage)
        job["progress"], job["detail"] = round(prog, 4), detail
    if job.get("started_ts") and job.get("state") == "running":
        job["elapsed_sec"] = round(time.time() - job["started_ts"], 1)
    return jsonify(job)


_ARTIFACTS = {
    "geometry": ("geometry.json", "application/json"),
    "results": ("results.json", "application/json"),
    "model": ("model.json", "application/json"),
    "idf": ("model.idf", "text/plain"),
    "log": ("pipeline.log", "text/plain"),
    "err": ("ep/eplusout.err", "text/plain"),
    "report": ("ep/eplustbl.htm", "text/html"),
}


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id: str):
    """Reset: kill a running pipeline (if any) and remove the job folder."""
    job = _read_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    with _lock:
        proc = _procs.pop(job_id, None)
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    target = _job_dir(job_id)
    # the waiter thread may still hold the log file for a moment on Windows
    for _ in range(5):
        try:
            shutil.rmtree(target)
            return jsonify({"ok": True, "id": job_id})
        except OSError:
            time.sleep(0.4)
    return jsonify({"error": "job folder is locked; try again"}), 500


@app.get("/api/jobs/<job_id>/report_summary")
def report_summary(job_id: str):
    """Standalone printable HTML report built from the job's result files."""
    job = _read_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    results_path = _job_dir(job_id) / "results.json"
    if not results_path.exists():
        return jsonify({"error": "no results yet — run the analysis first"}), 404
    with open(results_path, "r", encoding="utf-8") as f:
        results = json.load(f)
    summary, model = {}, {}
    for name, target in (("run_summary.json", "summary"), ("model.json", "model")):
        p = _job_dir(job_id) / name
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if target == "summary":
                summary = data
            else:
                model = data
    return Response(_build_report_html(job, results, summary, model),
                    mimetype="text/html")


def _build_report_html(job: dict, results: dict, summary: dict, model: dict) -> str:
    esc = html.escape
    t = results.get("totals", {})
    months = results.get("months", [])
    zones = results.get("zones", {})

    # monthly totals across all zones
    mh = [0.0] * len(months)
    mc = [0.0] * len(months)
    for z in zones.values():
        monthly = z.get("monthly") or {}
        for i, v in enumerate(monthly.get("heating_kwh") or []):
            if i < len(mh):
                mh[i] += v
        for i, v in enumerate(monthly.get("cooling_kwh") or []):
            if i < len(mc):
                mc[i] += v
    peak = max(mh + mc + [1e-9])

    def num(v, nd=1):
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:,.{nd}f}"
        return f"{v:,}"

    def bar(v, color):
        pct = max(0.4, v / peak * 100)
        return (f'<div class="bar"><i style="width:{pct:.1f}%;background:{color}"></i>'
                f'<span>{num(v, 0)}</span></div>')

    month_rows = "".join(
        f"<tr><td>{esc(m)}</td><td>{bar(mh[i], '#c0392b')}</td>"
        f"<td>{bar(mc[i], '#2266bb')}</td></tr>"
        for i, m in enumerate(months))

    def pct(v):
        return "–" if v is None else f"{v * 100:.0f}%"

    zone_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc(str(z.get('storey') or ''))}</td>"
        f"<td class='r'>{num(z.get('area_m2'))}</td>"
        f"<td class='r'>{num(z.get('volume_m3'))}</td>"
        f"<td class='r'>{num(z.get('heating_kwh'), 0)}</td>"
        f"<td class='r'>{num(z.get('cooling_kwh'), 0)}</td>"
        f"<td class='r'>{num(z.get('heating_kwh_m2'))}</td>"
        f"<td class='r'>{num(z.get('cooling_kwh_m2'))}</td>"
        f"<td class='r'>{num(z.get('solar_gain_kwh'), 0)}</td>"
        f"<td class='r'>{pct(z.get('sunlit_frac'))}</td>"
        f"<td class='r'>{num(z.get('temp_avg_c'))}</td></tr>"
        for name, z in sorted(zones.items(),
                              key=lambda kv: -(kv[1].get("heating_kwh") or 0)))

    notes = "".join(f"<li>{esc(str(n))}</li>" for n in (model.get("notes") or []))
    files = ", ".join(esc(v) for v in job.get("files", {}).values()) or "–"
    weather = esc(Path(summary.get("weather_file", job.get("weather", "–"))).name)
    eplus = esc(str(summary.get("energyplus", "EnergyPlus")))

    kpis = "".join(
        f'<div class="kpi"><span>{label}</span><b>{val}</b><small>{unit}</small></div>'
        for label, val, unit in (
            ("Zones", num(t.get("zone_count")), ""),
            ("Floor area", num(t.get("floor_area_m2")), "m²"),
            ("Heating", num(t.get("heating_kwh"), 0), "kWh/yr"),
            ("Cooling", num(t.get("cooling_kwh"), 0), "kWh/yr"),
            ("Solar gain", num(t.get("solar_gain_kwh"), 0), "kWh/yr"),
            ("Heating EUI", num(t.get("heating_kwh_m2")), "kWh/m²·yr"),
            ("Cooling EUI", num(t.get("cooling_kwh_m2")), "kWh/m²·yr"),
        ))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>BEM Report · {esc(model.get('building') or job['id'])}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: system-ui, "Segoe UI", sans-serif; color: #1c1c1c; margin: 0;
         background: #f2f2ef; }}
  .page {{ max-width: 860px; margin: 24px auto; background: #fff; padding: 40px 48px;
          box-shadow: 0 2px 14px rgba(0,0,0,.12); }}
  h1 {{ font-size: 22px; margin: 0 0 2px; }}
  .sub {{ color: #777; font-size: 12.5px; margin-bottom: 22px; }}
  h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: .7px; color: #555;
       border-bottom: 2px solid #e3e3de; padding-bottom: 5px; margin: 26px 0 12px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ text-align: left; color: #666; font-weight: 600; border-bottom: 1px solid #ccc;
       padding: 5px 8px; }}
  td {{ padding: 4px 8px; border-bottom: 1px solid #eee; }}
  td.r, th.r {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .meta td:first-child {{ color: #666; width: 170px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
  .kpi {{ border: 1px solid #e3e3de; border-radius: 8px; padding: 10px 12px; }}
  .kpi span {{ display: block; font-size: 11px; color: #777; }}
  .kpi b {{ font-size: 19px; font-weight: 650; }}
  .kpi small {{ color: #999; margin-left: 4px; }}
  .bar {{ display: flex; align-items: center; gap: 7px; min-width: 190px; }}
  .bar i {{ display: block; height: 9px; border-radius: 2px; }}
  .bar span {{ font-size: 11px; color: #555; font-variant-numeric: tabular-nums; }}
  ul {{ font-size: 12px; color: #444; margin: 6px 0; padding-left: 20px; }}
  .footer {{ margin-top: 28px; font-size: 11px; color: #999; }}
  .toolbar {{ max-width: 860px; margin: 14px auto 0; text-align: right; }}
  .toolbar button {{ background: #2266bb; color: #fff; border: 0; border-radius: 6px;
                    padding: 8px 18px; font-size: 13px; cursor: pointer; }}
  @media print {{ .toolbar {{ display: none; }} .page {{ box-shadow: none; margin: 0; }}
                 body {{ background: #fff; }} }}
</style></head><body>
<div class="toolbar"><button onclick="window.print()">Print / Save as PDF</button></div>
<div class="page">
  <h1>Building Energy Analysis Report</h1>
  <div class="sub">{esc(model.get('building') or 'Building model')} ·
    generated {time.strftime('%Y-%m-%d %H:%M')} · job {esc(job['id'])}</div>

  <h2>Run information</h2>
  <table class="meta">
    <tr><td>Input files</td><td>{files}</td></tr>
    <tr><td>Weather file</td><td>{weather}</td></tr>
    <tr><td>Simulation engine</td><td>{eplus}</td></tr>
    <tr><td>Engine diagnostics</td><td>{num(summary.get('warnings'))} warnings ·
        {num(summary.get('severe'))} severe · {num(summary.get('fatal'))} fatal</td></tr>
    <tr><td>Analysis date</td><td>{esc(job.get('created', '–'))}</td></tr>
  </table>

  <h2>Annual totals</h2>
  <div class="kpis">{kpis}</div>

  <h2>Monthly heating / cooling (all zones, kWh)</h2>
  <table>
    <tr><th>Month</th><th>Heating</th><th>Cooling</th></tr>
    {month_rows}
  </table>

  <h2>Zone breakdown</h2>
  <table>
    <tr><th>Zone</th><th>Storey</th><th class="r">Area m²</th><th class="r">Vol m³</th>
        <th class="r">Heat kWh</th><th class="r">Cool kWh</th>
        <th class="r">Heat kWh/m²</th><th class="r">Cool kWh/m²</th>
        <th class="r">Solar kWh</th><th class="r">Sunlit</th><th class="r">T̄ °C</th></tr>
    {zone_rows}
  </table>

  {'<h2>Conversion notes</h2><ul>' + notes + '</ul>' if notes else ''}

  <div class="footer">Generated by BIM → BEM Energy Analyzer · simulation by EnergyPlus.
    The full EnergyPlus tabular report is available from the app (E+ report link).</div>
</div>
</body></html>"""


@app.get("/api/jobs/<job_id>/<artifact>")
def job_artifact(job_id: str, artifact: str):
    if artifact not in _ARTIFACTS:
        return jsonify({"error": "unknown artifact"}), 404
    rel, mime = _ARTIFACTS[artifact]
    p = _job_dir(job_id) / rel
    if not p.exists():
        return jsonify({"error": f"{rel} not available"}), 404
    return send_file(p, mimetype=mime)


# ---------------------------------------------------------------------------
# catalogs
# ---------------------------------------------------------------------------

@app.get("/api/weather")
def weather_catalog():
    files = list_weather_files(load_config())
    return jsonify([{"name": Path(f).name} for f in files])


@app.get("/api/demos")
def demo_catalog():
    demos = []
    if DATASETS_DIR.exists():
        for p in sorted(DATASETS_DIR.iterdir()):
            if p.suffix.lower() in (".ifc", ".idf"):
                demos.append({"name": p.name, "type": p.suffix[1:].lower(),
                              "size_mb": round(p.stat().st_size / 1e6, 1)})
    return jsonify(demos)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5006)
    args = ap.parse_args()
    app.run(host=args.host, port=args.port, debug=False)
