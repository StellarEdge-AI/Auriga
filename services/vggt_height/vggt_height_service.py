"""VGGT-Omega object-height service (binds 127.0.0.1 by default; packaging and install: services/vggt_height/README.md).

Input per request: one RGB image (PNG/JPEG bytes, base64), the camera's calibrated intrinsics K (3x3),
camera-to-world T_world_camera (4x4, OpenCV optical axes right/down/forward) and the support (table)
height in the same world frame, plus the pixels of interest. Nothing else (no object names, no truth).

Method (see docs/vggt_height.md):
  1. one VGGT-Omega forward pass on the native image -> relative depth + confidence;
  2. back-project the depth with the CALIBRATED K and the known camera rotation ("unit-scale" points),
     RANSAC a near-horizontal plane (the table), scale s so the plane sits at support_z (per image);
  3. per pixel: object mask = SAM 2.1 point prompt at the pixel (default; fallback window), VGGT points in the
     mask (eroded 2 px, conf > p10, above support + 4 mm, DBSCAN 1 cm largest cluster) -> top = p98 z,
     center = (top + support) / 2.  method="window": 21x21 px window around the pixel, p98 z as top.
v2 (2026-10-20, object_table line): POST /object_table (automatic SAM 2.1 masks -> per-object VGGT geometry, see
object_table()) and plausibility flags on /height pixels (height above max, low depth confidence, contact
mismatch); /height values are unchanged.
v3 (2026-10-20, scene3d line): POST /points (voxel-downsampled VGGT point cloud of the image in the world frame, per point
confidence rank, source pixel and a near-robot flag, as a compressed npz) for Auriga features.scene3d; /height and
/object_table code and numerics are unchanged.
One shared process; requests are served FIFO by a single GPU worker thread; results per image are cached.
"""
import argparse
import base64
import collections
import hashlib
import io
import json
import math
import os
import queue
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import torch
from PIL import Image
from sklearn.cluster import DBSCAN

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("VGGT_HOST", "127.0.0.1"))
ap.add_argument("--port", type=int, default=int(os.environ.get("VGGT_PORT", "8331")))
ap.add_argument("--ckpt", default=os.environ.get("VGGT_CKPT", "weights/vggt_omega_1b_512.pt"))
ap.add_argument("--sam", default=os.environ.get("SAM2_MODEL", "facebook/sam2.1-hiera-large"))
ap.add_argument("--cache", type=int, default=48, help="images kept (reconstruction + SAM embedding)")
ap.add_argument("--max_queue", type=int, default=64)
ap.add_argument("--status", default="")
ap.add_argument("--sam31", default=os.environ.get("SAM31_ENDPOINT", "http://127.0.0.1:8333"), help="SAM 3.1 text-prompt service (sam31/sam31_service.py)")
args = ap.parse_args()
DEV = "cuda"
DEBUG_DIR = os.environ.get("OT_DEBUG_DIR", "")   # dev only: dump the object masks of each /object_table call
VERSION = "vggt-height-v2"
WIN = 10                    # window half width (px), fixed a priori in section 9
ABOVE = 0.004               # points above the support plane (m)
MAX_MASK_FRAC = 0.15        # SAM masks larger than this fraction of the image are support/background


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


# ------------------------------------------------------------------ models
from vggt_omega.models import VGGTOmega  # noqa: E402
from vggt_omega.utils.load_fn import load_and_preprocess_images  # noqa: E402

t0 = time.time()
VGGT = VGGTOmega().to(DEV).eval()
log("vggt load_state_dict", VGGT.load_state_dict(torch.load(args.ckpt, map_location="cpu", weights_only=True)))
torch.cuda.synchronize()
LOAD_S = time.time() - t0
MEM_LOADED_MB = torch.cuda.memory_allocated() / 2**20
log(f"models loaded in {LOAD_S:.1f}s, allocated {MEM_LOADED_MB:.0f} MB (VGGT-Omega only; segmentation via SAM 3.1)")


# ------------------------------------------------------------------ geometry (analyze.py, generalized to any T)
def fit_table(X1, conf, cam_z, support_z, rng, thr_rel=0.004, iters=400):
    """RANSAC near-horizontal plane on unit-scale world-oriented points X1 (origin at the camera centre)."""
    valid = conf > np.percentile(conf, 10)
    idx = np.flatnonzero(valid.reshape(-1))
    pts = X1.reshape(-1, 3)
    thr = thr_rel * np.median(np.linalg.norm(pts[idx], axis=1))
    sub = rng.choice(idx, size=min(len(idx), 20000), replace=False)
    best, best_n = None, -1
    for _ in range(iters):
        a, b, c = pts[rng.choice(sub, 3, replace=False)]
        n = np.cross(b - a, c - a)
        nn = np.linalg.norm(n)
        if nn < 1e-12:
            continue
        n = n / nn
        if n[2] < 0:
            n = -n
        if n[2] < math.cos(math.radians(25)):
            continue
        d = -n @ a
        cnt = int((np.abs(pts[sub] @ n + d) < thr).sum())
        if cnt > best_n:
            best, best_n = (n, d), cnt
    if best is None:
        raise ValueError("no_horizontal_support_plane")
    n, d = best
    inl = (np.abs(pts @ n + d) < thr) & valid.reshape(-1)
    Q = pts[inl]
    cen = Q.mean(0)
    _, _, vt = np.linalg.svd(Q - cen, full_matrices=False)
    n = vt[2] if vt[2][2] > 0 else -vt[2]
    d = -n @ cen
    inl = (np.abs(pts @ n + d) < thr) & valid.reshape(-1)
    zs = pts[inl, 2]
    s = float(np.median((support_z - cam_z) / zs))
    if not (math.isfinite(s) and s > 0):
        raise ValueError("invalid_support_scale")
    tilt = math.degrees(math.acos(min(1.0, abs(n[2]))))
    resid = np.abs(cam_z + s * zs - support_z)
    return s, dict(tilt_deg=tilt, residual_p50_m=float(np.median(resid)), residual_p90_m=float(np.percentile(resid, 90)),
                   inlier_frac=float(inl.mean()))


def reconstruct(img_rgb, K, T, support_z, allow_no_table=False):
    H, W = img_rgb.shape[:2]
    t = time.time()
    if H % 16 == 0 and W % 16 == 0:
        ims = torch.from_numpy(img_rgb).permute(2, 0, 1).float().div(255.0)[None].to(DEV)
        mode = "native"
    else:   # official balanced preprocessing, depth resized back to the original grid
        p = f"/tmp/vggt_svc_{os.getpid()}.png"
        Image.fromarray(img_rgb).save(p)
        ims = load_and_preprocess_images([p], mode="balanced", image_resolution=512).to(DEV)
        mode = "balanced"
    with torch.inference_mode():
        pr = VGGT(ims)
    torch.cuda.synchronize()
    dt = time.time() - t
    h, w = ims.shape[-2:]
    depth = pr["depth"].float().cpu().numpy().reshape(h, w)
    conf = pr["depth_conf"].float().cpu().numpy().reshape(h, w)
    if (h, w) != (H, W):
        depth = cv2.resize(depth, (W, H), interpolation=cv2.INTER_LINEAR)
        conf = cv2.resize(conf, (W, H), interpolation=cv2.INTER_LINEAR)
    uu, vv = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    Pc = np.stack([(uu - K[0, 2]) / K[0, 0] * depth, (vv - K[1, 2]) / K[1, 1] * depth, depth], -1)
    X1 = Pc @ T[:3, :3].T
    rng = np.random.default_rng(0)       # deterministic per image
    try:
        s, table = fit_table(X1, conf, T[2, 3], support_z, rng)
    except ValueError as ex:
        if not allow_no_table:
            raise
        s, table = 1.0, dict(error=str(ex))   # hp: unscaled points (held rows rescale them to the FK depth anyway)
    X = T[:3, 3] + s * X1
    return dict(X=X.astype(np.float32), conf=conf.astype(np.float32), conf_p10=float(np.percentile(conf, 10)),
                s=s, table=table, vggt_s=dt, mode=mode, hw=(H, W))


K3 = np.ones((3, 3), np.uint8)


def estimate(rec, mask, support_z):
    """analyze.py estimate(): VGGT points of a mask -> top (p98 z) and centre height."""
    m2 = cv2.erode(mask.astype(np.uint8), K3, iterations=2).astype(bool)
    if m2.sum() < 15:
        m2 = mask
    sel = m2 & (rec["conf"] > rec["conf_p10"])
    if sel.sum() < 5:
        sel = m2
    P = rec["X"][sel].astype(np.float64)
    if len(P) < 3:
        return None
    above = P[:, 2] > support_z + ABOVE
    if above.sum() >= 10:
        P = P[above]
    rng = np.random.default_rng(0)
    if len(P) > 4000:
        P = P[rng.choice(len(P), 4000, replace=False)]
    lab = DBSCAN(eps=0.01, min_samples=5).fit_predict(P)
    if (lab >= 0).any():
        P = P[lab == np.bincount(lab[lab >= 0]).argmax()]
    z98 = float(np.percentile(P[:, 2], 98))
    lo, hi = np.percentile(P[:, :2], 2, axis=0), np.percentile(P[:, :2], 98, axis=0)
    return dict(top_z_m=z98, n_points=int(len(P)), xy_extent_m=[float(v) for v in (hi - lo)],
                xy_center_m=[float(v) for v in (lo + hi) / 2])


def window(rec, u, v, support_z):
    H, W = rec["hw"]
    ui, vi = int(u), int(v)
    sl = (slice(max(0, vi - WIN), vi + WIN + 1), slice(max(0, ui - WIN), ui + WIN + 1))
    Pw = rec["X"][sl].reshape(-1, 3)
    cw = rec["conf"][sl].reshape(-1) > rec["conf_p10"]
    Pw = Pw[cw & (Pw[:, 2] > support_z + ABOVE)]
    if len(Pw) < 10:
        return None
    return dict(top_z_m=float(np.percentile(Pw[:, 2], 98)), n_points=int(len(Pw)))


def sam31_points(image_b64, points, H, W, timeout_s=60.0):
    """SAM 3.1 interactive point prompts on the SAM 3.1 service -> [(masks (3,H,W) bool, ious (3,))]."""
    if not len(points):
        return []
    body = dict(image_b64=image_b64, points=[[float(u), float(v)] for u, v in points], phrases=[])
    rq = urllib.request.Request(args.sam31.rstrip("/") + "/segment", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout_s) as r:
            ans = json.loads(r.read())
    except urllib.error.HTTPError as err:
        ans = json.loads(err.read())
    if ans.get("status") != "ok":
        raise RuntimeError(str(ans.get("error", "sam31_error"))[:200])
    if list(ans["image_size"]) != [W, H]:
        raise RuntimeError(f"sam31_image_size {ans['image_size']} != {[W, H]}")
    out = []
    for pt_res in ans.get("points") or []:
        ms = [np.unpackbits(np.frombuffer(base64.b64decode(m["mask_b64"]), np.uint8))[:H * W].reshape(H, W).astype(bool)
              for m in pt_res["masks"]]
        masks = np.stack(ms, axis=0) if ms else np.zeros((0, H, W), bool)
        ious = np.asarray([float(m["iou"]) for m in pt_res["masks"]], np.float32)
        out.append((masks, ious))
    return out


def pick_mask(masks, iou, u, v):
    """Highest predicted IoU among masks that contain the pixel and are not support/background-sized."""
    H, W = masks.shape[-2:]
    ui, vi = min(int(u), W - 1), min(int(v), H - 1)
    cand = [(float(q), i) for i, q in enumerate(iou) if masks[i][vi, ui] and masks[i].sum() <= MAX_MASK_FRAC * H * W]
    if not cand:
        return None, None
    q, i = max(cand)
    return masks[i], q


# ------------------------------------------------------------------ object table (Auriga features.object_table)
# POST /object_table {image_b64, K, T_world_camera, support_z_m, pixels?, robot_segments_m?, max_objects?,
#                     max_object_height_m?, params?, timeout_s?}
# Automatic SAM 2.1 masks (point-prompt grid restricted to pixels whose VGGT point lies above the support plane)
# -> drop support / background / robot masks -> per mask VGGT points -> centre, top, bottom, footprint (PCA yaw,
# length, width), principal-axis tilt, support relation, depth confidence, plausibility flags. No names, no truth.
OT_VERSION = "object-table-v5-hp1"
OT = dict(stride=8, min_above=0.004, bg_height=0.45, max_area_frac=0.08, min_area=40, robot_click_cut=0.90, iou=0.50, stab=0.80, min_relief=0.004,
          above_frac=0.6, overlap=0.3, batch=64, robot_radius=0.05, robot_frac=0.5, cluster_frac=0.6,
          contact_margin=0.03, max_object_height=0.30, low_conf_rank=0.25, support_gap=0.02, order="area",
          floating=0.03, reanchor=1, reanchor_mode="thickness", reanchor_margin=0.0, reuse_iou=0.5, mask_core=0,
          seg_backend="sam31", label_thr=0.4, label_max=20, label_iou=0.5, text_max_area_frac=0.08, auto_with_labels=0,
          auto_phrase="object", part_frac=0.5)
# object-table-v5 (2026-10-20): optional request field labels:[str] (phrases chosen by the CALLER, never by this service).
# seg_backend "sam31" (default): each phrase -> SAM 3.1 text-prompted instance masks (sam31_service.py, label_thr) -> the
# same filters and geometry as the automatic masks; an instance overlapping an already chosen object (IoU >= label_iou)
# adds its phrase to that object. With labels the automatic SAM 2.1 grid is skipped (auto_with_labels=1 keeps it).
# Every object carries label / label_score / labels; the response lists per phrase the object indices and a status.
# Without labels, or with seg_backend "sam21", or when the SAM 3.1 service fails: the v4 automatic SAM 2.1 path.
# Requested pixels always use the SAM 2.1 point prompt with the v4 reuse rule.
# Automatic part (seg_backend sam31): the class-agnostic prompt auto_phrase ("object"; a fixed generic word, not an
# object name) runs with the caller's phrases; its instances are added after the labelled ones, unlabelled, unless they
# repeat a chosen object (IoU >= label_iou) or lie mostly (> part_frac of their area) inside one. auto_phrase "" = labels only.
# reanchor_mode: "always" = rescale every floating object (lowest visible point > floating above the support plane,
# no supporting object) onto the table (object-table-v1); "thickness" = rescale only when the floating height
# (low_z - support_z) also exceeds the visible thickness of the raw point set (top_z - low_z) + reanchor_margin.
# Objects whose bottom is hidden (spheres, drawers) are then kept as measured and flagged "floating".
# object-table-v4 (2026-10-20): a requested pixel reuses an automatic object only when the pixel lies in that mask's core (its largest
# connected component; a SAM mask can contain detached parts, e.g. the rack mask also covering a separate white tube)
# AND the point-prompt mask at the pixel overlaps the object's mask by IoU >= reuse_iou; otherwise the point-prompt
# mask becomes a new object. Requested pixels that resolve to the same object are marked (same_object_as, status
# "ok_same_object"). mask_core=1 (default off) also cuts every automatic mask to its core before the greedy selection.


def _span_px(mask):
    ys, xs = np.nonzero(mask)
    return float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min())) if len(xs) else 0.0


def _erode(mask, *, thin=False):
    u8 = mask.astype(np.uint8)
    if thin:
        total = int(u8.sum())
        span0 = _span_px(mask)
        for it in (2, 1):
            m2 = cv2.erode(u8, K3, iterations=it).astype(bool)
            if m2.sum() >= max(15, int(0.40 * total)) and _span_px(m2) >= 0.78 * span0:
                return m2
        return mask
    m2 = cv2.erode(u8, K3, iterations=2).astype(bool)
    return m2 if m2.sum() >= 15 else mask


def _seg_dist(P, a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = b - a
    t = np.clip(((P - a) @ d) / max(float(d @ d), 1e-12), 0, 1)
    return np.linalg.norm(P - (a + t[:, None] * d), axis=1)


def _shape(P):
    """Geometry of a cleaned point set (world, metres): footprint centre/extent/yaw, top/low, 3D axis tilt."""
    top, low = float(np.percentile(P[:, 2], 98)), float(np.percentile(P[:, 2], 2))
    xy = P[:, :2]
    c0 = xy.mean(0)
    w, v = np.linalg.eigh(np.cov((xy - c0).T) + 1e-12 * np.eye(2))
    major, minor = v[:, 1], v[:, 0]
    yaw = math.degrees(math.atan2(major[1], major[0]))
    yaw = (yaw + 90.0) % 180.0 - 90.0
    a1, a2 = (xy - c0) @ major, (xy - c0) @ minor
    l_lo, l_hi = np.percentile(a1, [2, 98])
    w_lo, w_hi = np.percentile(a2, [2, 98])
    centre_xy = c0 + major * (l_lo + l_hi) / 2 + minor * (w_lo + w_hi) / 2
    w3, v3 = np.linalg.eigh(np.cov((P - P.mean(0)).T) + 1e-12 * np.eye(3))
    return dict(centre_xy=[float(x) for x in centre_xy], top_z_m=top, low_z_m=low, yaw_deg=float(yaw),
                length_m=float(l_hi - l_lo), width_m=float(w_hi - w_lo),
                axis_tilt_deg=float(math.degrees(math.acos(min(1.0, abs(v3[2, 2]))))),
                elongation=float(math.sqrt(max(w3[2], 0) / max(w3[1], 1e-12))))


def object_stats(rec, mask, support_z, T, K, floating=None, *, explicit=False):
    """VGGT points of one mask -> geometry + quality (no names)."""
    m2 = _erode(mask)
    conf = rec["conf"]
    thin_explicit = bool(explicit and int((m2 & (rec["X"][..., 2] > support_z + ABOVE)).sum()) < 45)
    if thin_explicit:
        m2 = _erode(mask, thin=True)
    sel = m2 & (conf > rec["conf_p10"])
    if sel.sum() < 5:
        sel = m2
    if thin_explicit and sel.sum() < 5:
        sel = mask
    P = rec["X"][sel].astype(np.float64)
    if len(P) < 5:
        return None
    above = P[:, 2] > support_z + ABOVE
    if thin_explicit:
        if above.mean() >= 0.5 and above.sum() >= 20:
            P = P[above]
    elif above.sum() >= 10:
        P = P[above]
    rng = np.random.default_rng(0)
    if len(P) > 4000:
        P = P[rng.choice(len(P), 4000, replace=False)]
    lab = DBSCAN(eps=0.01, min_samples=5).fit_predict(P)
    cluster_frac = 0.0
    if (lab >= 0).any():
        big = np.bincount(lab[lab >= 0]).argmax()
        cluster_frac = float((lab == big).mean())
        if not explicit or (lab == big).sum() >= 5:
            P = P[lab == big]
    if len(P) < 5:
        return None
    shape = _shape(P)
    # floating point set (lowest visible point well above the table): the same points rescaled along the camera rays
    # so that the lowest one lies on the table plane (depth offset under the mask; the shape is kept)
    reanchored = None
    if floating is not None and shape["low_z_m"] - support_z > floating:
        C = T[:3, 3]
        lam = (support_z - C[2]) / (shape["low_z_m"] - C[2])
        if 0 < lam < 10:
            reanchored = dict(_shape(C + lam * (P - C)), depth_scale=float(lam))
    # depth confidence of the mask pixels as a rank within this image (0 = least confident pixel of the image)
    cm = conf[m2]
    flat = np.sort(conf.reshape(-1))
    rank = np.searchsorted(flat, cm) / flat.size
    # contact consistency: the lowest mask pixels back-projected onto the support plane should not lie farther from
    # the camera than the object's estimated centre by more than a margin; a centre much nearer the camera than its
    # own contact means the depth under the mask places the object too high.
    ys, xs = np.nonzero(mask)
    vmax = ys.max()
    bot = xs[ys >= vmax - 2]
    ub, vb = float(np.median(bot)) + 0.5, float(vmax) + 0.5
    d = T[:3, :3] @ np.array([(ub - K[0, 2]) / K[0, 0], (vb - K[1, 2]) / K[1, 1], 1.0])
    contact = None
    if d[2] < -1e-6:
        s = (support_z - T[2, 3]) / d[2]
        contact = (T[:3, 3] + s * d)[:2]
    fwd = T[:3, 2][:2]
    fwd = fwd / max(np.linalg.norm(fwd), 1e-9)
    gap = (lambda xy: float(fwd @ (np.asarray(xy) - contact)) if contact is not None else None)
    if reanchored is not None:
        reanchored["contact_gap_m"] = gap(reanchored["centre_xy"])
    return dict(shape, contact_gap_m=gap(shape["centre_xy"]), reanchored=reanchored,
                n_points=int(len(P)), cluster_frac=cluster_frac, conf_rank_p50=float(np.median(rank)),
                conf_rank_p10=float(np.percentile(rank, 10)), conf_p50=float(np.median(cm)), mask_px=int(mask.sum()))


def relief(rec, mask, support_z):
    """p75 height of the mask minus the median height of a ring around it (both VGGT, metres)."""
    m = mask.astype(np.uint8)
    ring = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=6).astype(bool) & ~cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2).astype(bool)
    h = rec["X"][..., 2] - support_z
    if ring.sum() < 10:
        return None
    return float(np.percentile(h[_erode(mask)], 75) - np.median(h[ring]))


def _contour(mask):
    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return []
    c = max(cs, key=cv2.contourArea)
    c = cv2.approxPolyDP(c, 1.5, True).reshape(-1, 2)
    if len(c) > 40:
        c = c[np.linspace(0, len(c) - 1, 40).astype(int)]
    return c.astype(int).tolist()


def _core(mask):
    """Largest 8-connected component of a mask (the part that carries the object's points)."""
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 2:
        return mask
    return lab == 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))


def _label_px(mask):
    dt = cv2.distanceTransform(np.pad(mask.astype(np.uint8), 1), cv2.DIST_L2, 3)[1:-1, 1:-1]
    v, u = np.unravel_index(int(dt.argmax()), dt.shape)
    return [int(u), int(v)]


def sam31_segment(image_b64, phrases, cfg, H, W, timeout_s=60.0):
    """SAM 3.1 text prompts on the second process -> [(mask HxW bool, phrase index, score)], per-phrase counts."""
    body = dict(image_b64=image_b64, phrases=phrases, threshold=float(cfg["label_thr"]), max_per_phrase=int(cfg["label_max"]))
    rq = urllib.request.Request(args.sam31.rstrip("/") + "/segment", data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(rq, timeout=timeout_s) as r:
            ans = json.loads(r.read())
    except urllib.error.HTTPError as err:
        ans = json.loads(err.read())
    if ans.get("status") != "ok":
        raise RuntimeError(str(ans.get("error", "sam31_error"))[:200])
    if list(ans["image_size"]) != [W, H]:
        raise RuntimeError(f"sam31_image_size {ans['image_size']} != {[W, H]}")
    out, counts = [], []
    for li, ph in enumerate(ans["phrases"]):
        counts.append(len(ph["instances"]))
        for inst in ph["instances"]:
            m = np.unpackbits(np.frombuffer(base64.b64decode(inst["mask_b64"]), np.uint8))[:H * W].reshape(H, W).astype(bool)
            out.append((m, li, float(inst["score"])))
    return out, counts, ans.get("timing"), ans.get("gpu_peak_allocated_mb")


def object_table(req):
    t_all = time.time()
    data = base64.b64decode(req["image_b64"])
    K = np.asarray(req["K"], np.float64).reshape(3, 3)
    T = np.asarray(req["T_world_camera"], np.float64).reshape(4, 4)
    support_z = float(req["support_z_m"])
    cfg = dict(OT, **{k: v for k, v in (req.get("params") or {}).items() if k in OT})
    max_objects = int(req.get("max_objects", 20))
    max_h = float(req.get("max_object_height_m", cfg["max_object_height"]))
    segments = [np.asarray(s, float).reshape(2, 3) for s in req.get("robot_segments_m") or []]
    radius = float(req.get("robot_radius_m", cfg["robot_radius"]))
    exclude = [[int(v) for v in b] for b in req.get("exclude_boxes_px") or []]
    torch.cuda.reset_peak_memory_stats()
    entry, hit = _entry(data, K, T, support_z)
    rec = entry["rec"]
    H, W = rec["hw"]
    # robot links drawn into the image (capsules of the same radius projected with the calibration): a mask counts as
    # robot only when it is near the links both in 3D (VGGT points) and in the image, so a depth error under an object
    # cannot move it onto a link
    robot_img = np.zeros((H, W), np.uint8)
    Ti = np.linalg.inv(T)
    for a, b in segments:
        for q in np.linspace(0.0, 1.0, 12):
            pc = Ti[:3, :3] @ (a + q * (b - a)) + Ti[:3, 3]
            if pc[2] <= 0.05:
                continue
            u, v = K[0, 0] * pc[0] / pc[2] + K[0, 2], K[1, 1] * pc[1] / pc[2] + K[1, 2]
            rad = int(np.ceil(K[0, 0] * radius / pc[2]))
            if -rad <= u < W + rad and -rad <= v < H + rad:
                cv2.circle(robot_img, (int(u), int(v)), rad, 1, -1)
    robot_img = robot_img.astype(bool)
    # hp: a caller-supplied robot pixel mask (FK render of the robot meshes, already dilated) is used by held_estimate
    # to strip gripper fingers from held objects; table objects keep the 3D+2D segment check when segments are supplied
    pix_mask = _robot_mask(req, H, W)
    if pix_mask is not None and not segments:
        robot_img = pix_mask
    # caller-supplied phrases -> SAM 3.1 text-prompted instances (second process); failure falls back to the SAM 2.1 grid
    labels = [str(x).strip()[:80] for x in (req.get("labels") or []) if str(x).strip()][:16]
    auto_phrase = str(cfg["auto_phrase"] or "").strip()
    phrases = labels + ([auto_phrase] if auto_phrase and auto_phrase not in labels else [])
    use_text = bool(phrases) and cfg["seg_backend"] == "sam31"
    text_cands, text_counts, text_err, text_timing, text_peak = [], [], None, None, None
    t = time.time()
    if use_text:
        try:
            text_cands, text_counts, text_timing, text_peak = sam31_segment(req["image_b64"], phrases, cfg, H, W,
                                                                            float(req.get("timeout_s", 60)))
        except Exception as ex:
            text_err = f"{type(ex).__name__}: {ex}"[:200]
            use_text = False
    t_text = time.time() - t
    backend = ("sam31_text" + (":labels" if labels else "") + (f":{auto_phrase}" if auto_phrase else "")) if use_text else "sam31_points"
    t_embed = 0.0
    h = rec["X"][..., 2] - support_z
    above = (h > cfg["min_above"]) & (h < cfg["bg_height"]) & (rec["conf"] > rec["conf_p10"])
    grid = []
    border_np = np.zeros((H, W), dtype=bool)
    border_np[:2, :] = True; border_np[-2:, :] = True; border_np[:, :2] = True; border_np[:, -2:] = True
    exclude_np = np.zeros((H, W), dtype=bool)
    for x0, y0, x1, y1 in exclude:      # a mask reaching into an excluded box is treated like one touching the border
        border_np[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
        exclude_np[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    reject = collections.Counter()
    t_sam = 0.0
    chosen, dropped = [], collections.Counter()
    n_cands = 0
    t = time.time()

    def screen(mask, *, explicit=False):
        """Object geometry of one mask, or (None, reason, occupy) for support / background / fragmented / robot masks."""
        if pix_mask is not None and not segments:
            if pix_mask[mask].mean() >= cfg["robot_frac"]:
                return None, "robot", True
        rel = relief(rec, mask, support_z)          # cheap test first: plain table texture has no relief
        screen_flags = []
        if rel is not None and rel < cfg["min_relief"]:
            if not explicit:
                return None, "no_relief", False
            screen_flags.append("low_relief")
        st = object_stats(rec, mask, support_z, T, K, cfg["floating"], explicit=explicit)
        if st is None:
            return None, "no_points", False
        if st["top_z_m"] - support_z > cfg["bg_height"]:
            return None, "background_height", True
        if st["cluster_frac"] < cfg["cluster_frac"]:
            if not explicit:
                return None, "fragmented", False
            screen_flags.append("fragmented")
        st["relief_m"] = rel
        if screen_flags:
            st["screen_flags"] = screen_flags
        if segments:
            Pm = rec["X"][_erode(mask)].astype(np.float64)
            near = np.min([_seg_dist(Pm, a, b) for a, b in segments], axis=0) < radius
            if near.mean() >= cfg["robot_frac"] and robot_img[mask].mean() >= cfg["robot_frac"]:
                return None, "robot", True
        return st, None, True

    # text-prompted instances, highest score first: same mask filters (text_max_area_frac for size),
    # same screening; an instance that is the same mask as a chosen object (IoU >= label_iou) only adds its phrase
    label_drop = [collections.Counter() for _ in labels]
    for m_np, li, score in sorted(text_cands, key=lambda c: (c[1] >= len(labels), -c[2])):   # labelled first
        generic = li >= len(labels)
        area = float(m_np.sum())
        min_a = cfg["min_area"] if generic else min(cfg["min_area"], 8)
        max_a = (cfg["text_max_area_frac"] if generic else max(cfg["text_max_area_frac"], 0.25)) * H * W
        why = ("small" if area < min_a else "large" if area > max_a
               else "not_above_support" if (generic and (m_np & above).sum() / max(area, 1.0) < cfg["above_frac"])
               else "border_or_excluded" if ((m_np & border_np).any() if generic else (m_np & exclude_np).any()) else None)
        if why is None:
            dup, best_j = None, 0.0
            for i, c in enumerate(chosen):
                inter = float((m_np & c["mask"]).sum())
                if inter:
                    j = inter / float((m_np | c["mask"]).sum())
                    if j >= cfg["label_iou"] and j > best_j:
                        dup, best_j = i, j
            if dup is None and generic and any((m_np & c["mask"]).sum() > cfg["part_frac"] * area for c in chosen):
                dropped["text_part_of_chosen"] += 1
                continue
            if dup is not None:
                if generic:
                    continue
                lab = chosen[dup].setdefault("labels", [])
                if labels[li] not in [x[0] for x in lab]:
                    lab.append([labels[li], round(score, 3)])
                continue
            st, why, _ = screen(m_np, explicit=not generic)
            if st is not None:
                if not generic and (m_np & above).sum() / max(area, 1.0) < cfg["above_frac"]:
                    st.setdefault("screen_flags", []).append("thin_or_on_support")
                if not generic and (m_np & border_np).any():
                    st.setdefault("screen_flags", []).append("touches_border")
                chosen.append(dict(mask=m_np, iou_pred=score, stability=None, auto=False, text=True,
                                   labels=[] if generic else [[labels[li], round(score, 3)]], **st))
                continue
        if not generic:
            label_drop[li][why] += 1
        dropped["text_" + why] += 1
    # model-requested pixels: SAM 3.1 point prompt each; reuse an existing object only when the pixel lies in its mask core and
    # the point-prompt mask is the same object (IoU >= reuse_iou); otherwise the point-prompt mask is a new object
    requested = []
    req_pixels = np.asarray(req.get("pixels") or [], np.float64).reshape(-1, 2)
    valid_req_pts = [(float(u), float(v)) for u, v in req_pixels if 0 <= u < W and 0 <= v < H]
    t_pt0 = time.time()
    pt_masks_list = sam31_points(req["image_b64"], valid_req_pts, H, W, float(req.get("timeout_s", 60))) if valid_req_pts else []
    t_sam += time.time() - t_pt0
    pt_idx = 0
    for u, v in req_pixels:
        if not (0 <= u < W and 0 <= v < H):
            requested.append(dict(pixel=[float(u), float(v)], status="pixel_out_of_bounds"))
            continue
        ui, vi = int(u), int(v)
        masks, iou = pt_masks_list[pt_idx]
        pt_idx += 1
        pmask, q = pick_mask(masks, iou, u, v)
        found, best, why = None, 0.0, None
        for i, c in enumerate(chosen):
            if not c["mask"][vi, ui]:
                continue
            if "core" not in c:
                c["core"] = _core(c["mask"])
            if not c["core"][vi, ui]:
                why = "pixel_in_detached_part"
                continue
            if pmask is None:        # no usable point mask: the core containment alone decides
                found, best = i, None
                break
            j = float((pmask & c["mask"]).sum()) / max(float((pmask | c["mask"]).sum()), 1.0)
            if j >= cfg["reuse_iou"] and j > best:
                found, best = i, j
            elif found is None:
                why = "low_iou"
        if found is not None:
            chosen[found].setdefault("requested", []).append([float(u), float(v)])
            requested.append(dict(pixel=[float(u), float(v)], status="ok", index=found, match="reused",
                                  reuse_iou=None if best is None else round(best, 3)))
            continue
        # The caller's robot pixel mask is cut from a point mask even when robot segments are supplied (the 3D+2D
        # segment check of screen() does not run on this path); too little left means no object. Without it a click on
        # or next to the gripper returned the arm itself as the requested object.
        # A click on the robot whose mask is almost all robot pixels targeted the robot: the few pixels left after the
        # cut are fragments around the gripper, not an object.
        if pmask is not None and pix_mask is not None:
            area_before = int(pmask.sum())
            pmask = pmask & ~pix_mask
            left = int(pmask.sum())
            if left < cfg["min_area"] or (pix_mask[vi, ui] and left <= (1.0 - cfg["robot_click_cut"]) * area_before):
                pmask = None
        st = object_stats(rec, pmask, support_z, T, K, cfg["floating"], explicit=True) if pmask is not None else None
        if st is None:
            requested.append(dict(pixel=[float(u), float(v)], status="no_object_mask"))
            continue
        st["relief_m"] = relief(rec, pmask, support_z)
        chosen.append(dict(mask=pmask, iou_pred=q, stability=None, auto=False, point_only=True, requested=[[float(u), float(v)]], **st))
        requested.append(dict(pixel=[float(u), float(v)], status="ok", index=len(chosen) - 1, match="new_point_mask",
                              **({"not_reused": why} if why else {})))
    if DEBUG_DIR:
        np.savez_compressed(os.path.join(DEBUG_DIR, f"ot_{hashlib.sha256(data).hexdigest()[:12]}_{int(time.time()*1000)}.npz"),
                            masks=np.stack([c["mask"] for c in chosen]) if chosen else np.zeros((0, H, W), bool),
                            auto=np.array([c["auto"] for c in chosen]), requested=json.dumps(requested))
    # cap: requested objects always kept; then by predicted mask quality
    order = sorted(range(len(chosen)), key=lambda i: (not chosen[i].get("requested"), not chosen[i].get("labels"),
                                                      -(chosen[i]["iou_pred"] or 0)))
    keep = sorted(order[:max(max_objects, sum(1 for c in chosen if c.get("requested")))])
    omitted = len(chosen) - len(keep)
    remap = {old: new for new, old in enumerate(keep)}
    chosen = [chosen[i] for i in keep]
    for r in requested:
        if "index" in r:
            r["index"] = remap.get(r["index"])
    by_index = collections.defaultdict(list)
    for k, r in enumerate(requested):
        if r.get("index") is not None:
            by_index[r["index"]].append(k)
    for ks in by_index.values():
        if len(ks) > 1:
            for k in ks:
                requested[k]["status"] = "ok_same_object"
                requested[k]["same_object_as"] = [j for j in ks if j != k]   # positions in the request's pixels list
    # support relation and bottom height
    for i, c in enumerate(chosen):
        c["support"], c["bottom_z_m"] = "unknown", c["low_z_m"]
        if c["low_z_m"] - support_z <= cfg["support_gap"]:
            c["support"], c["bottom_z_m"] = "table", support_z
            continue
        best = None
        c_area = c["length_m"] * c["width_m"]
        for j, d in enumerate(chosen):
            if j == i or d["top_z_m"] <= support_z + 0.01:
                continue
            on_top = (d["top_z_m"] < c["low_z_m"] + cfg["support_gap"]
                      and d["top_z_m"] < c["top_z_m"] - 0.004
                      and d["low_z_m"] < c["low_z_m"])
            in_container = (c["low_z_m"] <= d["top_z_m"] + cfg["support_gap"]
                            and c["top_z_m"] >= d["low_z_m"] - 0.01
                            and d["length_m"] * d["width_m"] > c_area * 1.25)
            if not (on_top or in_container):
                continue
            rel = np.asarray(c["centre_xy"]) - np.asarray(d["centre_xy"])
            ax = np.array([math.cos(math.radians(d["yaw_deg"])), math.sin(math.radians(d["yaw_deg"]))])
            inside = (abs(rel @ ax) <= d["length_m"] / 2 + 0.015 and abs(rel @ np.array([-ax[1], ax[0]])) <= d["width_m"] / 2 + 0.015)
            if inside and (best is None or d["top_z_m"] > chosen[best]["top_z_m"]):
                best = j
        if best is not None:
            c["support"] = best
            c["bottom_z_m"] = chosen[best]["top_z_m"] if (
                chosen[best]["top_z_m"] < c["low_z_m"] + cfg["support_gap"]
                and chosen[best]["top_z_m"] < c["top_z_m"] - 0.004
            ) else c["low_z_m"]
    for c in chosen:
        c["floating"] = c["support"] == "unknown" and c["low_z_m"] - support_z > cfg["floating"]
        rescale = c["floating"] and cfg["reanchor"] and c.get("reanchored") and not c.get("point_only")
        if rescale and cfg["reanchor_mode"] == "thickness":
            rescale = c["low_z_m"] - support_z > (c["top_z_m"] - c["low_z_m"]) + float(cfg["reanchor_margin"])
        if rescale:
            raw = {k: c[k] for k in ("centre_xy", "top_z_m", "low_z_m", "yaw_deg", "length_m", "width_m", "axis_tilt_deg",
                                     "elongation", "contact_gap_m")}
            c.update({k: v for k, v in c["reanchored"].items() if k != "depth_scale"})
            c["raw"], c["support"], c["bottom_z_m"] = raw, "table", support_z
    t_held = time.time()
    held_out = [held_estimate(entry, req["image_b64"], K, T, support_z, pix_mask, item, [c["mask"] for c in chosen])
                for item in (req.get("held") or [])[:2]] if req.get("held") else None
    t_held = time.time() - t_held
    objects = []
    sorted_labs = [sorted(c.get("labels") or [], key=lambda x: -x[1]) for c in chosen]
    primary_counts = collections.Counter(lab[0][0] for lab in sorted_labs if lab)
    for idx_c, lab in enumerate(sorted_labs):
        if len(lab) > 1 and primary_counts[lab[0][0]] > 1:
            for alt_idx in range(1, len(lab)):
                alt_p, _ = lab[alt_idx]
                if primary_counts[alt_p] == 0:
                    primary_counts[lab[0][0]] -= 1
                    primary_counts[alt_p] += 1
                    sorted_labs[idx_c] = [lab[alt_idx], *lab[:alt_idx], *lab[alt_idx + 1:]]
                    break
    for idx_c, c in enumerate(chosen):
        flags = list(c.pop("screen_flags", None) or [])
        if c["floating"]:
            flags.append("floating_rescaled_to_table" if c.get("raw") else "floating")
        if c["top_z_m"] - support_z > max_h:
            flags.append("height_above_max")
        if c["conf_rank_p50"] < cfg["low_conf_rank"]:
            flags.append("low_depth_confidence")
        if (not c["floating"] and c["contact_gap_m"] is not None and not isinstance(c["support"], int)
                and c["contact_gap_m"] < -cfg["contact_margin"]):
            flags.append("contact_mismatch")
        # (support 'unknown', few points and fragmented point sets are reported in the columns / diagnostics only:
        # in the offline validation they did not mark wrong heights)
        mask = c.pop("mask")
        c.pop("reanchored", None)
        c.pop("core", None)
        cols, rows = np.nonzero(mask.any(0))[0], np.nonzero(mask.any(1))[0]
        lab = sorted_labs[idx_c]
        objects.append(dict(c, centre_m=[*c["centre_xy"], (c["top_z_m"] + c["bottom_z_m"]) / 2],
                            height_m=c["top_z_m"] - c["bottom_z_m"], flags=flags, quality="low" if flags else "ok",
                            label_px=_label_px(mask), contour_px=_contour(mask), labels=lab,
                            label=lab[0][0] if lab else None, label_score=lab[0][1] if lab else None,
                            bbox_px=[int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])]))
    # per phrase: which returned objects carry it (an object can carry several phrases; a phrase can match several objects)
    label_report = []
    for li, p in enumerate(labels):
        idx = [i for i, o in enumerate(objects) if p in [x[0] for x in o["labels"]]]
        n_inst = text_counts[li] if li < len(text_counts) else 0
        status = ("backend_unavailable" if text_err else "unsupported_backend" if cfg["seg_backend"] != "sam31"
                  else "ok" if idx else "no_match" if n_inst == 0 else "filtered")
        label_report.append(dict(label=p, status=status, indices=idx, n_instances=n_inst,
                                 **({"filtered": dict(label_drop[li])} if label_drop and label_drop[li] else {})))
    t_obj = time.time() - t
    total = time.time() - t_all
    peak = torch.cuda.max_memory_allocated() / 2**20
    STATS["peak_mb"] = max(STATS["peak_mb"], peak)
    STATS["total_s"].append(total)
    return dict(status="ok", version=OT_VERSION, source="vggt_estimate", image_sha256=hashlib.sha256(data).hexdigest(),
                cache_hit=hit, image_size=[W, H], scale=rec["s"], support_fit=rec["table"], support_z_m=support_z,
                objects=objects, requested=requested, omitted=omitted, dropped=dict(dropped), rejected=dict(reject),
                n_prompts=len(grid), n_candidates=n_cands, params=dict(cfg, max_objects=max_objects, max_object_height=max_h),
                backend=backend, labels=label_report, **({"backend_error": text_err} if text_err else {}),
                **({"held": held_out, "held_s": t_held} if held_out is not None else {}),
                **({"robot_mask": "pixels"} if pix_mask is not None else {}),
                timing=dict(vggt_s=None if hit else rec["vggt_s"], embed_s=t_embed, sam_s=t_sam, text_s=t_text,
                            sam31=text_timing, objects_s=t_obj, total_s=total),
                gpu_peak_allocated_mb=peak, sam31_gpu_peak_allocated_mb=text_peak)


def plausibility(rec, mask, support_z, T, K, top_z, max_h):
    """Flags for /height (pixel_plane plane_source vggt): the same checks as the object table."""
    flags, info = [], {}
    if mask is not None:
        st = object_stats(rec, mask, support_z, T, K)
        if st is not None:
            info = dict(conf_rank_p50=st["conf_rank_p50"], contact_gap_m=st["contact_gap_m"], low_z_m=st["low_z_m"])
            if st["low_z_m"] - support_z > OT["floating"]:
                flags.append("floating")
            if st["conf_rank_p50"] < OT["low_conf_rank"]:
                flags.append("low_depth_confidence")
            if st["contact_gap_m"] is not None and st["contact_gap_m"] < -OT["contact_margin"]:
                flags.append("contact_mismatch")
    if top_z - support_z > max_h:
        flags.append("height_above_max")
    return flags, info


# ------------------------------------------------------------------ hp: held objects (Auriga features.held_perception)
# The robot's own kinematics give the grasp centre (FK link6 + R [0.1501, 0, 0]); camera model and FK are exact in this sim,
# VGGT depth of a held object is not (pick_anatomy/cal_grip). Per held item {arm, grasp_centre_m, phrases?}:
#   mask  = SAM 3.1 text instance (caller phrases + "object", threshold text_thr) whose mask contains the projected grasp
#           centre, else the nearest within max_dist_px, minus the robot pixel mask; fallback: SAM 3.1 point prompt at the
#           nearest non-robot pixel to the projected grasp centre;
#   centre (Kfk) = ray through the median mask pixel at the FK grasp-centre camera depth;
#   shape (GK)   = VGGT points of the mask rescaled along the camera rays so that the points imaged within near_px of the
#           grasp centre sit at the FK depth; cleaned (1 cm DBSCAN largest cluster, z99 trim); footprint as _shape.
# Never the floating -> table rescale. No names, no truth.
HELD = dict(text_thr=0.2, max_per_phrase=10, max_dist_px=40.0, min_px=20, max_area_frac=0.25, near_px=25.0,
            min_points=20, point_fallback=1, max_phrases=3, generic="object")


def _robot_mask(req, H, W):
    b64 = req.get("robot_mask_b64")
    if not b64:
        return None
    shape = [int(v) for v in req.get("robot_mask_shape") or [H, W]]
    if shape != [H, W]:
        raise ValueError(f"robot_mask_shape {shape} != {[H, W]}")
    bits = np.unpackbits(np.frombuffer(base64.b64decode(b64), np.uint8))
    if bits.size < H * W:
        raise ValueError("robot_mask_too_short")
    return bits[:H * W].reshape(H, W).astype(bool)


def _held_pick(cands, robot, pu, pv, H, W, cfg):
    best = None
    ui, vi = int(math.floor(pu)), int(math.floor(pv))
    for m, src, phrase, score in cands:
        mm = m & ~robot if robot is not None else m
        n = int(mm.sum())
        if n < cfg["min_px"] or n > cfg["max_area_frac"] * H * W:
            continue
        inside = 0 <= vi < H and 0 <= ui < W and bool(m[vi, ui])
        if inside:
            dd = 0.0
        else:
            yy, xx = np.nonzero(mm)
            dd = float(np.min(np.hypot(xx + 0.5 - pu, yy + 0.5 - pv)))
        key = (dd, -score)
        if dd <= cfg["max_dist_px"] and (best is None or key < best[0]):
            best = (key, mm, src, phrase, score, dd)
    return best


def held_estimate(entry, image_b64, K, T, support_z, robot, item, chosen_masks=()):
    cfg = dict(HELD, **{k: v for k, v in (item.get("params") or {}).items() if k in HELD})
    rec = entry["rec"]
    H, W = rec["hw"]
    out = dict(arm=item.get("arm"), status="no_mask")
    G = np.asarray(item["grasp_centre_m"], float).reshape(3)
    Ti = np.linalg.inv(T)
    pc = Ti[:3, :3] @ G + Ti[:3, 3]
    if pc[2] <= 0.02:
        out["status"] = "grasp_centre_behind_camera"
        return out
    zg = float(pc[2])
    pu, pv = K[0, 0] * pc[0] / zg + K[0, 2], K[1, 1] * pc[1] / zg + K[1, 2]
    out.update(grasp_px=[float(pu), float(pv)], grasp_depth_m=zg)
    t = time.time()
    phrases = [str(p).strip()[:80] for p in (item.get("phrases") or []) if str(p).strip()][:cfg["max_phrases"]]
    if cfg["generic"] and cfg["generic"] not in phrases:
        phrases.append(cfg["generic"])
    cands, err = [], None
    try:
        tc, _, _, _ = sam31_segment(image_b64, phrases, dict(OT, label_thr=cfg["text_thr"], label_max=cfg["max_per_phrase"]),
                                    H, W, float(item.get("timeout_s", 30)))
        cands = [(m, "sam31_text", phrases[li], s) for m, li, s in tc]
    except Exception as ex:
        err = f"{type(ex).__name__}: {ex}"[:160]
    best = _held_pick(cands, robot, pu, pv, H, W, cfg)
    out["text_s"] = time.time() - t
    if best is None and cfg["point_fallback"]:
        # nearest non-robot pixel to the projected grasp centre (within max_dist_px) as a SAM 3.1 point prompt
        # (main has no SAM 2.1 predictor; PR #35 used a SAM 2.1 point prompt here)
        free = ~robot if robot is not None else np.ones((H, W), bool)
        r = int(cfg["max_dist_px"])
        u0, v0 = int(math.floor(pu)), int(math.floor(pv))
        win = free[max(0, v0 - r):max(0, v0 + r + 1), max(0, u0 - r):max(0, u0 + r + 1)]
        if win.any():
            yy, xx = np.nonzero(win)
            xx, yy = xx + max(0, u0 - r), yy + max(0, v0 - r)
            k = int(np.argmin(np.hypot(xx + 0.5 - pu, yy + 0.5 - pv)))
            qu, qv = float(xx[k]) + 0.5, float(yy[k]) + 0.5
            try:
                answers = sam31_points(image_b64, [(qu, qv)], H, W, float(item.get("timeout_s", 30)))
            except Exception as ex:
                answers, err = [], err or f"{type(ex).__name__}: {ex}"[:160]
            pm, q = pick_mask(*answers[0], qu, qv) if answers and len(answers[0][0]) else (None, None)
            if pm is not None:
                best = _held_pick([(pm, "sam31_point", None, float(q))], robot, pu, pv, H, W, cfg)
                out["point_prompt_px"] = [qu, qv]
    if best is None:
        if err:
            out["text_error"] = err
        return out
    _, mm, src, phrase, score, dd = best
    yy, xx = np.nonzero(mm)
    cu, cv_ = float(np.median(xx)) + 0.5, float(np.median(yy)) + 0.5
    C, R = T[:3, 3], T[:3, :3]
    d = np.array([(cu - K[0, 2]) / K[0, 0], (cv_ - K[1, 2]) / K[1, 1], 1.0])
    centre = C + zg * (R @ d)
    cols, rows = np.nonzero(mm.any(0))[0], np.nonzero(mm.any(1))[0]
    out.update(status="ok", mask_source=src, phrase=phrase, score=float(score), mask_px=int(mm.sum()), dist_px=dd,
               centroid_px=[cu, cv_], centre_m=[float(v) for v in centre], contour_px=_contour(mm), label_px=_label_px(mm),
               bbox_px=[int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])],
               border=bool(mm[:2].any() or mm[-2:].any() or mm[:, :2].any() or mm[:, -2:].any()),
               table_fit=bool(isinstance(rec.get("table"), dict) and "error" not in rec["table"]))
    # GK shape
    sel = _erode(mm) & (rec["conf"] > rec["conf_p10"])
    if sel.sum() < cfg["min_points"]:
        sel = mm & (rec["conf"] > rec["conf_p10"])
    vs, us = np.nonzero(sel)
    X = rec["X"][sel].astype(np.float64)
    gk = None
    if len(X) >= cfg["min_points"]:
        zc = (X - C) @ R[:, 2]
        dpx = np.hypot(us + 0.5 - pu, vs + 0.5 - pv)
        near = dpx < cfg["near_px"]
        if near.sum() < 5:
            near = dpx <= np.percentile(dpx, 20)
        kk = zg / float(np.median(zc[near]))
        P = C + kk * (X - C)
        P = P[P[:, 2] > support_z + 0.003] if (P[:, 2] > support_z + 0.003).sum() >= cfg["min_points"] else P
        if len(P) > 4000:
            P = P[np.random.default_rng(0).choice(len(P), 4000, replace=False)]
        lab = DBSCAN(eps=0.01, min_samples=5).fit_predict(P)
        if (lab >= 0).any():
            P = P[lab == np.bincount(lab[lab >= 0]).argmax()]
        if len(P) >= 5:
            P = P[P[:, 2] <= np.percentile(P[:, 2], 99) + 0.002]
        if len(P) >= cfg["min_points"]:
            sh = _shape(P)
            gk = dict(sh, k=float(kk), n_points=int(len(P)), centre_m=[*sh["centre_xy"], (sh["top_z_m"] + sh["low_z_m"]) / 2])
    out["gk"] = gk
    # the chosen object masks of the same call that are this held mask (IoU >= 0.3 or mostly inside it)
    match, best_j = None, 0.0
    for i, cm in enumerate(chosen_masks):
        inter = float((cm & mm).sum())
        if not inter:
            continue
        j = inter / float((cm | mm).sum())
        if (j >= 0.3 or inter >= 0.5 * float(cm.sum())) and j > best_j:
            match, best_j = i, j
    out["object_index"], out["object_iou"] = match, best_j if match is not None else None
    return out


def held(req):
    """POST /held {image_b64, K, T_world_camera, support_z_m, robot_mask_b64?, robot_mask_shape?, held:[{arm,
    grasp_centre_m, phrases?}]}: held rows of one image (no object table). A missing support plane is tolerated."""
    t_all = time.time()
    data = base64.b64decode(req["image_b64"])
    K = np.asarray(req["K"], np.float64).reshape(3, 3)
    T = np.asarray(req["T_world_camera"], np.float64).reshape(4, 4)
    support_z = float(req["support_z_m"])
    entry, hit = _entry(data, K, T, support_z, allow_no_table=True)
    H, W = entry["rec"]["hw"]
    robot = _robot_mask(req, H, W)
    res = [held_estimate(entry, req["image_b64"], K, T, support_z, robot, item) for item in (req.get("held") or [])[:2]]
    total = time.time() - t_all
    STATS["total_s"].append(total)
    return dict(status="ok", version=OT_VERSION, cache_hit=hit, image_size=[W, H], scale=entry["rec"]["s"],
                held=res, timing=dict(vggt_s=None if hit else entry["rec"]["vggt_s"], total_s=total))


# ------------------------------------------------------------------ scene3d points (Auriga features.scene3d)
# POST /points {image_b64, K, T_world_camera, support_z_m, voxel_m?, robot_segments_m?, robot_radius_m?, params?, timeout_s?}
# Same reconstruction and cache as /height: every pixel's VGGT point (calibrated K + T, scaled to the support plane),
# kept within [support_z - z_below, support_z + z_above] and max_range of the camera, one point per voxel (the most
# confident pixel of that voxel). Returned as npz (base64): xyz float32 (N,3) world frame, conf uint8 (depth-confidence
# rank of that pixel in the image, 0..255 = 0..1), uv uint16 (N,2) source pixel (column, row), count uint16 pixels in
# the voxel, robot bool (within robot_radius of the robot link segments in 3D). No per-object correction.
PTS_VERSION = "scene3d-points-v1"
PTS = dict(voxel=0.01, z_below=0.05, z_above=0.60, max_range=3.0, robot_radius=0.06)


def points(req):
    t_all = time.time()
    data = base64.b64decode(req["image_b64"])
    K = np.asarray(req["K"], np.float64).reshape(3, 3)
    T = np.asarray(req["T_world_camera"], np.float64).reshape(4, 4)
    support_z = float(req["support_z_m"])
    cfg = dict(PTS, **{k: float(v) for k, v in (req.get("params") or {}).items() if k in PTS})
    voxel = float(req.get("voxel_m", cfg["voxel"]))
    if not (0.002 <= voxel <= 0.05):
        raise ValueError("voxel_m_out_of_range")
    radius = float(req.get("robot_radius_m", cfg["robot_radius"]))
    segments = [np.asarray(s, float).reshape(2, 3) for s in req.get("robot_segments_m") or []]
    entry, hit = _entry(data, K, T, support_z)
    rec = entry["rec"]
    H, W = rec["hw"]
    t = time.time()
    X = rec["X"].reshape(-1, 3).astype(np.float64)
    conf = rec["conf"].reshape(-1)
    n_pix = len(conf)
    rank = np.empty(n_pix, np.float64)
    rank[np.argsort(conf, kind="stable")] = np.arange(n_pix) / max(n_pix - 1, 1)
    cam = T[:3, 3]
    keep = np.isfinite(X).all(1) & (X[:, 2] >= support_z - cfg["z_below"]) & (X[:, 2] <= support_z + cfg["z_above"]) \
        & (np.linalg.norm(X - cam, axis=1) <= cfg["max_range"])
    idx = np.flatnonzero(keep)
    q = np.floor(X[idx] / voxel).astype(np.int64)
    q -= q.min(0)
    span = q.max(0) + 1
    key = (q[:, 0] * span[1] + q[:, 1]) * span[2] + q[:, 2]
    order = np.lexsort((-rank[idx], key))           # by voxel, most confident pixel first
    key_sorted = key[order]
    first = np.ones(len(order), bool)
    first[1:] = key_sorted[1:] != key_sorted[:-1]
    starts = np.flatnonzero(first)
    counts = np.diff(np.append(starts, len(order)))
    sel = idx[order[starts]]
    P = X[sel]
    robot = np.zeros(len(sel), bool)
    for a, b in segments:
        robot |= _seg_dist(P, a, b) < radius
    uv = np.stack([sel % W, sel // W], 1).astype(np.uint16)
    buf = io.BytesIO()
    np.savez_compressed(buf, xyz=P.astype(np.float32), conf=np.rint(rank[sel] * 255).astype(np.uint8), uv=uv,
                        count=np.minimum(counts, 65535).astype(np.uint16), robot=robot,
                        support_z=np.float64(support_z), voxel_m=np.float64(voxel), image_size=np.asarray([W, H], np.int32))
    raw = buf.getvalue()
    above = P[(P[:, 2] > support_z + ABOVE) & ~robot]
    bbox = [P.min(0).round(4).tolist(), P.max(0).round(4).tolist()] if len(P) else None
    bbox_above = [above.min(0).round(4).tolist(), above.max(0).round(4).tolist()] if len(above) else None
    t_pts = time.time() - t
    total = time.time() - t_all
    STATS["total_s"].append(total)
    return dict(status="ok", version=PTS_VERSION, source="vggt_estimate", image_sha256=hashlib.sha256(data).hexdigest(),
                cache_hit=hit, image_size=[W, H], scale=rec["s"], support_fit=rec["table"], support_z_m=support_z,
                voxel_m=voxel, z_range_m=[support_z - cfg["z_below"], support_z + cfg["z_above"]], max_range_m=cfg["max_range"],
                n_points=int(len(P)), n_pixels_kept=int(len(idx)), n_pixels=int(n_pix), n_robot=int(robot.sum()),
                n_above_support=int(len(above)), bbox_m=bbox, bbox_above_support_m=bbox_above,
                npz_bytes=len(raw), npz_b64=base64.b64encode(raw).decode("ascii"),
                timing=dict(vggt_s=None if hit else rec["vggt_s"], points_s=t_pts, total_s=total))


# ------------------------------------------------------------------ cache + worker
CACHE = collections.OrderedDict()
CLOCK = threading.Lock()
STATS = dict(requests=0, images=0, cache_hits=0, errors=0, busy_s=0.0, vggt_s=[], total_s=[], peak_mb=0.0,
             started=time.strftime("%F %T %Z"))
Q = queue.Queue(maxsize=args.max_queue)


def image_key(data, K, T, support_z):
    h = hashlib.sha256(data)
    h.update(np.asarray(K, np.float64).tobytes()); h.update(np.asarray(T, np.float64).tobytes())
    h.update(np.float64(support_z).tobytes())
    return h.hexdigest()


def _entry(data, K, T, support_z, allow_no_table=False):
    key = image_key(data, K, T, support_z)
    if allow_no_table and key not in CACHE:
        try:
            return _entry(data, K, T, support_z)
        except ValueError:
            key = key + ":no_table"
    hit = key in CACHE
    if hit:
        CACHE.move_to_end(key)
        entry = CACHE[key]
        STATS["cache_hits"] += 1
    else:
        img = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        rec = reconstruct(img, K, T, support_z, allow_no_table=key.endswith(":no_table"))
        entry = dict(rec=rec, img=img)
        CACHE[key] = entry
        while len(CACHE) > args.cache:
            CACHE.popitem(last=False)
        STATS["images"] += 1
        STATS["vggt_s"].append(rec["vggt_s"])
    return entry, hit


def handle(req):
    t_all = time.time()
    data = base64.b64decode(req["image_b64"])
    K = np.asarray(req["K"], np.float64).reshape(3, 3)
    T = np.asarray(req["T_world_camera"], np.float64).reshape(4, 4)
    support_z = float(req["support_z_m"])
    method = req.get("method", "sam_point")
    if method not in ("sam_point", "window"):
        raise ValueError("unsupported_method")
    pixels = np.asarray(req.get("pixels", []), np.float64).reshape(-1, 2)
    torch.cuda.reset_peak_memory_stats()
    entry, hit = _entry(data, K, T, support_z)
    max_h = float(req.get("max_object_height_m", OT["max_object_height"]))
    rec = entry["rec"]
    H, W = rec["hw"]
    t_sam = 0.0
    valid_pts = [(float(u), float(v)) for u, v in pixels if 0 <= u < W and 0 <= v < H]
    pt_masks_list = []
    if method == "sam_point" and valid_pts:
        t = time.time()
        pt_masks_list = sam31_points(req["image_b64"], valid_pts, H, W, float(req.get("timeout_s", 60)))
        t_sam += time.time() - t
    pt_idx = 0
    out = []
    for u, v in pixels:
        if not (0 <= u < W and 0 <= v < H):
            out.append(dict(pixel=[u, v], status="pixel_out_of_bounds"))
            continue
        ui, vi = int(u), int(v)
        surf = rec["X"][vi, ui].astype(float)
        conf_rank = float((rec["conf"] < rec["conf"][vi, ui]).mean())
        est, used, mask_px, mask_iou, mask = None, None, 0, None, None
        if method == "sam_point":
            masks, iou = pt_masks_list[pt_idx]
            pt_idx += 1
            mask, mask_iou = pick_mask(masks, iou, u, v)
            if mask is not None:
                est = estimate(rec, mask, support_z)
                mask_px = int(mask.sum())
                used = "sam_point_mask" if est is not None else None
        if est is None:
            est = window(rec, u, v, support_z)
            used = "window" if est is not None else None
        if est is None:
            out.append(dict(pixel=[u, v], status="no_points_above_support", surface_point_m=surf.tolist(),
                            depth_conf_rank=conf_rank))
            continue
        top = est["top_z_m"]
        flags, check = plausibility(rec, mask if used == "sam_point_mask" else None, support_z, T, K, top, max_h)
        out.append(dict(pixel=[float(u), float(v)], status="ok", method=used, top_z_m=top, flags=flags, **check,
                        center_z_m=(top + support_z) / 2.0, support_z_m=support_z, height_m=top - support_z,
                        surface_point_m=surf.tolist(), n_points=est["n_points"], mask_px=mask_px,
                        mask_iou_pred=mask_iou, depth_conf_rank=conf_rank,
                        **({"xy_extent_m": est["xy_extent_m"]} if "xy_extent_m" in est else {})))
    total = time.time() - t_all
    peak = torch.cuda.max_memory_allocated() / 2**20
    STATS["peak_mb"] = max(STATS["peak_mb"], peak)
    STATS["total_s"].append(total)
    return dict(status="ok", version=VERSION, source="vggt_estimate", image_sha256=hashlib.sha256(data).hexdigest(),
                cache_hit=hit, image_size=[W, H], vggt_mode=rec["mode"], scale=rec["s"], support_fit=rec["table"],
                pixels=out, timing=dict(vggt_s=None if hit else rec["vggt_s"], sam_s=t_sam, total_s=total),
                gpu_peak_allocated_mb=peak)


def worker():
    while True:
        job = Q.get()
        t = time.time()
        try:
            job["result"] = {"object_table": object_table, "points": points, "held": held}.get(job.get("kind"), handle)(job["req"])
        except Exception as e:     # reported to the caller, never crashes the worker
            STATS["errors"] += 1
            job["result"] = dict(status="error", error=f"{type(e).__name__}: {e}"[:500])
            log("error", traceback.format_exc()[-800:])
        STATS["busy_s"] += time.time() - t
        STATS["requests"] += 1
        job["result"]["queue_wait_s"] = t - job["t_enq"]
        job["done"].set()


threading.Thread(target=worker, daemon=True).start()


def health():
    nv = STATS["vggt_s"][-200:]
    tv = STATS["total_s"][-200:]
    try:        # the SAM 3.1 text backend (second process); object_table falls back to SAM 2.1 when it is down
        with urllib.request.urlopen(args.sam31.rstrip("/") + "/health", timeout=1.5) as r:
            s31 = json.loads(r.read()).get("version")
    except Exception:
        s31 = None
    return dict(status="ok", version=VERSION, object_table=OT_VERSION, points=PTS_VERSION, held="held-v1",
                object_table_backend=OT["seg_backend"], sam31=s31, sam31_endpoint=args.sam31,
                pid=os.getpid(), device=torch.cuda.get_device_name(0),
                cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"), queue_depth=Q.qsize(),
                cached_images=len(CACHE), load_s=LOAD_S, allocated_mb=torch.cuda.memory_allocated() / 2**20,
                reserved_mb=torch.cuda.memory_reserved() / 2**20, peak_request_mb=STATS["peak_mb"],
                requests=STATS["requests"], images=STATS["images"], cache_hits=STATS["cache_hits"], errors=STATS["errors"],
                vggt_s_p50=float(np.median(nv)) if nv else None, total_s_p50=float(np.median(tv)) if tv else None,
                started=STATS["started"])


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, health())
        self._send(404, dict(status="error", error="not_found"))

    def do_POST(self):
        if not (self.path.startswith("/height") or self.path.startswith("/object_table") or self.path.startswith("/points")
                or self.path.startswith("/held")):
            return self._send(404, dict(status="error", error="not_found"))
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._send(400, dict(status="error", error="invalid_json"))
        job = dict(req=req, done=threading.Event(), t_enq=time.time(),
                   kind="object_table" if self.path.startswith("/object_table") else
                   "points" if self.path.startswith("/points") else "held" if self.path.startswith("/held") else "height")
        try:
            Q.put_nowait(job)
        except queue.Full:
            return self._send(503, dict(status="error", error="queue_full"))
        if not job["done"].wait(float(req.get("timeout_s", 120))):
            return self._send(504, dict(status="error", error="timeout_in_queue"))
        self._send(200 if job["result"]["status"] == "ok" else 422, job["result"])


srv = ThreadingHTTPServer((args.host, args.port), H)
srv.daemon_threads = True
if args.status:
    with open(args.status, "w") as fh:
        fh.write(f"UP pid={os.getpid()} port={args.port} gpu={os.environ.get('CUDA_VISIBLE_DEVICES')} "
                 f"since={time.strftime('%F %T %Z')} load_s={LOAD_S:.1f} allocated_mb={MEM_LOADED_MB:.0f}\n")
log(f"listening on {args.host}:{args.port}")
srv.serve_forever()
