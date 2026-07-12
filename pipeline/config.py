"""Pipeline configuration: defaults, config.json loading, EnergyPlus discovery."""
from __future__ import annotations

import glob
import json
import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG: dict = {
    "energyplus_dir": "",  # auto-discovered when empty
    "weather_file": "",    # defaults to the first EPW found (datasets/weather, then E+ install)
    "idf_version": "25.2",
    "conversion": {
        "window_mode": "auto",        # auto: project IfcWindow onto walls, fallback to WWR
        "window_wall_ratio": 0.3,      # used by the WWR fallback
        "plane_merge_angle_deg": 5.0,  # triangle clustering: max normal deviation
        "plane_merge_dist": 0.02,      # triangle clustering: max plane offset (m)
        "adjacency_gap": 0.5,          # max gap between paired inter-zone surfaces (m)
        "adjacency_overlap_ratio": 0.3,
        "ground_level_tol": 0.3,       # floors within this height of the lowest floor -> Ground
        "min_surface_area": 0.05,      # drop degenerate surfaces (m2)
        "max_vertices_per_surface": 60,
    },
    # ASHRAE-referenced defaults, all user-overridable via config.json:
    # setpoints ASHRAE 55; occupant density & ventilation ASHRAE 62.1;
    # LPD/EPD ASHRAE 90.1; envelope U-values ~ASHRAE 90.1 CZ4-5.
    "loads": {
        "people_per_area": 0.08,          # persons/m2 (62.1 office ~0.05)
        "activity_level_w": 120.0,        # W/person
        "lights_w_per_area": 8.0,         # W/m2 (90.1 office ~8.5)
        "equipment_w_per_area": 10.0,     # W/m2
        "infiltration_ach": 0.5,
        "ventilation_l_s_person": 2.5,    # ASHRAE 62.1 office
        "ventilation_l_s_m2": 0.3,        # ASHRAE 62.1 office
        "heating_setpoint_c": 20.0,       # ASHRAE 55
        "cooling_setpoint_c": 26.0,       # ASHRAE 55
    },
    "schedules": {
        # weekday occupancy fraction as [until_hour, fraction] steps
        "occupancy_weekday": [[7, 0.1], [19, 1.0], [24, 0.2]],
        "occupancy_other_days": 0.3,
    },
    "constructions": {
        "ext_wall_u": 0.45,
        "roof_u": 0.25,
        "window_u": 2.7,
        "window_shgc": 0.6,
    },
    "site": {
        "terrain": "Suburbs",   # Country|Suburbs|City|Ocean|Urban
        "ground_temps_c": [18.0] * 12,
    },
    "hvac": {
        "max_heating_supply_air_temp_c": 50.0,
        "min_cooling_supply_air_temp_c": 13.0,
        "max_heating_supply_humidity_ratio": 0.0156,
        "min_cooling_supply_humidity_ratio": 0.0077,
    },
    "simulation": {
        "timestep": 4,
        "run_period": [1, 1, 12, 31],
        "timeout_sec": 900,
    },
    "pset_metadata": {
        "space_quantities": "Qto_SpaceBaseQuantities",
        "wall_common": "Pset_WallCommon",
        "is_external": "IsExternal",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Load config.json (merged over defaults). Missing file -> pure defaults."""
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.json"
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            return _deep_merge(DEFAULT_CONFIG, user_cfg)
        except Exception as e:  # malformed config should not kill the pipeline
            print(f"[warn] failed to read {cfg_path}: {e}; using defaults")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_default_config(path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)


def find_energyplus(config: dict | None = None) -> str | None:
    """Locate energyplus.exe. Order: config, PATH, common install roots."""
    if config:
        d = config.get("energyplus_dir") or ""
        if d:
            exe = Path(d) / ("energyplus.exe" if os.name == "nt" else "energyplus")
            if exe.exists():
                return str(exe)
    on_path = shutil.which("energyplus")
    if on_path:
        return on_path
    exe_name = "energyplus.exe" if os.name == "nt" else "energyplus"
    candidates: list[str] = []
    for root in ("C:/", "D:/", "E:/", "F:/", "C:/Program Files/", "F:/Program/"):
        candidates += glob.glob(os.path.join(root, "EnergyPlus*", exe_name))
    if candidates:
        # prefer the highest version (lexicographic on the dir name is good enough)
        return sorted(candidates)[-1]
    return None


def find_weather_file(config: dict | None = None) -> str | None:
    """Pick a weather file: config, datasets/weather, then the E+ install."""
    if config:
        w = config.get("weather_file") or ""
        if w and Path(w).exists():
            return w
    local = sorted(glob.glob(str(PROJECT_ROOT / "datasets" / "weather" / "*.epw")))
    if local:
        return local[0]
    exe = find_energyplus(config)
    if exe:
        wd = Path(exe).parent / "WeatherData"
        epws = sorted(glob.glob(str(wd / "*.epw")))
        if epws:
            return epws[0]
    return None


def list_weather_files(config: dict | None = None) -> list[str]:
    files: list[str] = []
    files += sorted(glob.glob(str(PROJECT_ROOT / "datasets" / "weather" / "*.epw")))
    exe = find_energyplus(config)
    if exe:
        files += sorted(glob.glob(str(Path(exe).parent / "WeatherData" / "*.epw")))
    seen: set[str] = set()
    out = []
    for f in files:
        key = os.path.basename(f).lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
