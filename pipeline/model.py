"""Intermediate building model shared between the IFC parser and the IDF generator.

All geometry is in world coordinates (meters). Surface polygons are stored as
ordered 3D vertex loops whose Newell normal points *out* of the owning zone,
which matches the EnergyPlus convention (counter-clockwise when viewed from
outside the zone).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# EnergyPlus surface types
WALL = "Wall"
FLOOR = "Floor"
CEILING = "Ceiling"
ROOF = "Roof"

# Outside boundary conditions
OUTDOORS = "Outdoors"
GROUND = "Ground"
SURFACE = "Surface"  # inter-zone, paired with another surface
ADIABATIC = "Adiabatic"

_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def sanitize_name(raw: str, fallback: str = "Unnamed") -> str:
    """Make a string safe for use as an EnergyPlus object name."""
    name = _NAME_RE.sub("_", str(raw or fallback)).strip("_")
    if not name:
        name = fallback
    if name[0].isdigit():
        name = "Z_" + name
    return name[:80]


class NameRegistry:
    """Guarantees unique, sanitized EnergyPlus names."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def unique(self, raw: str, fallback: str = "Unnamed") -> str:
        base = sanitize_name(raw, fallback)
        name = base
        i = 1
        while name.upper() in self._used:
            i += 1
            name = f"{base}_{i}"
        self._used.add(name.upper())
        return name


@dataclass
class WindowSurface:
    name: str
    vertices: np.ndarray  # (N,3) float, same winding as host surface
    host: Optional["Surface"] = None

    @property
    def area(self) -> float:
        return polygon_area(self.vertices)


@dataclass
class Surface:
    name: str
    surface_type: str  # Wall | Floor | Ceiling | Roof
    vertices: np.ndarray  # (N,3) float, outward-facing winding
    zone_name: str
    boundary: str = OUTDOORS  # Outdoors | Ground | Surface | Adiabatic
    boundary_object: str = ""  # counterpart surface name when boundary == Surface
    windows: list[WindowSurface] = field(default_factory=list)

    @property
    def normal(self) -> np.ndarray:
        return newell_normal(self.vertices)

    @property
    def area(self) -> float:
        return polygon_area(self.vertices)

    @property
    def centroid(self) -> np.ndarray:
        return self.vertices.mean(axis=0)


@dataclass
class Zone:
    name: str
    ifc_guid: str = ""
    long_name: str = ""
    storey: str = ""
    floor_area: float = 0.0  # m2
    volume: float = 0.0  # m3
    surfaces: list[Surface] = field(default_factory=list)
    # triangle mesh of the zone volume for the 3D viewer
    mesh_vertices: Optional[np.ndarray] = None  # (V,3)
    mesh_faces: Optional[np.ndarray] = None  # (F,3) int


@dataclass
class ContextElement:
    """Non-zone building element exported for viewer context display."""

    name: str
    ifc_type: str
    ifc_guid: str
    mesh_vertices: np.ndarray
    mesh_faces: np.ndarray


@dataclass
class BuildingModel:
    name: str = "Building"
    source_file: str = ""
    zones: list[Zone] = field(default_factory=list)
    context: list[ContextElement] = field(default_factory=list)
    north_axis_deg: float = 0.0
    notes: list[str] = field(default_factory=list)

    def zone_by_name(self, name: str) -> Optional[Zone]:
        for z in self.zones:
            if z.name == name:
                return z
        return None


# ---------------------------------------------------------------------------
# small polygon helpers used across the pipeline
# ---------------------------------------------------------------------------

def newell_normal(verts: np.ndarray) -> np.ndarray:
    """Robust polygon normal (Newell's method), unnormalized direction kept safe."""
    v = np.asarray(verts, dtype=float)
    n = np.zeros(3)
    for i in range(len(v)):
        a, b = v[i], v[(i + 1) % len(v)]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    norm = np.linalg.norm(n)
    return n / norm if norm > 1e-12 else np.array([0.0, 0.0, 1.0])


def polygon_area(verts: np.ndarray) -> float:
    v = np.asarray(verts, dtype=float)
    if len(v) < 3:
        return 0.0
    total = np.zeros(3)
    for i in range(1, len(v) - 1):
        total += np.cross(v[i] - v[0], v[i + 1] - v[0])
    return float(np.linalg.norm(total) * 0.5)


def plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two orthonormal in-plane axes for a plane with the given normal."""
    n = np.asarray(normal, dtype=float)
    helper = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(helper, n)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    return u, v


def project_to_plane(points: np.ndarray, origin: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    d = np.asarray(points, dtype=float) - origin
    return np.stack([d @ u, d @ v], axis=1)


def point_in_polygon_2d(pt: np.ndarray, poly: np.ndarray) -> bool:
    """Ray-casting point-in-polygon test in 2D."""
    x, y = pt
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def clip_polygon_2d(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """Sutherland-Hodgman clipping; ``clip`` must be convex and CCW."""
    def is_inside(p, a, b):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= -1e-12

    def intersect(p1, p2, a, b):
        d1 = p2 - p1
        d2 = b - a
        denom = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(denom) < 1e-30:
            return p2
        t = ((a[0] - p1[0]) * d2[1] - (a[1] - p1[1]) * d2[0]) / denom
        return p1 + t * d1

    output = [np.asarray(p, dtype=float) for p in subject]
    n = len(clip)
    for i in range(n):
        a, b = clip[i], clip[(i + 1) % n]
        input_list = output
        output = []
        if not input_list:
            break
        s = input_list[-1]
        for p in input_list:
            if is_inside(p, a, b):
                if not is_inside(s, a, b):
                    output.append(intersect(s, p, a, b))
                output.append(p)
            elif is_inside(s, a, b):
                output.append(intersect(s, p, a, b))
            s = p
    return np.array(output) if output else np.zeros((0, 2))


def polygon_area_2d(poly: np.ndarray) -> float:
    if len(poly) < 3:
        return 0.0
    x, y = poly[:, 0], poly[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) * 0.5)


def convex_hull_2d(points: np.ndarray) -> np.ndarray:
    """Andrew's monotone chain; returns CCW hull."""
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) <= 2:
        return np.array(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1])


def simplify_loop(verts: np.ndarray, angle_tol: float = 0.017, dist_tol: float = 5e-3) -> np.ndarray:
    """Drop duplicate and collinear vertices from a 3D loop."""
    v = np.asarray(verts, dtype=float)
    # remove consecutive duplicates
    keep = [0]
    for i in range(1, len(v)):
        if np.linalg.norm(v[i] - v[keep[-1]]) > dist_tol:
            keep.append(i)
    if len(keep) > 1 and np.linalg.norm(v[keep[-1]] - v[keep[0]]) <= dist_tol:
        keep.pop()
    v = v[keep]
    if len(v) < 3:
        return v
    # remove collinear vertices
    out = []
    n = len(v)
    for i in range(n):
        a, b, c = v[(i - 1) % n], v[i], v[(i + 1) % n]
        ab, bc = b - a, c - b
        la, lb = np.linalg.norm(ab), np.linalg.norm(bc)
        if la < 1e-12 or lb < 1e-12:
            continue
        if np.linalg.norm(np.cross(ab / la, bc / lb)) > angle_tol:
            out.append(b)
    return np.array(out) if len(out) >= 3 else v


def mesh_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Signed mesh volume via divergence theorem (absolute value returned)."""
    v = np.asarray(vertices, dtype=float)
    f = np.asarray(faces, dtype=int)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0))
