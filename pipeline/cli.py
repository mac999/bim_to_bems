"""Command-line interface for the BIM-to-BEM pipeline.

Stages (composable, also runnable end-to-end with ``run``):

    python -m pipeline convert  -i building.ifc -o out_dir      # IFC -> IDF
    python -m pipeline geometry -i building.ifc|model.idf -o geometry.json
    python -m pipeline simulate -i model.idf -w weather.epw -o ep_out
    python -m pipeline results  -d ep_out -o results.json [-m model.json]
    python -m pipeline run      -i building.ifc [--idf custom.idf] -o job_dir [-w epw]
    python -m pipeline weather                                   # list EPW files
    python -m pipeline validate [-o out_dir]                     # demo dataset checks

The web app invokes ``run`` as a subprocess; ``[stage] <name>`` lines on
stdout are machine-readable progress markers.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from .config import (PROJECT_ROOT, find_weather_file, list_weather_files,
                     load_config)


def _stage(name: str) -> None:
    print(f"[stage] {name}", flush=True)


def _zones_meta(model) -> list[dict]:
    return [
        {"name": z.name, "ifc_guid": z.ifc_guid, "storey": z.storey,
         "floor_area": round(z.floor_area, 3), "volume": round(z.volume, 3)}
        for z in model.zones
    ]


def _write_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_convert(args) -> int:
    from .ifc_parser import parse_ifc
    from .idf_generator import write_idf
    from .geometry_export import export_geometry_from_model, write_geometry

    cfg = load_config(args.config)
    if args.wwr is not None:
        cfg["conversion"]["window_wall_ratio"] = args.wwr
    if args.window_mode:
        cfg["conversion"]["window_mode"] = args.window_mode
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    _stage("convert")
    model = parse_ifc(args.input, cfg)
    stem = Path(args.input).stem
    idf_path = out_dir / f"{stem}.idf"
    write_idf(model, cfg, str(idf_path))
    _write_json(out_dir / f"{stem}.model.json",
                {"building": model.name, "source": model.source_file,
                 "notes": model.notes, "zones": _zones_meta(model)})
    write_geometry(export_geometry_from_model(model), str(out_dir / f"{stem}.geometry.json"))
    print(f"[convert] wrote {idf_path}")
    return 0


def cmd_geometry(args) -> int:
    from .geometry_export import (export_geometry_from_idf,
                                  export_geometry_from_model, write_geometry)

    cfg = load_config(args.config)
    _stage("geometry")
    if args.input.lower().endswith(".ifc"):
        from .ifc_parser import parse_ifc
        geo = export_geometry_from_model(parse_ifc(args.input, cfg))
    else:
        geo = export_geometry_from_idf(args.input)
    write_geometry(geo, args.output)
    print(f"[geometry] wrote {args.output} ({len(geo['zones'])} zones)")
    return 0


def cmd_simulate(args) -> int:
    from .ep_runner import run_energyplus

    cfg = load_config(args.config)
    _stage("simulate")
    summary = run_energyplus(args.input, args.output, args.weather, cfg)
    _write_json(Path(args.output) / "simulation_summary.json", summary)
    return 0


def cmd_results(args) -> int:
    from .results_parser import write_results

    _stage("results")
    zones_meta = None
    if args.model and Path(args.model).exists():
        with open(args.model, "r", encoding="utf-8") as f:
            zones_meta = json.load(f).get("zones")
    results = write_results(args.dir, args.output, zones_meta)
    t = results["totals"]
    print(f"[results] {t['zone_count']} zones | heating {t['heating_kwh']} kWh "
          f"| cooling {t['cooling_kwh']} kWh -> {args.output}")
    return 0


def cmd_run(args) -> int:
    """Full pipeline: (IFC -> IDF) -> EnergyPlus -> results + viewer geometry."""
    from .ep_runner import run_energyplus
    from .geometry_export import (export_geometry_from_idf,
                                  export_geometry_from_model, write_geometry)
    from .results_parser import write_results

    cfg = load_config(args.config)
    if args.wwr is not None:
        cfg["conversion"]["window_wall_ratio"] = args.wwr
    if args.window_mode:
        cfg["conversion"]["window_mode"] = args.window_mode

    job_dir = Path(args.output)
    job_dir.mkdir(parents=True, exist_ok=True)
    ifc_path = args.input if args.input and args.input.lower().endswith(".ifc") else None
    idf_path = args.idf
    if args.input and args.input.lower().endswith(".idf") and not idf_path:
        idf_path = args.input
    if not ifc_path and not idf_path:
        print("[error] provide an .ifc and/or .idf input", file=sys.stderr)
        return 2

    zones_meta = None
    model = None

    # 1) conversion / geometry
    if ifc_path:
        from .ifc_parser import parse_ifc
        from .idf_generator import write_idf
        _stage("convert")
        model = parse_ifc(ifc_path, cfg)
        zones_meta = _zones_meta(model)
        _write_json(job_dir / "model.json",
                    {"building": model.name, "source": model.source_file,
                     "notes": model.notes, "zones": zones_meta})
        if not idf_path:
            idf_path = str(job_dir / "model.idf")
            write_idf(model, cfg, idf_path)
            print(f"[convert] IDF generated: {idf_path}")
        else:
            print(f"[convert] using provided IDF for simulation: {idf_path}")

    _stage("geometry")
    if model is not None and not args.idf:
        geo = export_geometry_from_model(model)
    else:
        # user-supplied IDF drives the simulation; render its zones so names match
        geo = export_geometry_from_idf(idf_path)
        if model is not None:  # add IFC elements as visual context
            ifc_geo = export_geometry_from_model(model)
            geo["context"] = ifc_geo["context"] + geo["context"]
        from .idf_io import parse_idf_geometry, zone_metrics_from_idf
        zones_meta = zone_metrics_from_idf(parse_idf_geometry(idf_path))
    write_geometry(geo, str(job_dir / "geometry.json"))

    if idf_path != str(job_dir / "model.idf"):
        shutil.copy(idf_path, job_dir / "model.idf")

    # 2) simulation
    _stage("simulate")
    ep_dir = job_dir / "ep"
    summary = run_energyplus(str(job_dir / "model.idf"), str(ep_dir), args.weather, cfg)

    # 3) results
    _stage("results")
    results = write_results(str(ep_dir), str(job_dir / "results.json"), zones_meta)
    summary["totals"] = results["totals"]
    _write_json(job_dir / "run_summary.json", summary)
    t = results["totals"]
    print(f"[done] zones={t['zone_count']} heating={t['heating_kwh']} kWh "
          f"cooling={t['cooling_kwh']} kWh")
    _stage("done")
    return 0


def cmd_weather(args) -> int:
    cfg = load_config(args.config)
    default = find_weather_file(cfg)
    for f in list_weather_files(cfg):
        mark = " (default)" if f == default else ""
        print(f"{f}{mark}")
    return 0


def cmd_validate(args) -> int:
    """Run the demo datasets end-to-end and report PASS/FAIL."""
    datasets = Path(args.datasets or PROJECT_ROOT / "datasets")
    out_root = Path(args.output or PROJECT_ROOT / "validation_out")
    cases = []
    for name in ("Duplex_A.ifc", "Office_A.ifc", "SimpleHouse.ifc",
                 "WellnessCenter.ifc", "sample.idf"):
        p = datasets / name
        if p.exists():
            cases.append(p)
    if not cases:
        print(f"[error] no demo files found under {datasets}", file=sys.stderr)
        return 2

    failures = 0
    rows = []
    for case in cases:
        job_dir = out_root / case.stem
        ns = argparse.Namespace(
            input=str(case), idf=None, output=str(job_dir), weather=args.weather,
            config=args.config, wwr=None, window_mode=None,
        )
        print(f"\n=== validate: {case.name} ===")
        try:
            rc = cmd_run(ns)
            with open(job_dir / "results.json", "r", encoding="utf-8") as f:
                totals = json.load(f)["totals"]
            ok = (rc == 0 and totals["zone_count"] > 0
                  and (totals["heating_kwh"] > 0 or totals["cooling_kwh"] > 0))
            rows.append((case.name, "PASS" if ok else "FAIL", totals))
            failures += 0 if ok else 1
        except Exception as e:
            traceback.print_exc()
            rows.append((case.name, f"FAIL ({type(e).__name__})", {}))
            failures += 1

    print("\n================ validation summary ================")
    for name, status, totals in rows:
        extra = (f" zones={totals.get('zone_count')} "
                 f"heat={totals.get('heating_kwh')}kWh "
                 f"cool={totals.get('cooling_kwh')}kWh") if totals else ""
        print(f"  {name:<24} {status}{extra}")
    print("====================================================")
    return 1 if failures else 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("-c", "--config", default=None, help="path to config.json")

    sp = sub.add_parser("convert", help="IFC -> IDF (+ model.json, geometry.json)")
    sp.add_argument("-i", "--input", required=True, help="input .ifc")
    sp.add_argument("-o", "--output", required=True, help="output directory")
    sp.add_argument("--wwr", type=float, default=None, help="window-to-wall ratio fallback")
    sp.add_argument("--window-mode", choices=["auto", "wwr", "none"], default=None)
    common(sp)
    sp.set_defaults(func=cmd_convert)

    sp = sub.add_parser("geometry", help="export viewer geometry.json from IFC or IDF")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("-o", "--output", required=True, help="output .json path")
    common(sp)
    sp.set_defaults(func=cmd_geometry)

    sp = sub.add_parser("simulate", help="run EnergyPlus on an IDF")
    sp.add_argument("-i", "--input", required=True, help="input .idf")
    sp.add_argument("-o", "--output", required=True, help="EnergyPlus output dir")
    sp.add_argument("-w", "--weather", default=None, help=".epw file")
    common(sp)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("results", help="parse EnergyPlus outputs to results.json")
    sp.add_argument("-d", "--dir", required=True, help="EnergyPlus output dir")
    sp.add_argument("-o", "--output", required=True, help="results.json path")
    sp.add_argument("-m", "--model", default=None, help="model.json (zone areas)")
    common(sp)
    sp.set_defaults(func=cmd_results)

    sp = sub.add_parser("run", help="full pipeline: convert + simulate + results")
    sp.add_argument("-i", "--input", required=True, help=".ifc or .idf input")
    sp.add_argument("--idf", default=None, help="use this IDF for simulation (with IFC as geometry)")
    sp.add_argument("-o", "--output", required=True, help="job output directory")
    sp.add_argument("-w", "--weather", default=None, help=".epw file")
    sp.add_argument("--wwr", type=float, default=None)
    sp.add_argument("--window-mode", choices=["auto", "wwr", "none"], default=None)
    common(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("weather", help="list available weather files")
    common(sp)
    sp.set_defaults(func=cmd_weather)

    sp = sub.add_parser("validate", help="run demo datasets end-to-end")
    sp.add_argument("-d", "--datasets", default=None)
    sp.add_argument("-o", "--output", default=None)
    sp.add_argument("-w", "--weather", default=None)
    common(sp)
    sp.set_defaults(func=cmd_validate)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        traceback.print_exc()
        print(f"[error] {e}", file=sys.stderr)
        return 1
