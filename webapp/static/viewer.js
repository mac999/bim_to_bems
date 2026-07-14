// Three.js viewer: renders zone meshes colored by energy results + context elements.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// color ramps (stops as CSS hex, interpolated in RGB)
const RAMPS = {
  sequential: ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#256abf', '#184f95', '#0d366b'],
  diverging: ['#3987e5', '#86b6ef', '#cfd0cc', '#f0a3a3', '#e66767'],
  viridis: ['#440154', '#414487', '#2a788e', '#22a884', '#7ad151', '#fde725'],
};

function hexToRgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

export function rampColor(ramp, t) {
  const stops = RAMPS[ramp] || RAMPS.sequential;
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  const a = hexToRgb(stops[i]), b = hexToRgb(stops[i + 1]);
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export function rampCss(ramp) {
  const stops = RAMPS[ramp] || RAMPS.sequential;
  return `linear-gradient(90deg, ${stops.join(',')})`;
}

export class Viewer {
  constructor(container, onPick) {
    this.container = container;
    this.onPick = onPick;
    this.zoneMeshes = new Map(); // zone name -> mesh
    this.selected = null;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0d0d0d);
    this.camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    container.appendChild(this.renderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;

    this.scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x30302c, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(80, 140, 60);
    this.scene.add(sun);

    // IFC / EnergyPlus are Z-up; three.js is Y-up
    this.root = new THREE.Group();
    this.root.rotation.x = -Math.PI / 2;
    this.scene.add(this.root);
    this.zoneGroup = new THREE.Group();
    this.contextGroup = new THREE.Group();
    this.edgeGroup = new THREE.Group();
    this.root.add(this.zoneGroup, this.contextGroup, this.edgeGroup);

    this.raycaster = new THREE.Raycaster();
    this.renderer.domElement.addEventListener('pointerdown', e => (this._down = [e.clientX, e.clientY]));
    this.renderer.domElement.addEventListener('pointerup', e => {
      if (!this._down) return;
      const dx = e.clientX - this._down[0], dy = e.clientY - this._down[1];
      if (dx * dx + dy * dy < 16) this._pick(e);
      this._down = null;
    });

    window.addEventListener('resize', () => this.resize());
    this.resize();
    this._animate();
  }

  resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  _animate() {
    requestAnimationFrame(() => this._animate());
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  _buildMesh(entry, material) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(entry.vertices, 3));
    geo.setIndex(entry.faces);
    geo.computeVertexNormals();
    return new THREE.Mesh(geo, material);
  }

  clear() {
    for (const g of [this.zoneGroup, this.contextGroup, this.edgeGroup]) {
      g.children.forEach(c => { c.geometry?.dispose(); c.material?.dispose?.(); });
      g.clear();
    }
    this.zoneMeshes.clear();
    this.selected = null;
  }

  load(geometry) {
    this.clear();

    for (const z of geometry.zones || []) {
      const mat = new THREE.MeshStandardMaterial({
        color: 0x5f5e59, roughness: 0.85, metalness: 0.0,
        transparent: true, opacity: 0.92, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      });
      const mesh = this._buildMesh(z, mat);
      mesh.userData = { zone: z.name, info: z };
      this.zoneGroup.add(mesh);
      this.zoneMeshes.set(z.name.toUpperCase(), mesh);
      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(mesh.geometry, 25),
        new THREE.LineBasicMaterial({ color: 0x0d0d0d, transparent: true, opacity: 0.5 }));
      this.edgeGroup.add(edges);
    }

    const ctxMat = new THREE.MeshStandardMaterial({
      color: 0x4a4a47, roughness: 0.95, transparent: true, opacity: 0.16,
      side: THREE.DoubleSide, depthWrite: false,
    });
    const winMat = new THREE.MeshStandardMaterial({
      color: 0x86b6ef, roughness: 0.4, transparent: true, opacity: 0.25,
      side: THREE.DoubleSide, depthWrite: false,
    });
    for (const c of geometry.context || []) {
      const mesh = this._buildMesh(c, c.type === 'IfcWindow' ? winMat : ctxMat);
      mesh.raycast = () => {}; // context never blocks picking
      this.contextGroup.add(mesh);
    }
    this.fit(geometry.bbox);
  }

  fit(bbox) {
    if (!bbox) return;
    const cx = (bbox.min[0] + bbox.max[0]) / 2;
    const cy = (bbox.min[1] + bbox.max[1]) / 2;
    const cz = (bbox.min[2] + bbox.max[2]) / 2;
    const size = Math.max(bbox.max[0] - bbox.min[0], bbox.max[1] - bbox.min[1], bbox.max[2] - bbox.min[2], 1);
    // root is rotated -90deg about X: world (x,y,z) -> scene (x,z,-y)
    const target = new THREE.Vector3(cx, cz, -cy);
    this.controls.target.copy(target);
    this.camera.position.set(cx + size * 0.9, cz + size * 0.8, -cy + size * 0.9);
    this.camera.near = size / 500;
    this.camera.far = size * 20;
    this.camera.updateProjectionMatrix();
  }

  colorize(values, ramp) {
    // values: Map(upper zone name -> value) or null to reset
    let min = Infinity, max = -Infinity;
    if (values) for (const v of values.values()) { if (v < min) min = v; if (v > max) max = v; }
    const span = max - min;
    for (const [name, mesh] of this.zoneMeshes) {
      const v = values?.get(name);
      if (v === undefined || !isFinite(v)) {
        mesh.material.color.set(0x5f5e59);
      } else {
        const t = span > 1e-9 ? (v - min) / span : 0.5;
        mesh.material.color.set(new THREE.Color(rampColor(ramp, t)));
      }
    }
    return { min, max };
  }

  setOpacity(o) { this.zoneGroup.children.forEach(m => (m.material.opacity = o)); }
  setContextVisible(v) { this.contextGroup.visible = v; }
  setEdgesVisible(v) { this.edgeGroup.visible = v; }

  select(zoneName) {
    if (this.selected) this.selected.material.emissive.set(0x000000);
    this.selected = zoneName ? this.zoneMeshes.get(zoneName.toUpperCase()) : null;
    if (this.selected) this.selected.material.emissive.set(0x2a4a7a);
  }

  _pick(e) {
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((e.clientX - r.left) / r.width) * 2 - 1,
      -((e.clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hits = this.raycaster.intersectObjects(this.zoneGroup.children, false);
    const zone = hits.length ? hits[0].object.userData.zone : null;
    this.select(zone);
    this.onPick?.(zone);
  }
}
