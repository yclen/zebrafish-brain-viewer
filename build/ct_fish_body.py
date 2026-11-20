"""Turn a whole-larva micro-CT scan into a fish-body mesh aligned to the mapZebrain atlas.

Source data: Ding et al. 2019, eLife 8:e44898, "Computational 3D histological phenotyping of whole
zebrafish by X-ray histotomography", Dryad doi:10.5061/dryad.4nb12g2 (CC0). We use one 5 dpf larva:
  5dpf_3_axial_8bit_chenglab.tif        (reconstruction, 0.743 um isotropic voxels)
  5dpf_3_Registered_Seg.tif             (brain-region labels in the same voxel space)

Steps
  1. Downsample the scan (block mean) to ~6 um voxels; segment the body (threshold + clean-up).
  2. Segment the swim bladder (the largest air pocket enclosed by the body) as a landmark.
  3. Build the brain mask from the label volume.
  4. Register CT brain -> atlas brain with a similarity transform (PCA start + trimmed ICP).
  5. Mesh body and swim bladder with marching cubes, transform into atlas coordinates (um),
     write binary STLs for build_fish_glb.mjs.

Usage: python -I ct_fish_body.py <recon.tif> <seg.tif> <atlas items.json> <atlas stl dir> <out dir> [factor]
"""
import json
import os
import sys
import time

import numpy as np
import tifffile
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from skimage import filters, measure

VOXEL_UM = 0.743
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


def load_downsampled(path, f, reduce="mean"):
    """Block-reduce a (Z, Y, X) TIFF stack by factor f without loading it all at full size."""
    with tifffile.TiffFile(path) as tf:
        n = len(tf.pages)
        h, w = tf.pages[0].shape
        log(f"{os.path.basename(path)}: {n} x {h} x {w}, dtype {tf.pages[0].dtype}")
        nz, ny, nx = n // f, h // f, w // f
        out = np.zeros((nz, ny, nx), np.float32 if reduce == "mean" else np.uint8)
        for k in range(nz):
            block = np.stack([tf.pages[k * f + j].asarray() for j in range(f)])[:, : ny * f, : nx * f]
            b = block.reshape(f, ny, f, nx, f)
            out[k] = b.mean(axis=(0, 2, 4)) if reduce == "mean" else b.max(axis=(0, 2, 4))
            if k % 100 == 0:
                log(f"  slice block {k}/{nz}")
    return out


def largest_component(mask):
    lab, n = ndi.label(mask)
    if n == 0:
        return mask
    sizes = ndi.sum(mask, lab, range(1, n + 1))
    return lab == (1 + int(np.argmax(sizes)))


def read_stl_vertices(path):
    import re
    txt = open(path, encoding="latin1").read()
    v = np.array(re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", txt), dtype=np.float64)
    return v


def umeyama(src, dst):
    """Least-squares similarity transform (s, R, t) mapping src -> dst."""
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    cov = b.T @ a / len(src)
    U, S, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(U) * np.linalg.det(Vt))
    D = np.diag([1, 1, d])
    R = U @ D @ Vt
    s = np.trace(np.diag(S) @ D) / a.var(0).sum()
    t = md - s * R @ ms
    return s, R, t


def icp_similarity(src, dst, s, R, t, iters=60, trim=0.9):
    tree = cKDTree(dst)
    err = None
    for _ in range(iters):
        moved = s * src @ R.T + t
        dist, idx = tree.query(moved)
        keep = dist <= np.quantile(dist, trim)
        s, R, t = umeyama(src[keep], dst[idx[keep]])
        err = float(np.sqrt(np.mean(dist[keep] ** 2)))
    return s, R, t, err


def write_stl(path, verts, faces):
    tri = verts[faces].astype(np.float32)
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    rec = np.zeros(len(faces), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tri
    with open(path, "wb") as fh:
        fh.write(b"fish body (Ding et al. 2019 micro-CT, aligned to mapZebrain)".ljust(80, b" "))
        fh.write(np.uint32(len(faces)).tobytes())
        fh.write(rec.tobytes())


def mesh_from_mask(mask, um_per_vox, sigma=1.2):
    sm = ndi.gaussian_filter(mask.astype(np.float32), sigma)
    verts, faces, _, _ = measure.marching_cubes(sm, 0.5, step_size=1)
    return verts * um_per_vox, faces  # (z, y, x) order, in um


def main(recon, seg, items_path, atlas_stl_dir, out_dir, factor=8):
    os.makedirs(out_dir, exist_ok=True)
    f = int(factor)
    um = VOXEL_UM * f

    # ---- 1. body
    cache = os.path.join(out_dir, "recon_ds.npy")
    vol = np.load(cache) if os.path.exists(cache) else load_downsampled(recon, f)
    np.save(cache, vol)
    # The last slices of this stack are a bright reconstruction artifact; drop them.
    zmax = int(round(vol.shape[0] * 0.95))
    vol[zmax:] = 0
    # Otsu (~56) keeps only dense tissue and loses the skin; 25 (background ~10) was chosen by eye
    # on cross-sections, after a light blur.
    thr = 25.0
    log(f"downsampled shape {vol.shape} ({um:.2f} um voxels); threshold {thr}; "
        f"intensity pct 1/50/99 = {np.percentile(vol, [1, 50, 99]).round(1)}")
    body = ndi.gaussian_filter(vol, 1.0) > thr
    body = ndi.binary_opening(body, iterations=1)
    body = largest_component(body)
    body = ndi.binary_closing(body, iterations=3)
    solid = ndi.binary_fill_holes(body)
    # also fill per axial slice (cavities that touch the scan edge in 3D)
    solid = np.stack([ndi.binary_fill_holes(s) for s in solid])
    log(f"body voxels {solid.sum()}  (~{solid.sum() * um ** 3 / 1e9:.3f} mm^3)")

    # ---- 2. swim bladder: largest dark pocket enclosed by the body
    cavity = solid & ~body
    cavity = ndi.binary_opening(cavity, iterations=1)
    bladder = largest_component(cavity) if cavity.any() else cavity
    log(f"swim-bladder candidate voxels {bladder.sum()} (~{bladder.sum() * um ** 3 / 1e6:.0f} x10^6 um^3)")

    # ---- 3. brain from labels
    labels = load_downsampled(seg, f, reduce="max")
    labels[zmax:] = 0
    brain = labels > 0
    log(f"brain voxels {brain.sum()}; label values {np.unique(labels)[:20]}")

    # ---- 4. register CT brain -> atlas brain
    items = json.load(open(items_path, encoding="utf-8"))
    by_id = {i["id"]: i for i in items}
    # atlas brain = forebrain children (minus retina) + midbrain + hindbrain
    atlas_ids = [c for c in by_id[1102]["children"] if c != 1051] + [1130, 1148]
    atlas_pts = np.concatenate([read_stl_vertices(os.path.join(atlas_stl_dir, f"{i}.stl"))
                                for i in atlas_ids if os.path.exists(os.path.join(atlas_stl_dir, f"{i}.stl"))])
    atlas_pts = np.unique(np.round(atlas_pts, 1), axis=0)
    bv, _ = mesh_from_mask(brain, um, sigma=1.0)
    ct_pts = bv[:, ::-1].copy()  # (x, y, z) in um, CT space
    rng = np.random.default_rng(0)
    ct_s = ct_pts[rng.choice(len(ct_pts), min(20000, len(ct_pts)), replace=False)]
    at_s = atlas_pts[rng.choice(len(atlas_pts), min(40000, len(atlas_pts)), replace=False)]
    log(f"registration points: CT {len(ct_s)}, atlas {len(at_s)}")

    # A brain is nearly symmetric, so shape-only fitting can lock onto flipped solutions. Start from
    # anatomical axes instead: AP = stack axis (snout at slice 0), LR = line between the two eye
    # lenses (the brightest blobs), DV = from the head's centre of mass toward the brain. Atlas axes:
    # x = LR, y = posterior, z = dorsal.
    zi, yi, xi = np.indices(vol.shape)
    head = np.zeros_like(solid)
    head[: int(vol.shape[0] * 0.45)] = True
    # Lenses are the two near-saturated blobs (max >= 240) at the same AP level; other dense tissue
    # in the head is large but less bright.
    bright = (vol > np.percentile(vol[head], 99.0)) & head
    lab, n = ndi.label(bright)
    sizes = ndi.sum(bright, lab, range(1, n + 1))
    peaks = ndi.maximum(vol, lab, range(1, n + 1))
    cand = [k for k in np.argsort(sizes)[::-1] if peaks[k] >= 240 and sizes[k] >= 100]
    lens_ids = [1 + k for k in cand[:2]]
    lenses = np.array([ndi.center_of_mass(bright, lab, k) for k in lens_ids])[:, ::-1] * um  # (x, y, z)
    log(f"lens centres (CT x,y,z um): {lenses.round(0).tolist()}, separation {np.linalg.norm(lenses[0] - lenses[1]):.0f} um")
    ap = np.array([0.0, 0.0, 1.0])
    lr = lenses[1] - lenses[0]
    lr -= ap * (lr @ ap)
    lr /= np.linalg.norm(lr)
    zr = slice(int(np.where(brain.any((1, 2)))[0].min()), int(np.where(brain.any((1, 2)))[0].max()))
    brain_c = np.array(ndi.center_of_mass(brain[zr]))[::-1] * um
    head_c = np.array(ndi.center_of_mass(solid[zr]))[::-1] * um
    dv = brain_c - head_c
    dv -= ap * (dv @ ap) + lr * (dv @ lr)
    dv /= np.linalg.norm(dv)
    lr = np.cross(ap, dv)  # right-handed: x = y cross z
    R0 = np.stack([lr, ap, dv])  # rows map CT vectors onto atlas x, y, z

    # Scale and position from the eyes: CT lens centres <-> centres of spheres fitted to each atlas
    # retina (the CT brain labels extend further caudally than the atlas brain, so they would bias
    # the scale). The CT brain then fixes only the remaining degree of freedom, the pitch about
    # the eye axis.
    def sphere_centre(P):
        A = np.c_[2 * P, np.ones(len(P))]
        c, *_ = np.linalg.lstsq(A, (P ** 2).sum(1), rcond=None)
        return c[:3]
    retina = read_stl_vertices(os.path.join(atlas_stl_dir, "1051.stl"))
    mid_x = retina[:, 0].mean()
    eyes_atlas = np.array([sphere_centre(retina[retina[:, 0] < mid_x]), sphere_centre(retina[retina[:, 0] >= mid_x])])
    lens_ct = lenses[np.argsort((lenses @ R0.T)[:, 0])]  # order by atlas x
    s = float(np.linalg.norm(eyes_atlas[1] - eyes_atlas[0]) / np.linalg.norm(lens_ct[1] - lens_ct[0]))
    piv_ct, piv_at = lens_ct.mean(0), eyes_atlas.mean(0)
    tree = cKDTree(at_s)

    def place(theta):
        c, si = np.cos(theta), np.sin(theta)
        Rx = np.array([[1, 0, 0], [0, c, -si], [0, si, c]])
        R = Rx @ R0
        return R, piv_at - s * R @ piv_ct

    def score(theta):
        R, t = place(theta)
        d, _ = tree.query(s * ct_s @ R.T + t)
        return float(np.sqrt(np.mean(np.sort(d)[: int(0.8 * len(d))] ** 2)))

    thetas = np.radians(np.arange(-45, 45.5, 0.5))
    errs = [score(th) for th in thetas]
    th = thetas[int(np.argmin(errs))]
    R, t = place(th)
    err = min(errs)
    log(f"eyes: atlas {eyes_atlas.round(0).tolist()}, scale {s:.3f}; best pitch {np.degrees(th):+.1f} deg, "
        f"brain surface rms (best 80%) {err:.1f} um")

    # ---- 5. meshes in atlas space
    meta = {"scale": s, "R": R.tolist(), "t": t.tolist(), "rms_um": err, "ct_voxel_um": um}
    for name, mask, sigma in (("fish_body", solid, 1.5), ("swim_bladder", bladder, 1.0)):
        if not mask.any():
            continue
        v, fc = mesh_from_mask(mask, um, sigma)
        v = s * v[:, ::-1] @ R.T + t
        write_stl(os.path.join(out_dir, f"{name}.stl"), v, fc)
        meta[name] = {"triangles": int(len(fc)), "min": v.min(0).round(1).tolist(), "max": v.max(0).round(1).tolist()}
        log(f"{name}: {len(fc)} triangles, atlas-space bbox {v.min(0).round(0)} .. {v.max(0).round(0)}")
    json.dump(meta, open(os.path.join(out_dir, "alignment.json"), "w"), indent=1)
    np.save(os.path.join(out_dir, "masks_ds.npy"), np.stack([solid, bladder, brain]).astype(np.uint8))


if __name__ == "__main__":
    main(*sys.argv[1:6], *(sys.argv[6:7] or [8]))
