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
        "door_u": 2.2,
    },
    # per-space overrides of "loads", chosen by keyword in the space name /
    # IfcSpace.LongName; the first profile whose keyword matches wins
    "space_profiles": {
        "corridor": {
            "keywords": ["corridor", "hall", "lobby", "stair", "elevator", "circulation"],
            "people_per_area": 0.02, "lights_w_per_area": 5.0,
            "equipment_w_per_area": 1.0,
        },
        "toilet": {
            "keywords": ["toilet", "wc", "restroom", "bath", "shower", "sanitary"],
            "people_per_area": 0.02, "lights_w_per_area": 6.0,
            "equipment_w_per_area": 1.0,
        },
        "storage": {
            "keywords": ["storage", "store", "closet", "utility", "plant", "mech",
                         "electrical", "garage", "attic"],
            "people_per_area": 0.005, "lights_w_per_area": 4.0,
            "equipment_w_per_area": 1.0, "ventilation_l_s_person": 0.0,
        },
        "meeting": {
            "keywords": ["meeting", "conference", "seminar", "classroom", "lecture"],
            "people_per_area": 0.35, "lights_w_per_area": 9.0,
            "equipment_w_per_area": 5.0,
        },
        "kitchen": {
            "keywords": ["kitchen", "pantry", "canteen", "dining"],
            "people_per_area": 0.1, "lights_w_per_area": 9.0,
            "equipment_w_per_area": 30.0,
        },
        "bedroom": {
            "keywords": ["bedroom", "bed room", "living", "dwelling", "apartment"],
            "people_per_area": 0.03, "lights_w_per_area": 5.0,
            "equipment_w_per_area": 4.0,
        },
    },
    # ideal thermal load -> estimated delivered energy (results.json only)
    "efficiency": {
        "heating_efficiency": 0.85,   # seasonal efficiency of the heat source
        "cooling_cop": 3.0,           # seasonal COP of the cooling plant
        "distribution_efficiency": 0.9,  # fans, pumps, duct and pipe losses
    },
    "site": {
        "terrain": "Suburbs",   # Country|Suburbs|City|Ocean|Urban
        "ground_temps_c": [18.0] * 12,
        "ground_temps_from_epw": True,  # replace the list with EPW header values
        "ground_temps_depth_m": 2.0,    # depth set to take from the EPW header
        "ground_coupling": 0.5,         # 0: indoor temperature, 1: raw EPW profile
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


def ground_temperatures_from_epw(epw_path: str, depth_m: float = 2.0) -> list[float] | None:
    """Monthly ground temperatures from the EPW ``GROUND TEMPERATURES`` header.

    The header holds one set per depth: depth, conductivity, density, specific
    heat, then 12 monthly values. The set closest to ``depth_m`` is returned.
    """
    try:
        with open(epw_path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(12):
                line = f.readline()
                if not line:
                    break
                if not line.upper().startswith("GROUND TEMPERATURES"):
                    continue
                parts = [p.strip() for p in line.rstrip("\r\n").split(",")]
                sets = []
                i = 2
                while len(parts) - i >= 16:
                    try:
                        depth = float(parts[i])
                        temps = [float(v) for v in parts[i + 4:i + 16]]
                    except ValueError:
                        break
                    sets.append((depth, temps))
                    i += 16
                if sets:
                    return min(sets, key=lambda s: abs(s[0] - depth_m))[1]
    except OSError:
        return None
    return None


def apply_epw_site_data(config: dict, epw_path: str | None) -> str:
    """Fill ``site.ground_temps_c`` from the weather file. Returns a log note.

    The undisturbed EPW profile must not be used as-is for
    ``Site:GroundTemperature:BuildingSurface`` (it ignores the heat the building
    itself puts into the ground), so it is blended towards the heating setpoint
    with ``site.ground_coupling``: 0 keeps the indoor temperature, 1 the raw
    weather-file values.
    """
    site = config.setdefault("site", {})
    if not epw_path or not site.get("ground_temps_from_epw", True):
        return ""
    depth = float(site.get("ground_temps_depth_m", 2.0))
    temps = ground_temperatures_from_epw(epw_path, depth)
    if not temps:
        return ""
    coupling = float(site.get("ground_coupling", 0.5))
    indoor = float((config.get("loads") or {}).get("heating_setpoint_c", 20.0))
    site["ground_temps_c"] = [
        round(indoor + coupling * (t - indoor), 2) for t in temps
    ]
    return (f"ground temperatures from {os.path.basename(epw_path)} "
            f"({depth:g} m depth, coupling {coupling:g})")


def match_space_profile(config: dict, *labels: str) -> str:
    """Name of the first space profile whose keywords occur in the labels."""
    text = " ".join(str(l).lower() for l in labels if l)
    if not text:
        return ""
    for name, profile in (config.get("space_profiles") or {}).items():
        for keyword in profile.get("keywords", []):
            if str(keyword).lower() in text:
                return name
    return ""


def zone_loads(config: dict, profile: str) -> dict:
    """Base loads with the named space profile applied on top."""
    loads = dict(config.get("loads") or {})
    overrides = (config.get("space_profiles") or {}).get(profile) or {}
    loads.update({k: v for k, v in overrides.items() if k != "keywords"})
    return loads
