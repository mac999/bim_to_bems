"""EnergyPlus execution wrapper (CLI: ``energyplus -w weather.epw -d out -r model.idf``)."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .config import find_energyplus, find_weather_file


class EnergyPlusError(RuntimeError):
    pass


def run_energyplus(idf_path: str, output_dir: str, weather_file: str | None,
                   cfg: dict, verbose: bool = True) -> dict:
    """Run EnergyPlus; returns a summary dict. Raises EnergyPlusError on fatal."""
    exe = find_energyplus(cfg)
    if not exe:
        raise EnergyPlusError(
            "energyplus executable not found. Install EnergyPlus or set "
            "'energyplus_dir' in config.json."
        )
    epw = weather_file or find_weather_file(cfg)
    if not epw or not Path(epw).exists():
        raise EnergyPlusError(
            "No weather file (.epw) found. Set 'weather_file' in config.json "
            "or put one under datasets/weather/."
        )
    os.makedirs(output_dir, exist_ok=True)
    cmd = [exe, "-w", epw, "-d", output_dir, "-r", str(idf_path)]
    if verbose:
        print(f"[e+] {os.path.basename(exe)} -w {os.path.basename(epw)} "
              f"-d {output_dir} -r {os.path.basename(str(idf_path))}", flush=True)
    timeout = cfg.get("simulation", {}).get("timeout_sec", 900)
    start = time.monotonic()
    # stream stdout line-by-line: the "[e+] ..." lines land in the job's
    # pipeline.log, which drives the web app's live progress bar
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1)
    out_lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        out_lines.append(line)
        if verbose:
            print(f"[e+] {line}", flush=True)
        if time.monotonic() - start > timeout:
            proc.kill()
            proc.wait()
            raise EnergyPlusError(f"EnergyPlus timed out after {timeout}s")
    returncode = proc.wait()
    if returncode == 0:
        _rerun_readvars_unlimited(exe, output_dir, verbose)
    err_file = Path(output_dir) / "eplusout.err"
    err_text = err_file.read_text(errors="replace") if err_file.exists() else ""
    summary = _parse_err_summary(err_text)
    summary.update({
        "returncode": returncode,
        "weather_file": epw,
        "energyplus": exe,
        "output_dir": str(output_dir),
    })
    if returncode != 0 or summary["fatal"] > 0 or not summary["success"]:
        tail = "\n".join(err_text.splitlines()[-25:])
        raise EnergyPlusError(
            f"EnergyPlus failed (rc={returncode}, severe={summary['severe']}, "
            f"fatal={summary['fatal']}).\n--- eplusout.err (tail) ---\n{tail}\n"
            f"--- console (tail) ---\n" + "\n".join(out_lines[-15:])
        )
    if verbose:
        print(f"[e+] completed: {summary['warnings']} warnings, "
              f"{summary['severe']} severe errors")
    return summary


def _rerun_readvars_unlimited(energyplus_exe: str, output_dir: str,
                              verbose: bool) -> None:
    """Regenerate eplusout.csv without ReadVarsESO's default 250-column cap.

    The ``-r`` run already produced a CSV, but per-surface outputs (sunlit
    fraction) can push the column count past the cap, silently dropping
    variables. Best effort: if the tool is missing the capped CSV stands.
    """
    rv = Path(energyplus_exe).parent / "PostProcess" / "ReadVarsESO.exe"
    if os.name != "nt":
        rv = rv.with_suffix("")
    if not rv.exists() or not (Path(output_dir) / "eplusout.eso").exists():
        return
    rvi = Path(output_dir) / "eplusout.rvi"
    rvi.write_text("eplusout.eso\neplusout.csv\n", encoding="ascii")
    try:
        subprocess.run([str(rv), rvi.name, "unlimited"], cwd=output_dir,
                       capture_output=True, text=True, timeout=300)
        if verbose:
            print("[e+] readvars re-run (unlimited columns)", flush=True)
    except (subprocess.SubprocessError, OSError) as e:
        if verbose:
            print(f"[e+] readvars unlimited re-run skipped: {e}", flush=True)


def _parse_err_summary(err_text: str) -> dict:
    warnings = severe = fatal = 0
    success = False
    m = re.search(r"EnergyPlus Completed Successfully--\s*(\d+)\s*Warning;\s*(\d+)\s*Severe", err_text)
    if m:
        success = True
        warnings, severe = int(m.group(1)), int(m.group(2))
    else:
        warnings = err_text.count("** Warning **")
        severe = err_text.count("** Severe  **")
    fatal = err_text.count("**  Fatal  **")
    return {"success": success, "warnings": warnings, "severe": severe, "fatal": fatal}
