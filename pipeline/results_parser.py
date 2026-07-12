"""Parse EnergyPlus outputs (eplusout.csv from readvars) into per-zone JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

J_TO_KWH = 1.0 / 3.6e6

_VAR_MAP = {
    "Zone Ideal Loads Supply Air Total Heating Energy": ("heating_kwh", J_TO_KWH),
    "Zone Ideal Loads Supply Air Total Cooling Energy": ("cooling_kwh", J_TO_KWH),
    "Zone Mean Air Temperature": ("temp_c", 1.0),
    "Zone Operative Temperature": ("operative_temp_c", 1.0),
    "Zone Air Relative Humidity": ("rh_pct", 1.0),
    # E+ >= 22.2 renamed "Zone Windows ..." to "Enclosure Windows ..."
    "Enclosure Windows Total Transmitted Solar Radiation Energy": ("solar_gain_kwh", J_TO_KWH),
}
# per-surface variables aggregated to their zone (surfaces are named
# "<zone>_<Wall|Roof|Ceiling|Floor>[_n]" by the IDF generator)
_SURFACE_VAR_MAP = {
    "Surface Outside Face Sunlit Fraction": "sunlit_frac",
}
_IDEAL_SUFFIX = "_IDEAL_LOADS"


def _zone_for_surface(surface_upper: str, zone_names_upper: list[str]) -> str | None:
    """Longest zone name that prefixes the surface name (guards against
    zone names that are prefixes of other zone names)."""
    best = None
    for zn in zone_names_upper:
        if surface_upper.startswith(zn + "_") and (best is None or len(zn) > len(best)):
            best = zn
    return best

_COL_RE = re.compile(r"^(?P<obj>[^:]+):(?P<var>[^\[]+)\s*\[(?P<unit>[^\]]*)\]\((?P<freq>[^)]+)\)\s*$")


def parse_results(output_dir: str, zones_meta: list[dict] | None = None) -> dict:
    """Build results.json content from an EnergyPlus output directory.

    zones_meta: optional [{name, ifc_guid, floor_area, volume, storey}, ...]
    used to attach areas and to restore canonical (mixed-case) zone names.
    """
    csv_path = Path(output_dir) / "eplusout.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} not found (was EnergyPlus run with -r?)")
    df = pd.read_csv(csv_path)

    canonical = {}
    meta_by_upper = {}
    if zones_meta:
        for zm in zones_meta:
            canonical[zm["name"].upper()] = zm["name"]
            meta_by_upper[zm["name"].upper()] = zm

    months: list[str] = [str(v).strip() for v in df.iloc[:, 0].tolist()]
    zones: dict[str, dict] = {}
    surface_cols: list[tuple[str, str, str]] = []  # (obj_upper, agg_key, column)

    for col in df.columns[1:]:
        m = _COL_RE.match(col.strip())
        if not m:
            continue
        var = m.group("var").strip()
        obj = m.group("obj").strip().upper()
        if var in _SURFACE_VAR_MAP:
            surface_cols.append((obj, _SURFACE_VAR_MAP[var], col))
            continue
        if var not in _VAR_MAP:
            continue
        key, factor = _VAR_MAP[var]
        zone_upper = obj[: -len(_IDEAL_SUFFIX)] if obj.endswith(_IDEAL_SUFFIX) else obj
        zone_name = canonical.get(zone_upper, zone_upper)
        series = pd.to_numeric(df[col], errors="coerce").fillna(0.0) * factor
        z = zones.setdefault(zone_name, {"monthly": {}})
        z["monthly"][key] = [round(float(v), 4) for v in series.tolist()]

    # per-surface variables -> zone average (e.g. shadow/sunlit analysis)
    zone_uppers = [zn.upper() for zn in zones]
    surf_acc: dict[tuple[str, str], list] = {}  # (zone_upper, key) -> list of series
    for obj, key, col in surface_cols:
        zone_upper = _zone_for_surface(obj, zone_uppers)
        if zone_upper is None:
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        surf_acc.setdefault((zone_upper, key), []).append(series)
    for (zone_upper, key), series_list in surf_acc.items():
        zone_name = canonical.get(zone_upper, zone_upper)
        avg = pd.concat(series_list, axis=1).mean(axis=1).fillna(0.0)
        zones[zone_name]["monthly"][key] = [round(float(v), 4) for v in avg.tolist()]

    # aggregate annual metrics per zone
    for zone_name, z in zones.items():
        monthly = z["monthly"]
        z["heating_kwh"] = round(sum(monthly.get("heating_kwh", [])), 3)
        z["cooling_kwh"] = round(sum(monthly.get("cooling_kwh", [])), 3)
        temps = monthly.get("temp_c", [])
        if temps:
            z["temp_avg_c"] = round(sum(temps) / len(temps), 2)
            z["temp_min_c"] = round(min(temps), 2)
            z["temp_max_c"] = round(max(temps), 2)
        if monthly.get("solar_gain_kwh"):
            z["solar_gain_kwh"] = round(sum(monthly["solar_gain_kwh"]), 3)
        if monthly.get("rh_pct"):
            z["rh_pct"] = round(sum(monthly["rh_pct"]) / len(monthly["rh_pct"]), 2)
        if monthly.get("operative_temp_c"):
            ot = monthly["operative_temp_c"]
            z["operative_temp_c"] = round(sum(ot) / len(ot), 2)
        if monthly.get("sunlit_frac"):
            sf = monthly["sunlit_frac"]
            z["sunlit_frac"] = round(sum(sf) / len(sf), 4)
        zm = meta_by_upper.get(zone_name.upper())
        if zm:
            area = float(zm.get("floor_area") or 0.0)
            z["area_m2"] = round(area, 2)
            z["volume_m3"] = round(float(zm.get("volume") or 0.0), 2)
            z["ifc_guid"] = zm.get("ifc_guid", "")
            z["storey"] = zm.get("storey", "")
            if area > 0:
                z["heating_kwh_m2"] = round(z["heating_kwh"] / area, 3)
                z["cooling_kwh_m2"] = round(z["cooling_kwh"] / area, 3)

    total_area = sum(z.get("area_m2", 0.0) for z in zones.values())
    totals = {
        "heating_kwh": round(sum(z.get("heating_kwh", 0) for z in zones.values()), 2),
        "cooling_kwh": round(sum(z.get("cooling_kwh", 0) for z in zones.values()), 2),
        "solar_gain_kwh": round(sum(z.get("solar_gain_kwh", 0) for z in zones.values()), 2),
        "floor_area_m2": round(total_area, 2),
        "zone_count": len(zones),
    }
    if total_area > 0:
        totals["heating_kwh_m2"] = round(totals["heating_kwh"] / total_area, 2)
        totals["cooling_kwh_m2"] = round(totals["cooling_kwh"] / total_area, 2)

    return {"months": months, "zones": zones, "totals": totals}


def write_results(output_dir: str, out_path: str, zones_meta: list[dict] | None = None) -> dict:
    results = parse_results(output_dir, zones_meta)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return results
