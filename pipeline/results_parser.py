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
    # fallback for IDFs that model a real air system instead of ideal loads
    "Zone Air System Sensible Heating Energy": ("heating_kwh", J_TO_KWH),
    "Zone Air System Sensible Cooling Energy": ("cooling_kwh", J_TO_KWH),
    "Zone Lights Electricity Energy": ("lights_kwh", J_TO_KWH),
    "Zone Electric Equipment Electricity Energy": ("equipment_kwh", J_TO_KWH),
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
# ideal-loads output wins when a model reports both
_VAR_RANK = {
    "Zone Ideal Loads Supply Air Total Heating Energy": 2,
    "Zone Ideal Loads Supply Air Total Cooling Energy": 2,
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


def parse_results(output_dir: str, zones_meta: list[dict] | None = None,
                  cfg: dict | None = None) -> dict:
    """Build results.json content from an EnergyPlus output directory.

    zones_meta: optional [{name, ifc_guid, floor_area, volume, storey}, ...]
    used to attach areas and to restore canonical (mixed-case) zone names.
    cfg: optional pipeline config; its ``efficiency`` section turns the ideal
    thermal load into an estimated delivered-energy figure.
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

    # monthly columns only: a hand-written IDF may report other frequencies into
    # the same CSV, whose rows are not months
    matched: list[tuple[str, str, str]] = []  # (column, variable, object_upper)
    for col in df.columns[1:]:
        m = _COL_RE.match(col.strip())
        if not m or m.group("freq").strip().lower() != "monthly":
            continue
        var = m.group("var").strip()
        if var in _VAR_MAP or var in _SURFACE_VAR_MAP:
            matched.append((col, var, m.group("obj").strip().upper()))
    if matched:
        numeric = df[[c for c, _v, _o in matched]].apply(pd.to_numeric, errors="coerce")
        keep = numeric.notna().any(axis=1)
        if keep.any():
            df = df[keep].reset_index(drop=True)

    months: list[str] = [str(v).strip() for v in df.iloc[:, 0].tolist()]
    zones: dict[str, dict] = {}
    surface_cols: list[tuple[str, str, str]] = []  # (obj_upper, agg_key, column)
    ranks: dict[tuple[str, str], int] = {}

    for col, var, obj in matched:
        if var in _SURFACE_VAR_MAP:
            surface_cols.append((obj, _SURFACE_VAR_MAP[var], col))
            continue
        key, factor = _VAR_MAP[var]
        zone_upper = obj[: -len(_IDEAL_SUFFIX)] if obj.endswith(_IDEAL_SUFFIX) else obj
        zone_name = canonical.get(zone_upper, zone_upper)
        rank = _VAR_RANK.get(var, 1)
        if ranks.get((zone_name, key), 0) > rank:
            continue
        ranks[(zone_name, key)] = rank
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
        for key in ("lights_kwh", "equipment_kwh"):
            if monthly.get(key):
                z[key] = round(sum(monthly[key]), 3)
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
            if zm.get("profile"):
                z["profile"] = zm["profile"]
            if area > 0:
                z["heating_kwh_m2"] = round(z["heating_kwh"] / area, 3)
                z["cooling_kwh_m2"] = round(z["cooling_kwh"] / area, 3)

    total_area = sum(z.get("area_m2", 0.0) for z in zones.values())
    totals = {
        "heating_kwh": round(sum(z.get("heating_kwh", 0) for z in zones.values()), 2),
        "cooling_kwh": round(sum(z.get("cooling_kwh", 0) for z in zones.values()), 2),
        "lights_kwh": round(sum(z.get("lights_kwh", 0) for z in zones.values()), 2),
        "equipment_kwh": round(sum(z.get("equipment_kwh", 0) for z in zones.values()), 2),
        "solar_gain_kwh": round(sum(z.get("solar_gain_kwh", 0) for z in zones.values()), 2),
        "floor_area_m2": round(total_area, 2),
        "zone_count": len(zones),
    }
    if total_area > 0:
        totals["heating_kwh_m2"] = round(totals["heating_kwh"] / total_area, 2)
        totals["cooling_kwh_m2"] = round(totals["cooling_kwh"] / total_area, 2)

    results = {"months": months, "zones": zones, "totals": totals}
    _add_delivered_energy(results, (cfg or {}).get("efficiency") or {})
    return results


def _add_delivered_energy(results: dict, efficiency: dict) -> None:
    """Estimate delivered energy from the ideal thermal load.

    ``heating_kwh`` is the thermal energy a zone needs; dividing it by the heat
    source efficiency and the distribution efficiency gives the fuel or
    electricity a real plant would use, and cooling is divided by the seasonal
    COP the same way. Lighting and equipment electricity is already final
    energy and is added as reported. The estimates are stored next to the load
    figures, never in place of them.
    """
    heat_eff = float(efficiency.get("heating_efficiency", 0.0) or 0.0)
    cool_cop = float(efficiency.get("cooling_cop", 0.0) or 0.0)
    dist_eff = float(efficiency.get("distribution_efficiency", 1.0) or 1.0)
    if heat_eff <= 0 or cool_cop <= 0 or dist_eff <= 0:
        return
    heat_div, cool_div = heat_eff * dist_eff, cool_cop * dist_eff

    for z in results["zones"].values():
        monthly = z.get("monthly", {})
        for src, dst, div in (("heating_kwh", "heating_energy_kwh", heat_div),
                              ("cooling_kwh", "cooling_energy_kwh", cool_div)):
            if monthly.get(src):
                monthly[dst] = [round(v / div, 4) for v in monthly[src]]
            z[dst] = round(z.get(src, 0.0) / div, 3)
            if z.get("area_m2"):
                z[dst + "_m2"] = round(z[dst] / z["area_m2"], 3)
        z["energy_kwh"] = round(
            z["heating_energy_kwh"] + z["cooling_energy_kwh"]
            + z.get("lights_kwh", 0.0) + z.get("equipment_kwh", 0.0), 3)
        if z.get("area_m2"):
            z["energy_kwh_m2"] = round(z["energy_kwh"] / z["area_m2"], 3)

    totals = results["totals"]
    totals["heating_energy_kwh"] = round(totals["heating_kwh"] / heat_div, 2)
    totals["cooling_energy_kwh"] = round(totals["cooling_kwh"] / cool_div, 2)
    totals["energy_kwh"] = round(
        totals["heating_energy_kwh"] + totals["cooling_energy_kwh"]
        + totals.get("lights_kwh", 0.0) + totals.get("equipment_kwh", 0.0), 2)
    if totals.get("floor_area_m2"):
        totals["energy_kwh_m2"] = round(
            totals["energy_kwh"] / totals["floor_area_m2"], 2)
    totals["efficiency"] = {"heating_efficiency": heat_eff, "cooling_cop": cool_cop,
                            "distribution_efficiency": dist_eff}


def write_results(output_dir: str, out_path: str, zones_meta: list[dict] | None = None,
                  cfg: dict | None = None, quality: dict | None = None) -> dict:
    results = parse_results(output_dir, zones_meta, cfg)
    if quality:
        results["quality"] = quality
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    return results
