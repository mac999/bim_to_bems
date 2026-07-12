"""Export viewer geometry (geometry.json) from a BuildingModel or a raw IDF."""
from __future__ import annotations

import json

import numpy as np

from .model import BuildingModel
from .idf_io import IdfModel, parse_idf_geometry


def _mesh_entry(vertices: np.ndarray, faces: np.ndarray) -> dict:
    return {
        "vertices": [round(float(v), 4) for v in np.asarray(vertices).reshape(-1)],
        "faces": [int(i) for i in np.asarray(faces).reshape(-1)],
    }


def _fan_triangulate(loop: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Triangulate a (mostly convex) polygon loop as a fan."""
    n = len(loop)
    faces = [[0, i, i + 1] for i in range(1, n - 1)]
    return np.asarray(loop, dtype=float), np.array(faces, dtype=int)


def export_geometry_from_model(model: BuildingModel) -> dict:
    zones = []
    for z in model.zones:
        if z.mesh_vertices is None or z.mesh_faces is None:
            # build a mesh from the boundary surfaces
            verts_all, faces_all, offset = [], [], 0
            for s in z.surfaces:
                v, f = _fan_triangulate(s.vertices)
                verts_all.append(v)
                faces_all.append(f + offset)
                offset += len(v)
            mv = np.vstack(verts_all)
            mf = np.vstack(faces_all)
        else:
            mv, mf = z.mesh_vertices, z.mesh_faces
        entry = _mesh_entry(mv, mf)
        entry.update({
            "name": z.name, "guid": z.ifc_guid, "storey": z.storey,
            "area_m2": round(z.floor_area, 2), "volume_m3": round(z.volume, 2),
        })
        zones.append(entry)

    context = []
    for c in model.context:
        entry = _mesh_entry(c.mesh_vertices, c.mesh_faces)
        entry.update({"name": c.name, "type": c.ifc_type, "guid": c.ifc_guid})
        context.append(entry)

    return _finalize({"source": "ifc", "building": model.name,
                      "zones": zones, "context": context})


def export_geometry_from_idf(idf_path: str) -> dict:
    idf = parse_idf_geometry(idf_path)
    return _geometry_from_idf_model(idf)


def _geometry_from_idf_model(idf: IdfModel) -> dict:
    from .model import polygon_area

    zones = []
    zone_names = idf.zones or sorted({s.zone for s in idf.surfaces})
    for zone in zone_names:
        surfs = [s for s in idf.surfaces if s.zone.upper() == zone.upper()]
        if not surfs:
            continue
        verts_all, faces_all, offset = [], [], 0
        for s in surfs:
            v, f = _fan_triangulate(s.vertices)
            verts_all.append(v)
            faces_all.append(f + offset)
            offset += len(v)
        mv, mf = np.vstack(verts_all), np.vstack(faces_all)
        floors = [s for s in surfs if s.surface_type == "Floor"]
        area = sum(polygon_area(s.vertices) for s in floors)
        all_z = np.concatenate([s.vertices[:, 2] for s in surfs])
        entry = _mesh_entry(mv, mf)
        entry.update({
            "name": zone, "guid": "", "storey": "",
            "area_m2": round(area, 2),
            "volume_m3": round(area * float(all_z.max() - all_z.min()), 2),
        })
        zones.append(entry)

    context = []
    for w in idf.windows:
        v, f = _fan_triangulate(w.vertices)
        entry = _mesh_entry(v, f)
        entry.update({"name": w.name, "type": "IfcWindow", "guid": ""})
        context.append(entry)

    return _finalize({"source": "idf", "building": "IDF Model",
                      "zones": zones, "context": context})


def _finalize(geo: dict) -> dict:
    mins = np.array([np.inf] * 3)
    maxs = np.array([-np.inf] * 3)
    for group in ("zones", "context"):
        for entry in geo[group]:
            v = np.array(entry["vertices"]).reshape(-1, 3)
            if len(v):
                mins = np.minimum(mins, v.min(axis=0))
                maxs = np.maximum(maxs, v.max(axis=0))
    if np.isfinite(mins).all():
        geo["bbox"] = {"min": [round(float(x), 3) for x in mins],
                       "max": [round(float(x), 3) for x in maxs]}
    else:
        geo["bbox"] = {"min": [0, 0, 0], "max": [1, 1, 1]}
    return geo


def write_geometry(geo: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(geo, f)
