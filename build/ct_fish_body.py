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


def ellipsoid_mesh(c, axes, nu=32, nv=16):
    """Closed ellipsoid around centre c; rows of `axes` are the three semi-axis vectors."""
    axes = np.asarray(axes, float)
    u = np.linspace(0, 2 * np.pi, nu, endpoint=False)
    w = np.linspace(0, np.pi, nv + 1)
    V = [c + axes[0] * np.cos(a) * np.sin(b) + axes[1] * np.sin(a) * np.sin(b) + axes[2] * np.cos(b)
         for b in w for a in u]
    F = []
    for i in range(nv):
        for j in range(nu):
            p0, p1 = i * nu + j, i * nu + (j + 1) % nu
            p2, p3 = (i + 1) * nu + j, (i + 1) * nu + (j + 1) % nu
            F += [(p0, p2, p1), (p1, p2, p3)]
    return np.array(V), np.array(F)


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
    solid_raw = solid.copy()  # for the swim-bladder search below
    # The roof of the fourth ventricle over the hindbrain and the epidermis are only a few um thick
    # and fall below the threshold, so the outline dipped into the head there. Seal dips narrower
    # than ~50 um with a ball closing, then add one voxel (~6 um) for the skin.
    rr = 4
    zz, yy, xx = np.indices((2 * rr + 1,) * 3) - rr
    ball = zz ** 2 + yy ** 2 + xx ** 2 <= rr ** 2
    padded = np.pad(solid, rr + 1)
    solid = ndi.binary_closing(padded, structure=ball)[rr + 1: -rr - 1, rr + 1: -rr - 1, rr + 1: -rr - 1]
    solid = ndi.binary_dilation(solid, iterations=1)
    log(f"body voxels {solid.sum()}  (~{solid.sum() * um ** 3 / 1e9:.3f} mm^3)")

    # ---- 2. swim bladder: largest dark pocket enclosed by the body
    cavity = solid_raw & ~body
    cavity = ndi.binary_opening(cavity, iterations=1)
    bladder = largest_component(cavity) if cavity.any() else cavity
    log(f"swim-bladder candidate voxels {bladder.sum()} (~{bladder.sum() * um ** 3 / 1e6:.0f} x10^6 um^3)")

    # ---- 3. brain from labels
    labels = load_downsampled(seg, f, reduce="max")
    labels[zmax:] = 0
    # Labels 101 and 103 lie 880-1016 um from the snout, beyond the caudal end of the atlas
    # hindbrain (most likely rostral spinal cord), so they are left out of the brain used for fitting.
    brain = (labels > 0) & (labels != 101) & (labels != 103)
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

    # Refine with an affine transform (per-axis stretch + shear). The 5 dpf CT brain is
    # proportioned differently from the 6 dpf atlas brain (longer, narrower), so no single scale
    # makes the two overlap; a uniform scale left the atlas brain poking out of the CT head.
    # Fitting the CT brain onto the atlas brain and warping the body with the same transform
    # keeps the fish's own brain-to-body proportions. Symmetric trimmed ICP on brain surfaces,
    # with the eyes (CT lens centre <-> atlas retina sphere centre) as weighted landmarks.
    A, b = s * R, t.copy()
    tree_at = cKDTree(at_s)
    w_eye = 0.05 * len(ct_s)

    def solve_affine(P, Q, w):
        X = np.c_[P, np.ones(len(P))] * np.sqrt(w)[:, None]
        M, *_ = np.linalg.lstsq(X, Q * np.sqrt(w)[:, None], rcond=None)
        return M[:3].T, M[3]

    for it in range(80):
        moved = ct_s @ A.T + b
        d1, i1 = tree_at.query(moved)
        k1 = d1 <= np.quantile(d1, 0.9)
        d2, i2 = cKDTree(moved).query(at_s)
        k2 = d2 <= np.quantile(d2, 0.9)
        P = np.concatenate([ct_s[k1], ct_s[i2[k2]], lens_ct])
        Q = np.concatenate([at_s[i1[k1]], at_s[k2], eyes_atlas])
        w = np.concatenate([np.ones(k1.sum()), np.ones(k2.sum()), np.full(2, w_eye)])
        A, b = solve_affine(P, Q, w)
    moved = ct_s @ A.T + b
    d1, _ = tree_at.query(moved)
    d2, _ = cKDTree(moved).query(at_s)
    sv = np.linalg.svd(A, compute_uv=False)
    eye_err = np.linalg.norm(lens_ct @ A.T + b - eyes_atlas, axis=1)
    log(f"affine: stretch factors {sv.round(3).tolist()}, brain rms ct->atlas {np.sqrt(np.mean(d1**2)):.1f} um, "
        f"atlas->ct {np.sqrt(np.mean(d2**2)):.1f} um, eye landmark error {eye_err.round(0).tolist()} um")
    s, R, t = None, A, b  # downstream code applies v @ R.T + t; with s folded into A

    # ---- 4b. make sure the atlas brain and eyes sit inside the skin
    # After the fit, 95% of the atlas brain surface lies within ~8 um of the inside of the CT skin,
    # but the 6 dpf atlas brain and eyes bulge up to ~35 um past the 5 dpf scanned head in places
    # (mostly the hindbrain roof and the eyes). There, push the skin out to cover them: voxelize the
    # atlas brain+eye outline in CT space and add it (plus one voxel of skin) to the body.
    outline = read_stl_vertices(os.path.join(atlas_stl_dir, "0.stl"))
    # densify: add triangle centroids and edge midpoints so the rasterized shell has no gaps
    tri = outline.reshape(-1, 3, 3)
    dense = np.concatenate([outline, tri.mean(1), (tri[:, 0] + tri[:, 1]) / 2, (tri[:, 1] + tri[:, 2]) / 2, (tri[:, 0] + tri[:, 2]) / 2])
    q = np.linalg.solve(R, (dense - t).T).T / um  # CT (x, y, z) voxel coords
    idx = np.round(q[:, ::-1]).astype(int)
    keep = np.all((idx >= 0) & (idx < np.array(solid.shape)), axis=1)
    shell = np.zeros_like(solid)
    shell[tuple(idx[keep].T)] = True
    shell = ndi.binary_closing(ndi.binary_dilation(shell, iterations=1), iterations=1)
    atlas_solid = ndi.binary_fill_holes(shell)
    added = atlas_solid & ~solid
    log(f"atlas brain+eyes outline: {atlas_solid.sum()} voxels; {added.sum()} voxels "
        f"({added.sum() / max(atlas_solid.sum(), 1) * 100:.1f}%) were outside the CT skin and are covered by pushing the skin out")
    solid = solid | ndi.binary_dilation(atlas_solid, iterations=1)

    # ---- 4c. eyes and lenses from the CT (the atlas crops the outer side of both eyes)
    sm = ndi.gaussian_filter(vol, 1.0)
    notbrain = ~ndi.binary_dilation(brain, iterations=2)
    eyes = np.zeros_like(solid)
    lens_mask = np.zeros_like(solid)
    zi, yi, xi = np.indices(vol.shape)
    for L in lenses / um:  # lens centre in voxel (x, y, z)
        d = np.sqrt((xi - L[0]) ** 2 + (yi - L[1]) ** 2 + (zi - L[2]) ** 2) * um
        m = ndi.binary_opening((sm > 40) & (d < 170) & notbrain, iterations=2)
        lab, n = ndi.label(m)
        shell = (d > 45) & (d < 110)  # the retina cup around the lens
        counts = np.bincount(lab[shell & m].ravel(), minlength=n + 1)
        counts[0] = 0
        e = ndi.binary_dilation(lab == counts.argmax(), iterations=1) & (sm > 30)
        eyes |= ndi.binary_fill_holes(ndi.binary_closing(e, iterations=2))
        lens_mask |= ndi.binary_fill_holes((vol > 150) & (d < 60))
    log(f"eyes: {eyes.sum()} voxels (equivalent radius each "
        f"{(eyes.sum() / 2 * um ** 3 * 3 / 4 / np.pi) ** (1 / 3):.0f} um); lenses {lens_mask.sum()} voxels")

    # ---- 4d. tail: the scan stops ~1.44 mm behind the snout. Continue the body from its last
    # complete cross-section, morphing into an ellipse and tapering to a 5 dpf standard length of
    # ~3.9 mm (snout to tip of the caudal fin fold), with a median fin fold. This part is a
    # stylized reconstruction, not scan data. Built in CT space so the same warp applies.
    areas = solid.sum((1, 2))
    zb = np.where(areas > 0)[0]
    z_snout, z_last = zb.min(), zb.max()
    ref = np.median(areas[z_last - 25: z_last - 5])
    z_c = max(z for z in range(z_last - 25, z_last + 1) if areas[z] > 0.8 * ref)
    cont = max(measure.find_contours(solid[z_c].astype(float), 0.5), key=len)  # (row=y, col=x)
    cont = np.c_[cont[:, 1], cont[:, 0]] * um  # (x, y) in um
    c0 = cont.mean(0)
    lr2, dv2 = lr[:2] / np.linalg.norm(lr[:2]), dv[:2] / np.linalg.norm(dv[:2])
    rel = cont - c0
    a_lr, a_dv = rel @ lr2, rel @ dv2
    w0, h0 = (a_lr.max() - a_lr.min()) / 2, (a_dv.max() - a_dv.min()) / 2
    off_dv = (a_dv.max() + a_dv.min()) / 2
    nth = 96
    th = np.linspace(0, 2 * np.pi, nth, endpoint=False)
    ang = np.arctan2(a_dv - off_dv, a_lr / w0 * h0)
    ring0 = np.array([rel[np.argmin(np.abs(np.angle(np.exp(1j * (ang - t_)))))] for t_ in th])
    L_total, L_fin = 3900.0, 150.0  # um, 5 dpf: snout to fin tip; caudal fin fold beyond the body tip
    z0_um, z_tip = z_c * um, z_snout * um + L_total - L_fin
    z_fin_end = z_snout * um + L_total
    sstep = lambda a, b, x: np.clip((x - a) / (b - a), 0, 1) ** 2 * (3 - 2 * np.clip((x - a) / (b - a), 0, 1))
    # Larval proportions (5 dpf): behind the anus (~1.95 mm) the tail is laterally compressed,
    # ~120-130 um wide and ~230 um deep (muscle), tapering to the notochord tip. Between the
    # scan's last slice and the anus the trunk cross-section morphs into that shape. The dorsal
    # edge stays continuous with the trunk and drops to the axis height at the tip.
    z_anus = z_snout * um + 1950
    bl0, bd0 = (ring0 @ lr2) / w0, (ring0 @ dv2 - off_dv) / h0
    top0 = off_dv + h0
    # Steer the tail tip onto the atlas midline (the scanned trunk angles ~50 um off it).
    straight_tip = np.r_[c0 + dv2 * (top0 - 115), z_tip]
    tip_atlas = R @ straight_tip + t
    tip_atlas[0] = mid_x
    lateral = np.linalg.solve(R, tip_atlas - t)[:2] - straight_tip[:2]
    zs = np.linspace(z0_um, z_tip, 160)
    rings, centres, halfh = [], [], []
    for z in zs:
        v_ = sstep(z0_um, z_anus, z)
        blend = sstep(z0_um, z0_um + 250, z)
        q = np.clip((z - z_anus) / (z_tip - z_anus), 0, 1)
        wt = 52 * (1 - q) ** 0.8 + 10 * (1 - q)
        ht = 115 * (1 - q) ** 0.7
        w_abs, h_abs = (1 - v_) * w0 + v_ * wt, (1 - v_) * h0 + v_ * ht
        top = top0 - 115 * sstep(z_anus, z_tip, z)  # dorsal edge
        cdv = top - h_abs
        bl = (1 - blend) * bl0 + blend * np.cos(th)
        bd = (1 - blend) * bd0 + blend * np.sin(th)
        shift = lateral * ((z - z0_um) / (z_tip - z0_um)) ** 2
        cxy = c0 + shift + dv2 * cdv
        rings.append(np.c_[cxy + np.outer(bl * w_abs, lr2) + np.outer(bd * h_abs, dv2), np.full(nth, z)])
        centres.append(np.r_[cxy, z])
        halfh.append(h_abs)
    V = np.concatenate(rings + [np.array([[*centres[-1][:2], z_tip + 5]]), np.array([[*centres[0]]])])
    F = []
    for i in range(len(rings) - 1):
        for j in range(nth):
            a, b2, c, d2 = i * nth + j, i * nth + (j + 1) % nth, (i + 1) * nth + j, (i + 1) * nth + (j + 1) % nth
            F += [(a, c, b2), (b2, c, d2)]
    tip, start = len(V) - 2, len(V) - 1
    last = (len(rings) - 1) * nth
    F += [(last + j, tip, last + (j + 1) % nth) for j in range(nth)]
    F += [(j, (j + 1) % nth, start) for j in range(nth)]
    tail_v, tail_f = V, np.array(F)

    # median fin fold: a thin (6 um) membrane in the midline plane, dorsal from the cut, ventral
    # from ~1.9 mm (behind the anus), meeting in a rounded caudal fin beyond the body tip
    fz = np.linspace(z0_um, z_fin_end, 160)
    d_in, d_out, v_in, v_out, cen = [], [], [], [], []
    for z in fz:
        if z <= z_tip:
            k = np.searchsorted(zs, z).clip(0, len(zs) - 1)
            c, hh = centres[k], halfh[k]
            fd = 95 * sstep(z0_um, z0_um + 200, z)
            fv = 85 * sstep(z_anus - 50, z_anus + 150, z)
            d_in.append(hh * 0.85); d_out.append(hh * 0.85 + fd)
            v_in.append(-hh * 0.85); v_out.append(-hh * 0.85 - fv)
        else:
            c = centres[-1]
            r = np.sqrt(max(0.0, 1 - ((z - z_tip) / (z_fin_end - z_tip)) ** 2))
            d_in.append(0.0); d_out.append(95 * r)
            v_in.append(0.0); v_out.append(-85 * r)
        cen.append(np.r_[c[:2], z])
    cen = np.array(cen)

    def slab(lo, hi, ns=6, half=3.0):
        """Thin closed membrane between DV offsets lo(z)..hi(z) around the tail axis."""
        nz_ = len(fz)
        V = []
        for side in (-half, half):
            for i in range(nz_):
                for j in range(ns + 1):
                    p = cen[i].copy()
                    p[:2] += dv2 * (lo[i] + (hi[i] - lo[i]) * j / ns) + lr2 * side
                    V.append(p)
        idx = lambda sd, i, j: sd * nz_ * (ns + 1) + i * (ns + 1) + j
        Fs = []
        for i in range(nz_ - 1):
            for j in range(ns):
                a, b2, c, d2 = idx(0, i, j), idx(0, i, j + 1), idx(0, i + 1, j), idx(0, i + 1, j + 1)
                Fs += [(a, b2, c), (b2, d2, c)]
                a, b2, c, d2 = idx(1, i, j), idx(1, i, j + 1), idx(1, i + 1, j), idx(1, i + 1, j + 1)
                Fs += [(a, c, b2), (b2, c, d2)]
        rim = [(i, 0) for i in range(nz_)] + [(nz_ - 1, j) for j in range(ns + 1)] + \
              [(i, ns) for i in range(nz_ - 1, -1, -1)] + [(0, j) for j in range(ns, -1, -1)]
        for (i1, j1), (i2, j2) in zip(rim, rim[1:]):
            a, b2, c, d2 = idx(0, i1, j1), idx(0, i2, j2), idx(1, i1, j1), idx(1, i2, j2)
            Fs += [(a, c, b2), (b2, c, d2)]
        return np.array(V), np.array(Fs)

    dv_, df_ = slab(np.array(d_in), np.array(d_out))
    vv_, vf_ = slab(np.array(v_out), np.array(v_in))
    fin_v, fin_f = np.concatenate([dv_, vv_]), np.concatenate([df_, vf_ + len(dv_)])
    log(f"tail: from {z0_um:.0f} um to body tip {z_tip:.0f} um, fin to {z_fin_end:.0f} um (CT space, snout at {z_snout * um:.0f})")

    # ---- 5. assemble a clean fish. The goal is a realistic-looking larva that keeps the scanned
    # proportions, not every CT bump: one smooth skin (scan + tail merged), rounded eyes with a
    # lens in each, a smooth swim bladder, and clean pectoral fins in place of the scan's
    # fragmentary ones.
    meta = {"affine_A": R.tolist(), "affine_b": t.tolist(), "stretch": sv.tolist(), "ct_voxel_um": um,
            "note": "atlas_um = A @ ct_um(x,y,z) + b"}

    # 5a. one skin. Remove thin structures (the pectoral fins and fin-fold fragments the scan only
    # half captured) with a ~18 um ball opening, keep the atlas brain/eyes covered, stop the scan
    # at the tail's first ring, rasterize the tail rings into the same grid, and mesh it all at once.
    r3 = 3
    zz, yy, xx = np.indices((2 * r3 + 1,) * 3) - r3
    ball3 = zz ** 2 + yy ** 2 + xx ** 2 <= r3 * r3
    core = ndi.binary_opening(np.pad(solid, r3 + 1), structure=ball3)[r3 + 1:-r3 - 1, r3 + 1:-r3 - 1, r3 + 1:-r3 - 1]
    core = largest_component(core) | ndi.binary_dilation(atlas_solid, iterations=1)
    core[z_c + 1:] = False
    pad = 25
    nz_ext = int(np.ceil((z_tip + 30) / um)) + 2
    G = np.zeros((nz_ext, solid.shape[1] + 2 * pad, solid.shape[2] + 2 * pad), bool)
    G[: solid.shape[0], pad: pad + solid.shape[1], pad: pad + solid.shape[2]] = core
    from skimage.draw import polygon as fill_polygon
    zs_arr = np.array(zs)
    for k in range(z_c, nz_ext):
        z = k * um
        if z < zs_arr[0] or z > zs_arr[-1]:
            continue
        i = int(np.clip(np.searchsorted(zs_arr, z) - 1, 0, len(zs_arr) - 2))
        fr = (z - zs_arr[i]) / (zs_arr[i + 1] - zs_arr[i])
        ring = rings[i] * (1 - fr) + rings[i + 1] * fr
        rr_, cc_ = fill_polygon(ring[:, 1] / um + pad, ring[:, 0] / um + pad, G.shape[1:])
        G[k, rr_, cc_] = True
    bv, bf = mesh_from_mask(G, um, sigma=2.5)
    body_v = bv[:, ::-1] - np.array([pad * um, pad * um, 0])

    # 5b. eyes: the CT eyes, cleaned and smoothed, with a socket where the lens sits; lenses as
    # spheres at the scanned lens centres (the scanned lens is a bright ring with a dark core, which
    # meshed as broken shells).
    lab_e, n_e = ndi.label(ndi.binary_opening(eyes, iterations=1))
    sizes_e = ndi.sum(lab_e > 0, lab_e, range(1, n_e + 1))
    eyes_c = np.isin(lab_e, 1 + np.argsort(sizes_e)[-2:])
    eyes_c = np.stack([ndi.binary_fill_holes(s) for s in ndi.binary_fill_holes(eyes_c)])
    r_lens = (lens_mask.sum() / 2 * um ** 3 * 3 / 4 / np.pi) ** (1 / 3)
    # Each eye is built in atlas space as a sphere fitted to the (cropped) atlas retina, just large
    # enough to enclose it, so it completes the retina the atlas cuts off. A larval eye is close to
    # spherical. The lens faces sideways and slightly forward and
    # bulges out through the pupil, ringed by a thin metallic iris. Meshes are built in atlas space
    # and mapped back into CT space, because every part goes through the same transform on output.
    lab2, _ = ndi.label(eyes_c)
    r_lens_a = r_lens * abs(np.linalg.det(R)) ** (1 / 3)
    ev_l, ef_l, lv, lf, iv, if_ = [], [], [], [], [], []
    to_ct = lambda P: np.linalg.solve(R, (np.atleast_2d(P) - t).T).T
    for k in (1, 2):
        ce_ct = np.argwhere(lab2 == k)[:, ::-1].mean(0) * um
        L_ct = lenses[np.argmin(np.linalg.norm(lenses - ce_ct, axis=1))]
        c_at = R @ ce_ct + t
        side = 0 if c_at[0] < mid_x else 1
        ca = eyes_atlas[side]
        ret_side = retina[(retina[:, 0] < mid_x) if side == 0 else (retina[:, 0] >= mid_x)]
        # sphere-fit radius of the retina cup (percentiles are skewed by the optic nerve, which is
        # part of the atlas retina region) plus a margin for the outer eye layers
        Af = np.c_[2 * ret_side, np.ones(len(ret_side))]
        cf, *_ = np.linalg.lstsq(Af, (ret_side ** 2).sum(1), rcond=None)
        r_eye = float(np.sqrt(cf[3] + (cf[:3] ** 2).sum())) + 28
        # larval eyes look sideways and a little forward
        out = np.array([-1.0 if side == 0 else 1.0, -0.3, 0.0])
        out /= np.linalg.norm(out)
        v_, f_ = ellipsoid_mesh(ca, np.eye(3) * r_eye, 40, 20)
        ef_l.append(f_ + sum(len(x) for x in ev_l)); ev_l.append(to_ct(v_))
        lc = ca + out * (r_eye - 0.35 * r_lens_a)
        v_, f_ = ellipsoid_mesh(lc, np.eye(3) * r_lens_a, 28, 14)
        lf.append(f_ + sum(len(x) for x in lv)); lv.append(to_ct(v_))
        a1 = np.cross(out, [0, 0, 1.0]); a1 /= np.linalg.norm(a1); a2 = np.cross(out, a1)
        R_ring, r_tube = r_lens_a * 1.2, r_lens_a * 0.2
        uu, vv = np.meshgrid(np.linspace(0, 2 * np.pi, 48, endpoint=False), np.linspace(0, 2 * np.pi, 10, endpoint=False))
        rc = ca + out * (r_eye - 0.08 * r_lens_a)
        ring_pts = (rc + (R_ring + r_tube * np.cos(vv))[..., None] * (np.cos(uu)[..., None] * a1 + np.sin(uu)[..., None] * a2)
                    + (r_tube * 0.5 * np.sin(vv))[..., None] * out).reshape(-1, 3)
        F_ = []
        for i in range(10):
            for j in range(48):
                p0, p1 = i * 48 + j, i * 48 + (j + 1) % 48
                p2, p3 = ((i + 1) % 10) * 48 + j, ((i + 1) % 10) * 48 + (j + 1) % 48
                F_ += [(p0, p2, p1), (p1, p2, p3)]
        if_.append(np.array(F_) + sum(len(x) for x in iv)); iv.append(to_ct(ring_pts))
        log(f"eye {k}: atlas centre {ca.round(0)}, radius {r_eye:.0f} um, lens direction {out.round(2)}")
    eyes_v, ef = np.concatenate(ev_l), np.concatenate(ef_l)
    lens_v, lens_f = np.concatenate(lv), np.concatenate(lf)
    iris_v, iris_f = np.concatenate(iv), np.concatenate(if_)
    log(f"lens radius {r_lens:.0f} um (CT space)")

    # 5c. swim bladder: ellipsoid with the scanned bladder's position, size and orientation
    pb = np.argwhere(bladder)[:, ::-1] * um
    cb = pb.mean(0)
    w_, V_ = np.linalg.eigh(np.cov((pb - cb).T))
    sb_v, sb_f = ellipsoid_mesh(cb, (V_ * np.sqrt(5 * w_)).T, 36, 18)
    log(f"swim bladder ellipsoid semi-axes {np.sqrt(5 * w_).round(0)} um")

    # 5d. pectoral fins: the scan shows thin fin fragments on both sides from ~520 um behind its
    # first slice. Model each as a thin paddle (~300 um long) attached at the side of the body,
    # angled ~30 deg outward and backward, as larvae hold them between beats.
    z_pf = 540.0
    kp = int(round(z_pf / um))
    sl = np.argwhere(core[kp])[:, ::-1] * um  # (x, y) of the body cross-section
    pf_v, pf_f = [], []
    for sgn in (1, -1):
        proj = sl @ lr2 * sgn
        mid = np.abs((sl - sl.mean(0)) @ dv2) < 40
        base = sl[mid][np.argmax(proj[mid])] - lr2 * sgn * 10
        dfin = np.r_[lr2 * sgn * np.sin(np.radians(30)), np.cos(np.radians(30))]
        dv3 = np.r_[dv2, 0.0]
        nrm = np.cross(dfin, dv3)
        cfin = np.r_[base, z_pf] + dfin * 150 * 0.85
        v_, f_ = ellipsoid_mesh(cfin, np.stack([dfin * 150, dv3 * 70, nrm * 3]), 32, 12)
        pf_f.append(f_ + sum(len(x) for x in pf_v))
        pf_v.append(v_)
    pf_v, pf_f = np.concatenate(pf_v), np.concatenate(pf_f)

    parts = [("fish_body", body_v, bf), ("fin_fold", fin_v, fin_f), ("pectoral_fins", pf_v, pf_f),
             ("eyes", eyes_v, ef), ("lenses", lens_v, lens_f), ("iris", iris_v, iris_f),
             ("swim_bladder", sb_v, sb_f)]
    for name, v, fc in parts:
        v = v @ R.T + t
        write_stl(os.path.join(out_dir, f"{name}.stl"), v, fc)
        meta[name] = {"triangles": int(len(fc)), "min": v.min(0).round(1).tolist(), "max": v.max(0).round(1).tolist()}
        log(f"{name}: {len(fc)} triangles, atlas-space bbox {v.min(0).round(0)} .. {v.max(0).round(0)}")
    json.dump(meta, open(os.path.join(out_dir, "alignment.json"), "w"), indent=1)
    np.save(os.path.join(out_dir, "masks_ds.npy"), np.stack([solid, bladder, brain]).astype(np.uint8))


if __name__ == "__main__":
    main(*sys.argv[1:6], *(sys.argv[6:7] or [8]))
