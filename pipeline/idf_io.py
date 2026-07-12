"""Minimal IDF reader: extract zones/surfaces/fenestration geometry.

Used when the user supplies an IDF directly (no IFC), so the 3D viewer can
still render zones and color them by simulation results. Field positions for
name/type/construction/zone are stable across IDF versions; the vertex block
is located by finding a field N such that exactly 3*N numeric fields follow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np


@dataclass
class IdfSurface:
    name: str
    surface_type: str
    zone: str
    vertices: np.ndarray


@dataclass
class IdfModel:
    zones: list[str] = field(default_factory=list)
    surfaces: list[IdfSurface] = field(default_factory=list)
    windows: list[IdfSurface] = field(default_factory=list)  # zone = host surface name


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


def parse_idf_geometry(idf_path: str) -> IdfModel:
    with open(idf_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    model = IdfModel()
    for obj_type, fields in _iter_objects(text):
        if obj_type == "zone" and fields:
            model.zones.append(fields[0])
        elif obj_type == "buildingsurface:detailed" and len(fields) >= 4:
            verts = _extract_vertices(fields, 4)
            if verts is not None and len(verts) >= 3:
                model.surfaces.append(IdfSurface(
                    name=fields[0], surface_type=fields[1].title(),
                    zone=fields[3], vertices=verts,
                ))
        elif obj_type == "fenestrationsurface:detailed" and len(fields) >= 4:
            verts = _extract_vertices(fields, 4)
            if verts is not None and len(verts) >= 3:
                model.windows.append(IdfSurface(
                    name=fields[0], surface_type="Window",
                    zone=fields[3], vertices=verts,
                ))
    return model


def zone_metrics_from_idf(model: IdfModel) -> list[dict]:
    """Approximate floor area / volume per zone from surface geometry."""
    from .model import polygon_area

    out = []
    for zone in model.zones:
        surfs = [s for s in model.surfaces if s.zone.upper() == zone.upper()]
        if not surfs:
            out.append({"name": zone, "floor_area": 0.0, "volume": 0.0,
                        "ifc_guid": "", "storey": ""})
            continue
        floors = [s for s in surfs if s.surface_type == "Floor"]
        area = sum(polygon_area(s.vertices) for s in floors)
        all_z = np.concatenate([s.vertices[:, 2] for s in surfs])
        height = float(all_z.max() - all_z.min())
        out.append({
            "name": zone,
            "floor_area": round(area, 3),
            "volume": round(area * height, 3),
            "ifc_guid": "", "storey": "",
        })
    return out
