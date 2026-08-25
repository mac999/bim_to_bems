"""Minimal IDF reader: extract zones/surfaces/fenestration geometry.

Used when the user supplies an IDF directly (no IFC), so the 3D viewer can
still render zones and color them by simulation results. Field positions for
name/type/construction/zone are stable across IDF versions; the vertex block
is located by finding a field N such that exactly 3*N numeric fields follow.
Surfaces written in the relative coordinate system are moved into world
coordinates with their zone origin and relative north.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

# monthly variables the results parser understands; appended to a user IDF that
# does not report them, so a run always yields per-zone numbers
OUTPUT_VARIABLES = [
    "Zone Ideal Loads Supply Air Total Heating Energy",
    "Zone Ideal Loads Supply Air Total Cooling Energy",
    "Zone Air System Sensible Heating Energy",
    "Zone Air System Sensible Cooling Energy",
    "Zone Mean Air Temperature",
    "Zone Lights Electricity Energy",
    "Zone Electric Equipment Electricity Energy",
]


@dataclass
class IdfSurface:
    name: str
    surface_type: str
    zone: str
    vertices: np.ndarray


@dataclass
class IdfZone:
    name: str
    origin: np.ndarray
    north_deg: float = 0.0
    multiplier: int = 1


@dataclass
class IdfModel:
    zones: list[str] = field(default_factory=list)
    surfaces: list[IdfSurface] = field(default_factory=list)
    windows: list[IdfSurface] = field(default_factory=list)  # zone = host surface name
    zone_info: dict[str, IdfZone] = field(default_factory=dict)


def _iter_objects(text: str):
    text = re.sub(r"!.*", "", text)  # strip comments
    for raw in text.split(";"):
        fields = [f.strip() for f in raw.split(",")]
        if len(fields) >= 2 and fields[0]:
            yield fields[0].lower(), fields[1:]


def _extract_vertices(fields: list[str], search_from: int) -> np.ndarray | None:
    def is_num(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    n_fields = len(fields)
    for idx in range(search_from, n_fields):
        f = fields[idx]
        if not f or not f.lstrip("-").replace(".", "", 1).isdigit():
            continue
        try:
            n = int(float(f))
        except ValueError:
            continue
        if n >= 3 and n_fields - idx - 1 == 3 * n and all(
            is_num(x) or x == "" for x in fields[idx + 1:]
        ):
            coords = [float(x) if x else 0.0 for x in fields[idx + 1:]]
            return np.array(coords, dtype=float).reshape(-1, 3)
    # fallback: count trailing numeric fields ("autocalculate" vertex count)
    tail = []
    for f in reversed(fields):
        if is_num(f):
            tail.append(float(f))
        else:
            break
    n_tail = (len(tail) // 3) * 3
    if n_tail >= 9:
        coords = list(reversed(tail))[len(tail) - n_tail:]
        return np.array(coords, dtype=float).reshape(-1, 3)
    return None


def _num(fields: list[str], idx: int, default: float = 0.0) -> float:
    try:
        return float(fields[idx])
    except (IndexError, ValueError):
        return default


def _zone_object(fields: list[str]) -> IdfZone:
    """Zone: name, relative north, X/Y/Z origin, type, multiplier, ..."""
    return IdfZone(
        name=fields[0],
        origin=np.array([_num(fields, 2), _num(fields, 3), _num(fields, 4)]),
        north_deg=_num(fields, 1),
        multiplier=max(1, int(_num(fields, 6, 1.0))),
    )


def _to_world(verts: np.ndarray, zone: IdfZone | None) -> np.ndarray:
    """Relative-coordinate vertices -> world: rotate by relative north, offset."""
    if zone is None:
        return verts
    out = verts
    if abs(zone.north_deg) > 1e-9:
        a = math.radians(-zone.north_deg)
        cos_a, sin_a = math.cos(a), math.sin(a)
        x, y = out[:, 0], out[:, 1]
        out = np.column_stack([x * cos_a - y * sin_a, x * sin_a + y * cos_a, out[:, 2]])
    return out + zone.origin


def parse_idf_geometry(idf_path: str) -> IdfModel:
    with open(idf_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    model = IdfModel()
    relative = False
    pending: list[tuple[IdfSurface, str, bool]] = []  # surface, zone key, is window
    host_zone: dict[str, str] = {}
    for obj_type, fields in _iter_objects(text):
        if obj_type == "globalgeometryrules" and len(fields) >= 3:
            relative = fields[2].strip().lower().startswith("relative")
        elif obj_type == "zone" and fields:
            zone = _zone_object(fields)
            model.zones.append(zone.name)
            model.zone_info[zone.name.upper()] = zone
        elif obj_type == "buildingsurface:detailed" and len(fields) >= 4:
            verts = _extract_vertices(fields, 4)
            if verts is not None and len(verts) >= 3:
                surface = IdfSurface(name=fields[0], surface_type=fields[1].title(),
                                     zone=fields[3], vertices=verts)
                model.surfaces.append(surface)
                host_zone[fields[0].upper()] = fields[3]
                pending.append((surface, fields[3], False))
        elif obj_type == "fenestrationsurface:detailed" and len(fields) >= 4:
            verts = _extract_vertices(fields, 4)
            if verts is not None and len(verts) >= 3:
                surface = IdfSurface(name=fields[0], surface_type=fields[1].title(),
                                     zone=fields[3], vertices=verts)
                model.windows.append(surface)
                pending.append((surface, fields[3], True))
    if relative:
        for surface, key, is_window in pending:
            if is_window:
                key = host_zone.get(key.upper(), key)
            surface.vertices = _to_world(
                surface.vertices, model.zone_info.get(key.upper()))
    return model


def ensure_output_variables(idf_path: str) -> list[str]:
    """Append the monthly output variables the results parser needs.

    A hand-written IDF is simulated as-is; without these it produces no
    per-zone numbers to color the viewer with. Returns what was added.
    """
    with open(idf_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    present = {
        fields[1].strip().lower()
        for obj_type, fields in _iter_objects(text)
        if obj_type == "output:variable" and len(fields) >= 2
    }
    missing = [v for v in OUTPUT_VARIABLES if v.lower() not in present]
    if missing:
        block = "".join(f"\nOutput:Variable,*,{v},Monthly;\n" for v in missing)
        with open(idf_path, "a", encoding="utf-8") as f:
            f.write(block)
    return missing


def zone_metrics_from_idf(model: IdfModel) -> list[dict]:
    """Approximate floor area / volume per zone from surface geometry."""
    from .model import polygon_area

    out = []
    for zone in model.zones:
        surfs = [s for s in model.surfaces if s.zone.upper() == zone.upper()]
        # zone loads are reported with the multiplier applied, so floor area
        # has to carry it as well for the per-area figures to line up
        mult = model.zone_info.get(zone.upper(), IdfZone(zone, np.zeros(3))).multiplier
        if not surfs:
            out.append({"name": zone, "floor_area": 0.0, "volume": 0.0,
                        "ifc_guid": "", "storey": "", "multiplier": mult})
            continue
        floors = [s for s in surfs if s.surface_type == "Floor"]
        area = sum(polygon_area(s.vertices) for s in floors)
        all_z = np.concatenate([s.vertices[:, 2] for s in surfs])
        height = float(all_z.max() - all_z.min())
        out.append({
            "name": zone,
            "floor_area": round(area * mult, 3),
            "volume": round(area * height * mult, 3),
            "ifc_guid": "", "storey": "", "multiplier": mult,
        })
    return out
