"""IFC -> BuildingModel extraction.

Algorithm overview (improvements over naive pset-based conversion):

1. Real geometry. Every product is meshed with ifcopenshell.geom in world
   coordinates (meters). Thermal zones are derived from ``IfcSpace`` solids.

2. Space shell -> planar surfaces. The triangle mesh of each space is
   clustered into planar regions (normal + plane-offset tolerance), the
   boundary loop of each region is reconstructed by chaining edges that appear
   exactly once, and the loop is simplified (duplicate/collinear vertex
   removal). Loop winding is fixed so its Newell normal points out of the zone
   (the EnergyPlus convention).

3. Boundary conditions. Opposite-facing surface pairs from different zones
   that overlap in-plane (Sutherland-Hodgman clipped area over a convex hull)
   become inter-zone ``Surface`` pairs; unmatched floors near the building base
   become ``Ground``; everything else is ``Outdoors``. Unmatched upward-facing
   surfaces are re-typed as ``Roof``.

4. Windows. Each ``IfcWindow`` mesh is projected onto the best matching
   external wall and inscribed as a rectangle (shrunk iteratively until fully
   inside the host polygon). If a model carries no usable windows, a
   window-to-wall-ratio fallback inscribes centered glazing on external walls.

5. No-space fallback. Models without ``IfcSpace`` (common in as-built or
   structural exports) get one box zone per ``IfcBuildingStorey`` from the
   bounding box of that storey's elements - a standard "shoebox" BEM
   simplification - so the energy pipeline still runs end to end.
"""
from __future__ import annotations

import math
import multiprocessing
import os
from collections import defaultdict

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import ifcopenshell.util.unit

from .model import (
    ADIABATIC, CEILING, FLOOR, GROUND, OUTDOORS, ROOF, SURFACE, WALL,
    BuildingModel, ContextElement, NameRegistry, Surface, WindowSurface, Zone,
    clip_polygon_2d, convex_hull_2d, mesh_volume, newell_normal,
    plane_basis, point_in_polygon_2d, polygon_area, polygon_area_2d,
    project_to_plane, simplify_loop,
)

CONTEXT_TYPES = (
    "IfcWall", "IfcSlab", "IfcRoof", "IfcWindow", "IfcDoor", "IfcColumn",
    "IfcBeam", "IfcStair", "IfcStairFlight", "IfcRailing", "IfcCurtainWall",
    "IfcPlate", "IfcMember", "IfcCovering", "IfcFooting",
    "IfcBuildingElementProxy",
)


def _geom_settings():
    settings = ifcopenshell.geom.settings()
    try:
        settings.set("use-world-coords", True)
    except Exception:
        settings.set(settings.USE_WORLD_COORDS, True)  # older API
    return settings


def _extract_all_meshes(ifc_file) -> dict[str, tuple[str, str, np.ndarray, np.ndarray]]:
    """Mesh every product once. Returns {guid: (ifc_type, name, verts, faces)}."""
    settings = _geom_settings()
    meshes: dict[str, tuple[str, str, np.ndarray, np.ndarray]] = {}
    iterator = ifcopenshell.geom.iterator(
        settings, ifc_file, max(1, multiprocessing.cpu_count() - 1)
    )
    if not iterator.initialize():
        return meshes
    while True:
        shape = iterator.get()
        try:
            element = ifc_file.by_guid(shape.guid)
            verts = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3)
            faces = np.array(shape.geometry.faces, dtype=int).reshape(-1, 3)
            if len(verts) and len(faces):
                meshes[shape.guid] = (
                    element.is_a(), element.Name or "", verts, faces
                )
        except Exception:
            pass
        if not iterator.next():
            break
    return meshes


def _weld(verts: np.ndarray, faces: np.ndarray, tol: float = 1e-4):
    """Merge near-duplicate vertices so edge topology is watertight."""
    keys = np.round(verts / tol).astype(np.int64)
    _, first_idx, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    new_verts = verts[first_idx]
    new_faces = inverse[faces]
    # drop degenerate triangles
    ok = (
        (new_faces[:, 0] != new_faces[:, 1])
        & (new_faces[:, 1] != new_faces[:, 2])
        & (new_faces[:, 0] != new_faces[:, 2])
    )
    return new_verts, new_faces[ok]


# ---------------------------------------------------------------------------
# planar region extraction
# ---------------------------------------------------------------------------

def _cluster_planar_regions(verts: np.ndarray, faces: np.ndarray,
                            angle_tol_deg: float, dist_tol: float):
    """Group triangles into planar regions -> [(unit normal, [face indices])]."""
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    raw_n = np.cross(b - a, c - a)
    lengths = np.linalg.norm(raw_n, axis=1)
    centroids = (a + b + c) / 3.0

    cos_tol = math.cos(math.radians(angle_tol_deg))
    clusters: list[dict] = []
    for i in np.argsort(-lengths):  # seed clusters with the biggest triangles
        if lengths[i] < 1e-12:
            continue
        n = raw_n[i] / lengths[i]
        d = float(n @ centroids[i])
        placed = False
        for cl in clusters:
            if n @ cl["n"] >= cos_tol and abs(cl["n"] @ centroids[i] - cl["d"]) <= dist_tol:
                cl["tris"].append(i)
                placed = True
                break
        if not placed:
            clusters.append({"n": n, "d": d, "tris": [i]})
    return [(cl["n"], cl["tris"]) for cl in clusters]


def _boundary_loops(faces: np.ndarray, tri_idx: list[int]) -> list[list[int]]:
    """Vertex-index loops around a set of triangles (edges used exactly once)."""
    edge_use: dict[tuple[int, int], int] = defaultdict(int)
    directed: dict[tuple[int, int], tuple[int, int]] = {}
    for ti in tri_idx:
        f = faces[ti]
        for k in range(3):
            i, j = int(f[k]), int(f[(k + 1) % 3])
            edge_use[(min(i, j), max(i, j))] += 1
            directed[(min(i, j), max(i, j))] = (i, j)
    nxt: dict[int, list[int]] = defaultdict(list)
    for key, count in edge_use.items():
        if count == 1:
            i, j = directed[key]
            nxt[i].append(j)
    loops: list[list[int]] = []
    visited: set[tuple[int, int]] = set()
    for start in list(nxt.keys()):
        for first in nxt[start]:
            if (start, first) in visited:
                continue
            loop = [start]
            cur, prev = first, start
            visited.add((start, first))
            guard = 0
            while cur != start and guard < 10000:
                loop.append(cur)
                candidates = [v for v in nxt.get(cur, []) if (cur, v) not in visited]
                if not candidates:
                    loop = []
                    break
                nxt_v = candidates[0]
                visited.add((cur, nxt_v))
                prev, cur = cur, nxt_v
                guard += 1
            if len(loop) >= 3:
                loops.append(loop)
    return loops


def _mesh_to_boundary_surfaces(verts: np.ndarray, faces: np.ndarray, cfg: dict) -> list[tuple[str, np.ndarray]]:
    """Space shell mesh -> [(surface_type, ordered outward loop)] list."""
    conv = cfg["conversion"]
    verts, faces = _weld(verts, faces)
    if not len(faces):
        return []
    regions = _cluster_planar_regions(
        verts, faces, conv["plane_merge_angle_deg"], conv["plane_merge_dist"]
    )
    out: list[tuple[str, np.ndarray]] = []
    for normal, tris in regions:
        loops = _boundary_loops(faces, tris)
        if not loops:
            continue
        # keep the largest loop (outer boundary); holes are ignored
        best, best_area = None, 0.0
        for loop in loops:
            poly = verts[loop]
            area = polygon_area(poly)
            if area > best_area:
                best, best_area = poly, area
        if best is None or best_area < conv["min_surface_area"]:
            continue
        poly = simplify_loop(best)
        if len(poly) < 3 or polygon_area(poly) < conv["min_surface_area"]:
            continue
        # enforce outward winding (region normal points out of the space solid)
        if newell_normal(poly) @ normal < 0:
            poly = poly[::-1]
        # cap vertex count for EnergyPlus (fallback: convex hull in-plane)
        if len(poly) > conv["max_vertices_per_surface"]:
            u, v = plane_basis(normal)
            origin = poly.mean(axis=0)
            hull2d = convex_hull_2d(project_to_plane(poly, origin, u, v))
            poly = np.array([origin + p[0] * u + p[1] * v for p in hull2d])
            if newell_normal(poly) @ normal < 0:
                poly = poly[::-1]
        nz = normal[2]
        if nz <= -0.5:
            stype = FLOOR
        elif nz >= 0.5:
            stype = CEILING  # promoted to Roof later if it faces outdoors
        else:
            stype = WALL
        out.append((stype, poly))
    return out


# ---------------------------------------------------------------------------
# boundary condition resolution
# ---------------------------------------------------------------------------

def _mirror_pair_geometry(si: Surface, sj: Surface, conv: dict) -> bool:
    """Rebuild a matched pair as exact mirror polygons (EnergyPlus requires
    inter-zone Surface pairs to have identical, opposite-wound vertices).

    The shared polygon is the intersection of the two surfaces' convex hulls,
    placed on each surface's own plane.
    """
    ni, ci = si.normal, si.centroid
    nj, cj = sj.normal, sj.centroid
    u, v = plane_basis(ni)
    hull_i = convex_hull_2d(project_to_plane(si.vertices, ci, u, v))
    hull_j = convex_hull_2d(project_to_plane(sj.vertices, ci, u, v))
    if len(hull_i) < 3 or len(hull_j) < 3:
        return False
    shared = clip_polygon_2d(hull_j, hull_i)
    if polygon_area_2d(shared) < conv["min_surface_area"]:
        return False
    # order CCW about ni so the rebuilt polygon faces out of zone i
    area2 = 0.0
    for k in range(len(shared)):
        a, b = shared[k], shared[(k + 1) % len(shared)]
        area2 += a[0] * b[1] - b[0] * a[1]
    if area2 < 0:
        shared = shared[::-1]
    pts_i = np.array([ci + p[0] * u + p[1] * v for p in shared])
    # project the shared polygon onto zone j's plane (offset by wall thickness)
    pts_j = pts_i - np.outer((pts_i - cj) @ nj, nj)
    si.vertices = pts_i
    sj.vertices = pts_j[::-1]  # exact mirror: same loop, reversed winding
    return True


def _pair_adjacent_surfaces(model: BuildingModel, cfg: dict) -> None:
    conv = cfg["conversion"]
    surfaces = [s for z in model.zones for s in z.surfaces]
    candidates: list[tuple[float, Surface, Surface]] = []
    had_candidate: set[int] = set()
    for i in range(len(surfaces)):
        si = surfaces[i]
        ni, ci = si.normal, si.centroid
        u, v = plane_basis(ni)
        hull_i = convex_hull_2d(project_to_plane(si.vertices, ci, u, v))
        if len(hull_i) < 3:
            continue
        for j in range(i + 1, len(surfaces)):
            sj = surfaces[j]
            if si.zone_name == sj.zone_name:
                continue
            if ni @ sj.normal > -0.95:  # must face each other
                continue
            gap = abs(float(ni @ (sj.centroid - ci)))
            if gap > conv["adjacency_gap"]:
                continue
            proj_j = project_to_plane(sj.vertices, ci, u, v)
            overlap = polygon_area_2d(clip_polygon_2d(proj_j, hull_i))
            min_area = min(si.area, sj.area)
            if min_area > 0 and overlap / min_area >= conv["adjacency_overlap_ratio"]:
                candidates.append((overlap, si, sj))
                had_candidate.add(id(si))
                had_candidate.add(id(sj))
    candidates.sort(key=lambda t: -t[0])
    matched: set[int] = set()
    for overlap, si, sj in candidates:
        if id(si) in matched or id(sj) in matched:
            continue
        if not _mirror_pair_geometry(si, sj, conv):
            continue
        matched.add(id(si))
        matched.add(id(sj))
        si.boundary, si.boundary_object = SURFACE, sj.name
        sj.boundary, sj.boundary_object = SURFACE, si.name
    # interior surfaces that overlapped another zone but lost the one-to-one
    # match: treat as adiabatic rather than (wrongly) exposing them outdoors
    for s in surfaces:
        if id(s) in had_candidate and id(s) not in matched:
            s.boundary = ADIABATIC


def _resolve_boundaries(model: BuildingModel, cfg: dict) -> None:
    conv = cfg["conversion"]
    _pair_adjacent_surfaces(model, cfg)
    floor_zs = [
        float(s.vertices[:, 2].min())
        for z in model.zones for s in z.surfaces if s.surface_type == FLOOR
    ]
    base_z = min(floor_zs) if floor_zs else 0.0
    for z in model.zones:
        for s in z.surfaces:
            if s.boundary in (SURFACE, ADIABATIC):
                continue
            if s.surface_type == FLOOR:
                near_ground = s.vertices[:, 2].min() <= base_z + conv["ground_level_tol"]
                s.boundary = GROUND if near_ground else OUTDOORS
            elif s.surface_type == CEILING:
                s.surface_type = ROOF
                s.boundary = OUTDOORS
            else:
                s.boundary = OUTDOORS


# ---------------------------------------------------------------------------
# window placement
# ---------------------------------------------------------------------------

def _inscribe_rect(host: Surface, rect2d: np.ndarray, poly2d: np.ndarray,
                   origin: np.ndarray, u: np.ndarray, v: np.ndarray,
                   min_area: float = 0.1) -> np.ndarray | None:
    """Shrink a 2D rect about its center until all corners are inside poly2d."""
    center = rect2d.mean(axis=0)
    rect = rect2d.copy()
    for _ in range(8):
        if all(point_in_polygon_2d(p, poly2d) for p in rect):
            area = polygon_area_2d(rect)
            if area < min_area or area > 0.9 * host.area:
                return None
            return np.array([origin + p[0] * u + p[1] * v for p in rect])
        rect = center + (rect - center) * 0.85
    return None


def _rect_2d(min_x, min_y, max_x, max_y) -> np.ndarray:
    return np.array([
        [min_x, min_y], [max_x, min_y], [max_x, max_y], [min_x, max_y]
    ], dtype=float)


def _place_ifc_windows(model: BuildingModel, meshes: dict, reg: NameRegistry, cfg: dict) -> int:
    ext_walls = [
        s for z in model.zones for s in z.surfaces
        if s.surface_type == WALL and s.boundary == OUTDOORS
    ]
    placed = 0
    for guid, (ifc_type, name, verts, _faces) in meshes.items():
        if not ifc_type.startswith(("IfcWindow", "IfcCurtainWall")):
            continue
        center = verts.mean(axis=0)
        best: tuple[float, Surface] | None = None
        for s in ext_walls:
            n, c = s.normal, s.centroid
            dist = abs(float(n @ (center - c)))
            if dist > 0.6:
                continue
            u, v = plane_basis(n)
            poly2d = project_to_plane(s.vertices, c, u, v)
            pc = project_to_plane(center[None, :], c, u, v)[0]
            if not point_in_polygon_2d(pc, poly2d):
                continue
            if best is None or dist < best[0]:
                best = (dist, s)
        if best is None:
            continue
        host = best[1]
        n, c = host.normal, host.centroid
        u, v = plane_basis(n)
        poly2d = project_to_plane(host.vertices, c, u, v)
        pw = project_to_plane(verts, c, u, v)
        margin = 0.04
        rect = _rect_2d(pw[:, 0].min() + margin, pw[:, 1].min() + margin,
                        pw[:, 0].max() - margin, pw[:, 1].max() - margin)
        if polygon_area_2d(rect) < 0.1:
            continue
        verts3d = _inscribe_rect(host, rect, poly2d, c, u, v)
        if verts3d is None:
            continue
        # match host winding so EnergyPlus accepts the subsurface
        if newell_normal(verts3d) @ n < 0:
            verts3d = verts3d[::-1]
        win = WindowSurface(reg.unique(f"{host.name}_Win_{name or guid[:6]}"), verts3d, host)
        host.windows.append(win)
        placed += 1
    return placed


def _place_wwr_windows(model: BuildingModel, reg: NameRegistry, cfg: dict) -> int:
    wwr = float(cfg["conversion"]["window_wall_ratio"])
    if wwr <= 0:
        return 0
    placed = 0
    for z in model.zones:
        for s in z.surfaces:
            if s.surface_type != WALL or s.boundary != OUTDOORS or s.windows:
                continue
            n, c = s.normal, s.centroid
            u, v = plane_basis(n)
            poly2d = project_to_plane(s.vertices, c, u, v)
            min_x, min_y = poly2d.min(axis=0)
            max_x, max_y = poly2d.max(axis=0)
            w, h = max_x - min_x, max_y - min_y
            if w < 0.4 or h < 0.4:
                continue
            scale = math.sqrt(min(0.85, wwr * s.area / max(1e-9, w * h)))
            cw, ch = w * scale, h * scale
            cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
            rect = _rect_2d(cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
            verts3d = _inscribe_rect(s, rect, poly2d, c, u, v)
            if verts3d is None:
                continue
            if newell_normal(verts3d) @ n < 0:
                verts3d = verts3d[::-1]
            s.windows.append(WindowSurface(reg.unique(f"{s.name}_WWR_Win"), verts3d, s))
            placed += 1
    return placed


# ---------------------------------------------------------------------------
# zone construction
# ---------------------------------------------------------------------------

def _zone_from_space_mesh(space, verts, faces, reg: NameRegistry, cfg: dict) -> Zone | None:
    label = space.Name or space.LongName or f"Space_{space.GlobalId[:8]}"
    zone = Zone(name=reg.unique(label, "Zone"), ifc_guid=space.GlobalId,
                long_name=space.LongName or "")
    container = None
    try:
        container = ifcopenshell.util.element.get_container(
            space, ifc_class="IfcBuildingStorey")
    except TypeError:
        container = ifcopenshell.util.element.get_container(space)
    if container is None:  # spaces are often aggregated, not "contained"
        agg = getattr(space, "Decomposes", None) or []
        for rel in agg:
            parent = rel.RelatingObject
            if parent is not None and parent.is_a("IfcBuildingStorey"):
                container = parent
                break
    if container is not None:
        zone.storey = getattr(container, "Name", "") or ""
    surfaces = _mesh_to_boundary_surfaces(verts, faces, cfg)
    if not surfaces:
        return None
    for stype, poly in surfaces:
        zone.surfaces.append(Surface(
            name=reg.unique(f"{zone.name}_{stype}"),
            surface_type=stype, vertices=poly, zone_name=zone.name,
        ))
    zone.volume = mesh_volume(verts, faces)
    zone.floor_area = sum(
        s.area * abs(s.normal[2]) for s in zone.surfaces if s.surface_type == FLOOR
    )
    # fall back to quantity psets when geometry-derived values look degenerate
    psets = ifcopenshell.util.element.get_psets(space)
    qto = psets.get(cfg["pset_metadata"]["space_quantities"], {})
    if zone.floor_area < 0.5:
        zone.floor_area = float(qto.get("GrossFloorArea", qto.get("NetFloorArea", 10.0)))
    if zone.volume < 1.0:
        zone.volume = float(qto.get("GrossVolume", qto.get("NetVolume", zone.floor_area * 2.7)))
    zone.mesh_vertices, zone.mesh_faces = verts, faces
    return zone


def _box_zone(name: str, guid: str, bmin: np.ndarray, bmax: np.ndarray,
              reg: NameRegistry) -> Zone:
    """Axis-aligned box zone (fallback for models without IfcSpace)."""
    x0, y0, z0 = bmin
    x1, y1, z1 = bmax
    zone = Zone(name=name, ifc_guid=guid)
    P = lambda x, y, z: np.array([x, y, z], dtype=float)

    def add(stype, pts):
        zone.surfaces.append(Surface(
            name=reg.unique(f"{name}_{stype}"), surface_type=stype,
            vertices=np.array(pts), zone_name=name,
        ))

    add(FLOOR, [P(x0, y0, z0), P(x0, y1, z0), P(x1, y1, z0), P(x1, y0, z0)])
    add(CEILING, [P(x0, y0, z1), P(x1, y0, z1), P(x1, y1, z1), P(x0, y1, z1)])
    add(WALL, [P(x0, y0, z1), P(x0, y0, z0), P(x1, y0, z0), P(x1, y0, z1)])   # south (-y)
    add(WALL, [P(x1, y0, z1), P(x1, y0, z0), P(x1, y1, z0), P(x1, y1, z1)])   # east (+x)
    add(WALL, [P(x1, y1, z1), P(x1, y1, z0), P(x0, y1, z0), P(x0, y1, z1)])   # north (+y)
    add(WALL, [P(x0, y1, z1), P(x0, y1, z0), P(x0, y0, z0), P(x0, y0, z1)])   # west (-x)

    zone.floor_area = float((x1 - x0) * (y1 - y0))
    zone.volume = float(zone.floor_area * (z1 - z0))
    # simple box mesh for the viewer
    corners = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ])
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = []
    for q in quads:
        tris += [[q[0], q[1], q[2]], [q[0], q[2], q[3]]]
    zone.mesh_vertices, zone.mesh_faces = corners, np.array(tris, dtype=int)
    return zone


def _fallback_storey_zones(ifc_file, meshes: dict, reg: NameRegistry,
                           model: BuildingModel, cfg: dict) -> list[Zone]:
    unit_scale = 1.0
    try:
        unit_scale = ifcopenshell.util.unit.calculate_unit_scale(ifc_file)
    except Exception:
        pass
    element_meshes = [
        (verts, faces) for _, (t, _n, verts, faces) in meshes.items()
        if t.startswith(("IfcWall", "IfcSlab", "IfcRoof", "IfcCurtainWall", "IfcColumn"))
    ]
    if not element_meshes:
        return []
    all_pts = np.vstack([v for v, _ in element_meshes])
    gmin, gmax = all_pts.min(axis=0), all_pts.max(axis=0)

    storeys = []
    for st in ifc_file.by_type("IfcBuildingStorey"):
        elev = getattr(st, "Elevation", None)
        if elev is not None:
            storeys.append((float(elev) * unit_scale, st))
    storeys.sort(key=lambda t: t[0])
    # deduplicate storeys at (almost) the same elevation
    levels: list[tuple[float, object]] = []
    for elev, st in storeys:
        if not levels or elev - levels[-1][0] > 0.5:
            levels.append((elev, st))

    zones: list[Zone] = []
    if not levels:
        zone = _box_zone(reg.unique(model.name or "Building_Zone"), "", gmin, gmax, reg)
        model.notes.append("No IfcSpace/IfcBuildingStorey: single bounding-box zone used.")
        return [zone]

    for idx, (elev, st) in enumerate(levels):
        top = levels[idx + 1][0] if idx + 1 < len(levels) else float(gmax[2])
        if top - elev < 1.0:
            top = elev + 3.0
        # bbox of elements that live (mostly) in this storey band
        pts = [
            v for v, _ in element_meshes
            if elev - 0.5 <= float(np.median(v[:, 2])) <= top + 0.5
        ]
        band = np.vstack(pts) if pts else all_pts
        bmin = np.array([band[:, 0].min(), band[:, 1].min(), elev])
        bmax = np.array([band[:, 0].max(), band[:, 1].max(), top])
        if (bmax[0] - bmin[0]) < 1.0 or (bmax[1] - bmin[1]) < 1.0:
            continue
        label = getattr(st, "Name", None) or f"Storey_{idx + 1}"
        zones.append(_box_zone(reg.unique(label, "Storey"), getattr(st, "GlobalId", ""), bmin, bmax, reg))
    model.notes.append(
        f"No IfcSpace found: generated {len(zones)} box zone(s) from storey bounding boxes."
    )
    return zones


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def parse_ifc(ifc_path: str, cfg: dict, verbose: bool = True) -> BuildingModel:
    ifc_file = ifcopenshell.open(ifc_path)
    reg = NameRegistry()

    building_name = "Building"
    buildings = ifc_file.by_type("IfcBuilding")
    if buildings and buildings[0].Name:
        building_name = buildings[0].Name
    model = BuildingModel(
        name=sanitized_building_name(building_name),
        source_file=os.path.basename(ifc_path),
    )

    if verbose:
        print(f"[ifc] meshing products from {os.path.basename(ifc_path)} ...")
    meshes = _extract_all_meshes(ifc_file)
    if verbose:
        print(f"[ifc] {len(meshes)} product meshes extracted")

    # context elements for the viewer
    for guid, (ifc_type, name, verts, faces) in meshes.items():
        if ifc_type.startswith(CONTEXT_TYPES):
            model.context.append(ContextElement(
                name=name or ifc_type, ifc_type=ifc_type, ifc_guid=guid,
                mesh_vertices=verts, mesh_faces=faces,
            ))

    spaces = ifc_file.by_type("IfcSpace")
    for space in spaces:
        entry = meshes.get(space.GlobalId)
        if entry is None:
            model.notes.append(f"Space {space.GlobalId} has no geometry; skipped.")
            continue
        _t, _n, verts, faces = entry
        zone = _zone_from_space_mesh(space, verts, faces, reg, cfg)
        if zone is not None:
            model.zones.append(zone)
        else:
            model.notes.append(f"Space {space.GlobalId}: surface extraction failed; skipped.")

    if not model.zones:
        model.zones = _fallback_storey_zones(ifc_file, meshes, reg, model, cfg)
    if not model.zones:
        raise RuntimeError("No thermal zones could be derived from the IFC model.")

    _resolve_boundaries(model, cfg)

    mode = cfg["conversion"].get("window_mode", "auto")
    n_win = 0
    if mode == "auto":
        n_win = _place_ifc_windows(model, meshes, reg, cfg)
        if n_win and verbose:
            print(f"[ifc] {n_win} IfcWindow(s) projected onto external walls")
    if mode != "none" and n_win == 0:  # auto found nothing usable, or mode == "wwr"
        n_win = _place_wwr_windows(model, reg, cfg)
        if n_win:
            model.notes.append(f"Windows via WWR fallback: {n_win} placed.")

    if verbose:
        n_surf = sum(len(z.surfaces) for z in model.zones)
        print(f"[ifc] model: {len(model.zones)} zones, {n_surf} surfaces, "
              f"{n_win} windows, {len(model.context)} context elements")
        for note in model.notes:
            print(f"[ifc][note] {note}")
    return model


def sanitized_building_name(raw: str) -> str:
    from .model import sanitize_name
    return sanitize_name(raw, "Building")
