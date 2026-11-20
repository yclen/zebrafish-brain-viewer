import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';

// ---------------------------------------------------------------- state
const regions = new Map();      // id -> { ...data, mesh, row, li, checkbox }
let roots = [];                 // top-level region ids
let outlineMesh = null;
let selectedId = null;
let isolated = false;
let opacity = 1;
let colorMode = 'atlas';
let currentPreset = 'regions';

const DIVISION_COLORS = { forebrain: '#7fc97f', midbrain: '#6baed6', hindbrain: '#fdae6b', peripheral: '#bc80bd', other: '#bbbbbb' };
const SELECT_EMISSIVE = new THREE.Color('#ffb703');
const isAF = (r) => /^retinal arborization field/.test(r.name);

// ---------------------------------------------------------------- three.js setup
const canvas = document.getElementById('canvas');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor('#0f1216');

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, 1, 20000);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.autoRotateSpeed = 1.2;

scene.add(new THREE.HemisphereLight('#ffffff', '#3a3f47', 1.4));
const keyLight = new THREE.DirectionalLight('#ffffff', 1.6);
keyLight.position.set(0.6, 1, 0.8);
camera.add(keyLight);          // light follows the camera so the visible side is always lit
const fill = new THREE.DirectionalLight('#9fc3ff', 0.5);
fill.position.set(-1, -0.4, -0.6);
camera.add(fill);
scene.add(camera);

// Atlas coordinates: x = left-right, y = anterior->posterior, z = ventral->dorsal (µm).
// Rotate so that dorsal is +Y (up) and anterior is +Z (towards the default camera).
const brain = new THREE.Group(); // rotated after the meshes are attached (see load())
scene.add(brain);
let brainRadius = 500;

function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas.parentElement);

// ---------------------------------------------------------------- loading
const loadingEl = document.getElementById('loading');
const loadingPct = document.getElementById('loading-pct');

async function load() {
  const meta = await fetch('data/regions.json').then((r) => r.json());
  for (const r of meta.regions) regions.set(r.id, { ...r, mesh: null });
  roots = meta.regions.filter((r) => r.parent === null).map((r) => r.id);

  const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
  const gltf = await loader.loadAsync('data/brain.glb', (e) => {
    if (e.total) loadingPct.textContent = `${Math.round((100 * e.loaded) / e.total)}%`;
  });

  const meshes = [];
  gltf.scene.updateMatrixWorld(true);
  brain.updateMatrixWorld(true);
  gltf.scene.traverse((o) => { if (o.isMesh) meshes.push(o); });
  for (const o of meshes) {
    const id = Number(o.name);
    o.geometry.computeBoundingSphere();
    if (id === 0) {
      outlineMesh = o;
      o.material = new THREE.MeshStandardMaterial({
        color: '#c9d3df', transparent: true, opacity: 0.1, depthWrite: false, roughness: 0.9, side: THREE.FrontSide,
      });
      o.renderOrder = 10;
    } else if (regions.has(id)) {
      const r = regions.get(id);
      r.mesh = o;
      o.material = new THREE.MeshStandardMaterial({ color: r.color, roughness: 0.55, metalness: 0.05 });
      o.userData.regionId = id;
    }
    brain.attach(o); // keeps world transform (dequantization scale/offset) while re-parenting
  }

  // Orient (dorsal up, anterior toward +Z) and centre the brain on the origin using the outline.
  brain.rotation.x = -Math.PI / 2;
  brain.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(outlineMesh || brain);
  const center = box.getCenter(new THREE.Vector3());
  brain.position.sub(center);
  brainRadius = box.getSize(new THREE.Vector3()).length() / 2;
  controls.maxDistance = brainRadius * 8;
  controls.minDistance = brainRadius * 0.15;

  buildTree();
  applyColors();
  applyPreset('regions');
  setView('oblique', false);
  resize();
  loadingEl.remove();
  window.zebrafishViewer = { THREE, scene, camera, controls, brain, regions, outlineMesh }; // for console debugging
}

// ---------------------------------------------------------------- visibility logic
function ancestors(id) {
  const out = [];
  let r = regions.get(id);
  while (r && r.parent !== null) { out.push(r.parent); r = regions.get(r.parent); }
  return out;
}
function descendants(id) {
  const out = [];
  const stack = [...regions.get(id).children];
  while (stack.length) { const c = stack.pop(); out.push(c); stack.push(...regions.get(c).children); }
  return out;
}
function setVisible(id, on) {
  const r = regions.get(id);
  r.visible = on && r.hasMesh;
  if (r.mesh) r.mesh.visible = r.visible;
  if (r.checkbox) r.checkbox.checked = r.visible;
}
// Turning a region on hides its ancestors and descendants, so nested meshes never overlap.
function showExclusive(id) {
  for (const a of ancestors(id)) setVisible(a, false);
  for (const d of descendants(id)) setVisible(d, false);
  setVisible(id, true);
}
// Show a node's subregions in place of the node. Children without a mesh are skipped; if the
// children do not cover the parent (e.g. pretectum -> only its retinal arborization fields) the
// parent stays visible, faded, so the region does not vanish.
function expand(id) {
  const r = regions.get(id);
  if (!r.children.length) return;
  const kids = r.children.filter((c) => regions.get(c).hasMesh);
  const partial = r.children.every((c) => isAF(regions.get(c)));
  for (const d of descendants(id)) setVisible(d, false);
  setVisible(id, partial);
  for (const c of kids) setVisible(c, true);
  applyMaterials();
}
function collapse(id) {
  const r = regions.get(id);
  if (r.parent === null) return;
  showExclusive(r.parent);
  applyMaterials();
}

function presetSet(name) {
  const set = new Set();
  const all = [...regions.values()];
  if (name === 'divisions') for (const id of roots) set.add(id);
  if (name === 'regions') {
    for (const id of roots) {
      const kids = regions.get(id).children;
      if (kids.length) kids.forEach((k) => set.add(k)); else set.add(id);
    }
  }
  if (name === 'detail') {
    const walk = (id) => {
      const r = regions.get(id);
      if (!r.children.length || r.children.every((c) => isAF(regions.get(c)))) { set.add(id); return; }
      r.children.forEach(walk);
    };
    roots.forEach(walk);
  }
  return new Set([...set].filter((id) => all.find((r) => r.id === id).hasMesh));
}
function applyPreset(name) {
  currentPreset = name;
  isolated = false;
  const set = presetSet(name);
  for (const id of regions.keys()) setVisible(id, set.has(id));
  document.querySelectorAll('[data-preset]').forEach((b) => b.classList.toggle('active', b.dataset.preset === name));
  // Open the tree down to the level being shown.
  for (const r of regions.values()) {
    if (!r.li) continue;
    const showsChildren = r.children.some((c) => set.has(c) || descendants(c).some((d) => set.has(d)));
    r.li.classList.toggle('collapsed', !showsChildren);
  }
  applyMaterials();
  updateInfo();
}

// ---------------------------------------------------------------- materials / colours
function hashJitter(id) { const x = Math.sin(id * 12.9898) * 43758.5453; return x - Math.floor(x); }
function regionColor(r) {
  if (colorMode === 'atlas') return new THREE.Color(r.color);
  const c = new THREE.Color(DIVISION_COLORS[r.division] || DIVISION_COLORS.other);
  const hsl = {}; c.getHSL(hsl);
  const j = hashJitter(r.id);
  return new THREE.Color().setHSL((hsl.h + (j - 0.5) * 0.06 + 1) % 1, hsl.s * (0.75 + j * 0.4), hsl.l * (0.75 + j * 0.45));
}
function applyColors() {
  for (const r of regions.values()) {
    const c = '#' + regionColor(r).getHexString();
    r.displayColor = c;
    if (r.mesh) r.mesh.material.color.set(c);
    if (r.swatch) r.swatch.style.background = c;
  }
  document.getElementById('legend').classList.toggle('show', colorMode === 'division');
  if (selectedId !== null) updateInfo();
}
function applyMaterials() {
  // When a parent is shown together with its own subregions (partial expand), fade the parent.
  const visibleIds = new Set([...regions.values()].filter((r) => r.visible).map((r) => r.id));
  for (const r of regions.values()) {
    if (!r.mesh) continue;
    const m = r.mesh.material;
    const containsVisibleChild = r.children.some((c) => visibleIds.has(c));
    let a = containsVisibleChild ? Math.min(opacity, 0.25) : opacity;
    const isSel = r.id === selectedId;
    const related = selectedId !== null && (isSel || ancestors(selectedId).includes(r.id) || descendants(selectedId).includes(r.id));
    if (isolated && selectedId !== null && !related) a = Math.min(a, 0.06);
    m.transparent = a < 1;
    m.opacity = a;
    m.depthWrite = a >= 1;
    m.emissive.copy(isSel ? SELECT_EMISSIVE : new THREE.Color(0, 0, 0));
    m.emissiveIntensity = isSel ? 0.35 : 0;
    r.mesh.renderOrder = a < 1 ? 5 : 0;
    m.needsUpdate = true;
  }
}

// ---------------------------------------------------------------- tree UI
const treeEl = document.getElementById('tree');
function buildTree() {
  const make = (id, parentUl) => {
    const r = regions.get(id);
    const li = document.createElement('li');
    li.setAttribute('role', 'treeitem');
    const row = document.createElement('div');
    row.className = 'row' + (r.hasMesh ? '' : ' no-mesh');
    row.title = r.hasMesh ? r.name : `${r.name} (no 3D mesh in atlas)`;
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = r.children.length ? '▾' : '';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.disabled = !r.hasMesh;
    cb.setAttribute('aria-label', `Show ${r.name}`);
    const sw = document.createElement('span');
    sw.className = 'swatch';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = r.name;
    row.append(caret, cb, sw, name);
    li.append(row);
    Object.assign(r, { li, row, checkbox: cb, swatch: sw });

    caret.addEventListener('click', (e) => {
      e.stopPropagation();
      li.classList.toggle('collapsed');
      caret.textContent = r.children.length ? (li.classList.contains('collapsed') ? '▸' : '▾') : '';
    });
    cb.addEventListener('click', (e) => e.stopPropagation());
    cb.addEventListener('change', () => {
      if (cb.checked) showExclusive(id); else setVisible(id, false);
      applyMaterials();
    });
    row.addEventListener('click', () => select(id, { reveal: true }));

    if (r.children.length) {
      const ul = document.createElement('ul');
      ul.setAttribute('role', 'group');
      r.children.forEach((c) => make(c, ul));
      li.append(ul);
    }
    parentUl.append(li);
  };
  roots.forEach((id) => make(id, treeEl));
  const obs = new MutationObserver(() => {
    for (const r of regions.values()) if (r.li && r.children.length) r.li.querySelector(':scope > .row .caret').textContent = r.li.classList.contains('collapsed') ? '▸' : '▾';
  });
  obs.observe(treeEl, { subtree: true, attributes: true, attributeFilter: ['class'] });
}

document.getElementById('search').addEventListener('input', (e) => {
  const q = e.target.value.trim().toLowerCase();
  for (const r of regions.values()) { r.li.classList.remove('filtered-out'); r.row.classList.remove('match'); }
  if (!q) return;
  const keep = new Set();
  for (const r of regions.values()) {
    if (r.name.toLowerCase().includes(q) || r.description.toLowerCase().includes(q)) {
      r.row.classList.add('match');
      keep.add(r.id);
      ancestors(r.id).forEach((a) => keep.add(a));
    }
  }
  for (const r of regions.values()) {
    r.li.classList.toggle('filtered-out', !keep.has(r.id));
    if (keep.has(r.id) && r.children.some((c) => keep.has(c))) r.li.classList.remove('collapsed');
  }
});

// ---------------------------------------------------------------- selection + info panel
function select(id, { reveal = false } = {}) {
  if (selectedId !== null) regions.get(selectedId).row.classList.remove('selected');
  selectedId = id;
  if (id !== null) {
    const r = regions.get(id);
    r.row.classList.add('selected');
    if (reveal && r.hasMesh && !r.visible) showExclusive(id);
    ancestors(id).forEach((a) => regions.get(a).li.classList.remove('collapsed'));
    r.row.scrollIntoView({ block: 'nearest' });
  } else {
    isolated = false;
  }
  applyMaterials();
  updateInfo();
}

const info = document.getElementById('info');
const infoEmpty = document.getElementById('info-empty');
function updateInfo() {
  if (selectedId === null) { info.hidden = true; infoEmpty.hidden = false; return; }
  const r = regions.get(selectedId);
  info.hidden = false; infoEmpty.hidden = true;
  const crumbs = document.getElementById('crumbs');
  crumbs.replaceChildren();
  ancestors(r.id).reverse().forEach((a, i) => {
    if (i) crumbs.append(' › ');
    const link = document.createElement('a');
    link.textContent = regions.get(a).name;
    link.addEventListener('click', () => select(a, { reveal: true }));
    crumbs.append(link);
  });
  document.getElementById('info-name').textContent = r.name;
  const div = document.getElementById('info-division');
  div.textContent = r.division === 'peripheral' ? 'peripheral nervous system' : r.division;
  div.style.borderColor = DIVISION_COLORS[r.division];
  document.getElementById('info-desc').textContent = r.description + (r.hasMesh ? '' : ' (Not shown in 3D.)');
  const iso = document.getElementById('btn-isolate');
  iso.textContent = isolated ? 'Show everything' : 'Show only this';
  iso.disabled = !r.hasMesh;
  document.getElementById('btn-expand').disabled = !r.children.some((c) => regions.get(c).hasMesh);
  document.getElementById('btn-collapse').disabled = r.parent === null;
  const wrap = document.getElementById('info-children-wrap');
  const list = document.getElementById('info-children');
  list.replaceChildren();
  wrap.hidden = !r.children.length;
  for (const c of r.children) {
    const cr = regions.get(c);
    const li = document.createElement('li');
    const sw = document.createElement('i');
    sw.className = 'swatch';
    sw.style.background = cr.displayColor;
    const name = document.createElement('span');
    name.textContent = cr.name;
    li.append(sw, name);
    li.addEventListener('click', () => select(c, { reveal: true }));
    list.append(li);
  }
}
document.getElementById('btn-isolate').addEventListener('click', () => {
  if (selectedId === null) return;
  const r = regions.get(selectedId);
  if (!isolated && !r.visible) showExclusive(selectedId);
  isolated = !isolated;
  applyMaterials();
  updateInfo();
});
document.getElementById('btn-expand').addEventListener('click', () => { if (selectedId !== null) { expand(selectedId); updateInfo(); } });
document.getElementById('btn-collapse').addEventListener('click', () => {
  if (selectedId === null) return;
  const p = regions.get(selectedId).parent;
  collapse(selectedId);
  select(p);
});

// ---------------------------------------------------------------- picking
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const tooltip = document.getElementById('tooltip');
function pick(ev) {
  const rect = canvas.getBoundingClientRect();
  pointer.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const targets = [...regions.values()].filter((r) => r.mesh && r.visible && r.mesh.material.opacity > 0.1).map((r) => r.mesh);
  const hits = raycaster.intersectObjects(targets, false);
  if (!hits.length) return null;
  // Prefer the nearest opaque hit; faded parents only win if nothing else is under the cursor.
  const solid = hits.find((h) => h.object.material.opacity >= 0.5);
  return (solid || hits[0]).object.userData.regionId;
}
let hoverFrame = 0;
canvas.addEventListener('pointermove', (ev) => {
  if (ev.buttons) { tooltip.hidden = true; return; }
  if (hoverFrame) return;
  hoverFrame = requestAnimationFrame(() => {
    hoverFrame = 0;
    const id = pick(ev);
    if (id == null) { tooltip.hidden = true; canvas.style.cursor = ''; return; }
    const rect = canvas.getBoundingClientRect();
    tooltip.textContent = regions.get(id).name;
    tooltip.style.left = `${ev.clientX - rect.left}px`;
    tooltip.style.top = `${ev.clientY - rect.top}px`;
    tooltip.hidden = false;
    canvas.style.cursor = 'pointer';
  });
});
canvas.addEventListener('pointerleave', () => { tooltip.hidden = true; });
let downAt = null;
canvas.addEventListener('pointerdown', (ev) => { downAt = [ev.clientX, ev.clientY]; });
canvas.addEventListener('pointerup', (ev) => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5 || ev.button !== 0) return;
  const id = pick(ev);
  select(id ?? null);
});
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.activeElement?.id !== 'search') select(null);
});

// ---------------------------------------------------------------- camera views
const VIEWS = {
  // Dorsal: looking down, anterior towards the top of the screen.
  dorsal: [0, 1, -0.001],
  ventral: [0, -1, -0.001],
  lateral: [1, 0.05, 0],
  anterior: [0, 0.15, 1],
  oblique: [0.75, 0.6, 0.85],
};
let tween = null;
function setView(name, animate = true) {
  const dir = new THREE.Vector3(...VIEWS[name]).normalize();
  const dist = brainRadius / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.15 / Math.min(1, camera.aspect);
  const to = dir.multiplyScalar(dist);
  if (!animate) { camera.position.copy(to); controls.target.set(0, 0, 0); controls.update(); return; }
  tween = { from: camera.position.clone(), fromT: controls.target.clone(), to, start: performance.now(), t: 0 };
}
document.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
document.querySelectorAll('[data-preset]').forEach((b) => b.addEventListener('click', () => applyPreset(b.dataset.preset)));
document.getElementById('show-all-level').addEventListener('click', () => applyPreset(currentPreset));
document.getElementById('hide-all').addEventListener('click', () => {
  for (const id of regions.keys()) setVisible(id, false);
  applyMaterials();
});
document.getElementById('outline-toggle').addEventListener('change', (e) => { if (outlineMesh) outlineMesh.visible = e.target.checked; });
document.getElementById('opacity').addEventListener('input', (e) => { opacity = Number(e.target.value); applyMaterials(); });
document.getElementById('color-mode').addEventListener('change', (e) => { colorMode = e.target.value; applyColors(); });
document.getElementById('spin').addEventListener('change', (e) => { controls.autoRotate = e.target.checked; });
document.getElementById('toggle-tree').addEventListener('click', () => {
  document.querySelector('.layout').classList.toggle('tree-hidden');
});

// ---------------------------------------------------------------- render loop
renderer.setAnimationLoop(() => {
  if (tween) {
    tween.t = Math.min(1, (performance.now() - tween.start) / 600);
    const k = 1 - Math.pow(1 - tween.t, 3);
    // Interpolate on a sphere so the camera swings around the brain instead of through it.
    const a = tween.from.clone().normalize(), b = tween.to.clone().normalize();
    const len = THREE.MathUtils.lerp(tween.from.length(), tween.to.length(), k);
    const q = new THREE.Quaternion().setFromUnitVectors(a, b);
    const dir = a.clone().applyQuaternion(new THREE.Quaternion().slerp(q, k));
    camera.position.copy(dir.multiplyScalar(len));
    controls.target.lerpVectors(tween.fromT, new THREE.Vector3(), k);
    if (tween.t >= 1) tween = null;
  }
  controls.update();
  renderer.render(scene, camera);
});

load().catch((err) => {
  console.error(err);
  loadingEl.textContent = location.protocol === 'file:'
    ? 'This page must be served over http. Run "Start Viewer.bat" (or: python -m http.server) in this folder.'
    : `Failed to load: ${err.message}`;
});
