// Usage: node build_glb.mjs <items.json> <stl dir> <out.glb> [ratio] [error]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(process.env.TOOLS_DIR + '/package.json');
const { Document, NodeIO } = require('@gltf-transform/core');
const { weld, simplify, normals, quantize, meshopt, dedup, prune } = require('@gltf-transform/functions');
const { EXTMeshoptCompression, KHRMeshQuantization } = require('@gltf-transform/extensions');
const { MeshoptSimplifier, MeshoptEncoder } = require('meshoptimizer');

const [itemsPath, stlDir, outPath, ratioArg, errArg] = process.argv.slice(2);
const RATIO = Number(ratioArg ?? 0.35), ERR = Number(errArg ?? 0.0008);
const items = JSON.parse(fs.readFileSync(itemsPath, 'utf8'));

function parseStl(buf) {
  const isAscii = buf.slice(0, 80).toString('latin1').trimStart().startsWith('solid') && buf.indexOf('facet') > 0 && buf.indexOf('facet') < 300;
  if (isAscii) {
    const txt = buf.toString('latin1');
    const re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g; const out = []; let m;
    while ((m = re.exec(txt))) out.push(+m[1], +m[2], +m[3]);
    return new Float32Array(out);
  }
  const n = buf.readUInt32LE(80); const out = new Float32Array(n * 9);
  for (let i = 0; i < n; i++) for (let v = 0; v < 9; v++) out[i * 9 + v] = buf.readFloatLE(84 + i * 50 + 12 + v * 4);
  return out;
}

await MeshoptSimplifier.ready; await MeshoptEncoder.ready;
const doc = new Document(); const buffer = doc.createBuffer();
const scene = doc.createScene('brain');
let triIn = 0;
for (const it of items) {
  const f = path.join(stlDir, `${it.id}.stl`);
  if (!fs.existsSync(f)) continue;
  const pos = parseStl(fs.readFileSync(f)); triIn += pos.length / 9;
  const acc = doc.createAccessor().setType('VEC3').setArray(pos).setBuffer(buffer);
  const prim = doc.createPrimitive().setAttribute('POSITION', acc);
  const mesh = doc.createMesh(String(it.id)).addPrimitive(prim);
  scene.addChild(doc.createNode(String(it.id)).setMesh(mesh));
}
const stat = (tag) => { let n = 0, v = 0, noIdx = 0; for (const m of doc.getRoot().listMeshes()) for (const p of m.listPrimitives()) { const ix = p.getIndices(); if (ix) n += ix.getCount() / 3; else noIdx++; v += p.getAttribute('POSITION').getCount(); } console.log(tag, 'tris', n, 'verts', v, 'noIndex', noIdx); };
await doc.transform(weld()); stat('weld');
await doc.transform(simplify({ simplifier: MeshoptSimplifier, ratio: RATIO, error: ERR })); stat('simplify');
// Some source meshes contain mirrored pieces wound inside-out (e.g. the left eye of the retina).
// For each connected piece, flip its triangles if its signed volume is negative.
let flipped = 0;
for (const m of doc.getRoot().listMeshes()) for (const p of m.listPrimitives()) {
  const P = p.getAttribute('POSITION').getArray(), I = p.getIndices().getArray();
  const nV = P.length / 3, parent = new Int32Array(nV).map((_, i) => i);
  const find = (x) => { while (parent[x] !== x) x = parent[x] = parent[parent[x]]; return x; };
  for (let t = 0; t < I.length; t += 3) { const a = find(I[t]), b = find(I[t + 1]), c = find(I[t + 2]); parent[b] = a; parent[find(c)] = a; }
  const vol = new Map();
  for (let t = 0; t < I.length; t += 3) {
    const a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
    const v = P[a] * (P[b + 1] * P[c + 2] - P[b + 2] * P[c + 1]) - P[a + 1] * (P[b] * P[c + 2] - P[b + 2] * P[c]) + P[a + 2] * (P[b] * P[c + 1] - P[b + 1] * P[c]);
    const r = find(I[t]); vol.set(r, (vol.get(r) || 0) + v);
  }
  let changed = false;
  for (let t = 0; t < I.length; t += 3) {
    if (vol.get(find(I[t])) < 0) { const tmp = I[t + 1]; I[t + 1] = I[t + 2]; I[t + 2] = tmp; changed = true; }
  }
  if (changed) { flipped++; p.getIndices().setArray(I); }
}
console.log(`re-oriented inside-out pieces in ${flipped} meshes (mapZebrain STLs are mostly wound clockwise)`);

// smooth, area-weighted vertex normals (gltf-transform's normals() unwelds, which we don't want)
for (const m of doc.getRoot().listMeshes()) for (const p of m.listPrimitives()) {
  const P = p.getAttribute('POSITION').getArray(), I = p.getIndices().getArray(), N = new Float32Array(P.length);
  for (let t = 0; t < I.length; t += 3) {
    const a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
    const ux = P[b] - P[a], uy = P[b + 1] - P[a + 1], uz = P[b + 2] - P[a + 2];
    const vx = P[c] - P[a], vy = P[c + 1] - P[a + 1], vz = P[c + 2] - P[a + 2];
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    for (const k of [a, b, c]) { N[k] += nx; N[k + 1] += ny; N[k + 2] += nz; }
  }
  for (let k = 0; k < N.length; k += 3) { const l = Math.hypot(N[k], N[k + 1], N[k + 2]) || 1; N[k] /= l; N[k + 1] /= l; N[k + 2] /= l; }
  p.setAttribute('NORMAL', doc.createAccessor().setType('VEC3').setArray(N).setBuffer(buffer));
}
stat('normals');
await doc.transform(
  dedup(), prune(),
  quantize({ quantizePosition: 14, quantizeNormal: 10 }),
  meshopt({ encoder: MeshoptEncoder, level: 'medium' }),
);
let triOut = 0;
for (const m of doc.getRoot().listMeshes()) for (const p of m.listPrimitives()) triOut += p.getIndices().getCount() / 3;
await new NodeIO().registerExtensions([EXTMeshoptCompression, KHRMeshQuantization])
  .registerDependencies({ 'meshopt.encoder': MeshoptEncoder }).write(outPath, doc);
console.log(`tris in ${triIn} -> out ${triOut}; file ${(fs.statSync(outPath).size / 1e6).toFixed(2)} MB`);
