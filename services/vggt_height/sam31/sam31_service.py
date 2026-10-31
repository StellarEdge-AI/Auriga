"""SAM 3.1 text-prompt segmentation service (binds 127.0.0.1 by default; own venv, see services/vggt_height/README.md).

Official facebookresearch/sam3 package (7f473cfd98), image model (detector only, build_sam3_image_model with
enable_inst_interactivity=False) loaded from facebook/sam3.1 sam3.1_multiplex.pt: the "detector.*" keys are the image
grounding model (same key mapping as sam3/model_builder.py _load_checkpoint); tracker keys are not used for single images.
  GET  /health
  POST /segment {image_b64, phrases:[str], threshold?: 0.5, max_per_phrase?: 20}
    -> {status, image_size [W,H], phrases:[{phrase, instances:[{score, box_xyxy, area, mask_b64}]}], timing}
       mask_b64 = base64 of np.packbits(mask HxW bool, row-major); unpack with np.unpackbits(...)[:H*W].reshape(H, W).
One GPU worker (lock), per-image backbone cache (LRU). Phrases are whatever the caller sends; this service never names objects.
"""
import argparse
import base64
import collections
import hashlib
import io
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("--host", default=os.environ.get("SAM31_HOST", "127.0.0.1"))
ap.add_argument("--port", type=int, default=int(os.environ.get("SAM31_PORT", "8333")))
ap.add_argument("--ckpt", default=os.environ.get("SAM31_CKPT", "weights/sam3.1/sam3.1_multiplex.pt"))
ap.add_argument("--cache", type=int, default=16)
ap.add_argument("--status", default="")
args = ap.parse_args()
VERSION = "sam31-text-v1"


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


import sam3  # noqa: E402
from sam3.model_builder import build_sam3_image_model  # noqa: E402
from sam3.model.sam3_image_processor import Sam3Processor  # noqa: E402

t0 = time.time()
bpe = os.path.join(os.path.dirname(sam3.__file__), "assets", "bpe_simple_vocab_16e6.txt.gz")
MODEL = build_sam3_image_model(bpe_path=bpe, device="cuda", eval_mode=True, checkpoint_path=None, load_from_HF=False,
                               enable_inst_interactivity=True)
ck = torch.load(args.ckpt, map_location="cpu", weights_only=True)
if "model" in ck and isinstance(ck["model"], dict):
    ck = ck["model"]
sd = {}
for k, v in ck.items():
    if k.startswith("detector."):
        k2 = k.replace("detector.", "", 1)
        sd[k2] = v
        if k2.startswith("backbone.vision_backbone.interactive_convs."):
            sd[k2.replace("backbone.vision_backbone.interactive_convs.", "backbone.vision_backbone.sam2_convs.", 1)] = v
    elif k == "tracker.model.interactivity_no_mem_embed":
        sd["inst_interactive_predictor.model.no_mem_embed"] = v
    elif k.startswith("tracker.model.interactive_sam_prompt_encoder."):
        sd[k.replace("tracker.model.interactive_sam_prompt_encoder.", "inst_interactive_predictor.model.sam_prompt_encoder.", 1)] = v
    elif k.startswith("tracker.model.interactive_sam_mask_decoder."):
        sd[k.replace("tracker.model.interactive_sam_mask_decoder.", "inst_interactive_predictor.model.sam_mask_decoder.", 1)] = v
    elif k.startswith("tracker."):
        sd[k.replace("tracker.", "inst_interactive_predictor.model.", 1)] = v
res = MODEL.load_state_dict(sd, strict=False)
LOAD = dict(ckpt=os.path.basename(args.ckpt), n_ckpt_keys=len(ck), n_loaded_keys=len(sd),
            missing=len(res.missing_keys), unexpected=len(res.unexpected_keys),
            missing_non_rope=len([k for k in res.missing_keys if "freqs_cis" not in k]),
            missing_sample=[k for k in res.missing_keys if "freqs_cis" not in k][:8],
            unexpected_sample=list(res.unexpected_keys)[:8])
del ck, sd
MODEL = MODEL.cuda().eval()
PROC = Sam3Processor(MODEL, confidence_threshold=0.0)
torch.cuda.synchronize()
LOAD_S = time.time() - t0
log("loaded", json.dumps(LOAD), f"{LOAD_S:.1f}s", f"{torch.cuda.memory_allocated() / 2**20:.0f} MB")

CACHE = collections.OrderedDict()
LOCK = threading.Lock()
STATS = dict(requests=0, images=0, errors=0, total_s=[], peak_mb=0.0, started=time.strftime("%F %T %Z"))


def _clone_bb(bb):
    out = {}
    for k, v in bb.items():
        if isinstance(v, dict):
            out[k] = {sk: (list(sv) if isinstance(sv, list) else sv) for sk, sv in v.items()}
        elif isinstance(v, list):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def segment(req):
    t_all = time.time()
    data = base64.b64decode(req["image_b64"])
    phrases = [str(p).strip()[:120] for p in req.get("phrases") or [] if str(p).strip()][:32]
    points = [[float(u), float(v)] for u, v in np.asarray(req.get("points") or [], np.float64).reshape(-1, 2)]
    thr = float(req.get("threshold", 0.5))
    cap = int(req.get("max_per_phrase", 20))
    key = hashlib.sha256(data).hexdigest()
    torch.cuda.reset_peak_memory_stats()
    with torch.autocast("cuda", dtype=torch.bfloat16):
        t = time.time()
        hit = key in CACHE
        if hit:
            CACHE.move_to_end(key)
            img, bb = CACHE[key]
        else:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            st = PROC.set_image(img)
            bb = _clone_bb(st["backbone_out"])
            CACHE[key] = (img, bb)
            while len(CACHE) > args.cache:
                CACHE.popitem(last=False)
            STATS["images"] += 1
        torch.cuda.synchronize()
        t_img = time.time() - t
        W, H = img.size
        out = []
        pts_out = []
        t = time.time()
        for p in phrases:
            state = dict(original_height=H, original_width=W, backbone_out=_clone_bb(bb))
            state = PROC.set_text_prompt(prompt=p, state=state)
            sc = state["scores"].float().cpu().numpy().reshape(-1)
            order = [int(i) for i in np.argsort(-sc) if sc[i] >= thr][:cap]
            masks = state["masks"][:, 0].cpu().numpy() if len(sc) else np.zeros((0, H, W), bool)
            boxes = state["boxes"].float().cpu().numpy()
            inst = []
            for i in order:
                m = masks[i].astype(bool)
                inst.append(dict(score=float(sc[i]), box_xyxy=[float(x) for x in boxes[i]], area=int(m.sum()),
                                 mask_b64=base64.b64encode(np.packbits(m.reshape(-1)).tobytes()).decode("ascii")))
            out.append(dict(phrase=p, instances=inst, n_scored=int(len(sc)), n_above_0_3=int((sc >= 0.3).sum())))
        obj_masks, obj_scores = None, None
        for u, v in points:
            state = dict(original_height=H, original_width=W, backbone_out=_clone_bb(bb))
            pmasks, pious, _ = MODEL.predict_inst(
                state,
                point_coords=np.array([[u, v]], dtype=np.float32),
                point_labels=np.array([1], dtype=np.int32),
                multimask_output=True,
            )
            pmasks_bool = np.asarray(pmasks > 0, dtype=bool)
            pious_f = np.asarray(pious, dtype=np.float64).reshape(-1)
            m_list = [
                dict(
                    iou=float(pious_f[k]),
                    area=int(pmasks_bool[k].sum()),
                    mask_b64=base64.b64encode(np.packbits(pmasks_bool[k].reshape(-1)).tobytes()).decode("ascii"),
                )
                for k in range(len(pmasks_bool))
            ]
            ui, vi = min(max(int(u), 0), W - 1), min(max(int(v), 0), H - 1)
            if (len(pious_f) == 0 or float(pious_f.max()) < 0.70):
                if obj_masks is None:
                    ost = dict(original_height=H, original_width=W, backbone_out=_clone_bb(bb))
                    ost = PROC.set_text_prompt(prompt="object", state=ost)
                    osc = ost["scores"].float().cpu().numpy().reshape(-1)
                    keep = [int(i) for i in np.argsort(-osc) if osc[i] >= 0.4][:20]
                    obj_masks = ost["masks"][keep, 0].cpu().numpy().astype(bool) if keep else np.zeros((0, H, W), bool)
                    obj_scores = osc[keep] if keep else np.zeros((0,), np.float32)
                for om, osc_val in zip(obj_masks, obj_scores):
                    if om[vi, ui]:
                        m_list.append(dict(
                            iou=float(max(osc_val, 0.75)),
                            area=int(om.sum()),
                            mask_b64=base64.b64encode(np.packbits(om.reshape(-1)).tobytes()).decode("ascii"),
                        ))
            pts_out.append(dict(point=[u, v], masks=m_list))
        t_txt = time.time() - t
    peak = torch.cuda.max_memory_allocated() / 2**20
    STATS["peak_mb"] = max(STATS["peak_mb"], peak)
    total = time.time() - t_all
    STATS["total_s"].append(total)
    return dict(status="ok", version=VERSION, image_sha256=key, cache_hit=hit, image_size=[W, H], threshold=thr,
                phrases=out, points=pts_out, timing=dict(image_s=t_img, text_s=t_txt, total_s=total), gpu_peak_allocated_mb=peak)


def health():
    tv = STATS["total_s"][-200:]
    return dict(status="ok", version=VERSION, load=LOAD, load_s=LOAD_S, pid=os.getpid(), device=torch.cuda.get_device_name(0),
                cuda_visible_devices=os.environ.get("CUDA_VISIBLE_DEVICES"), allocated_mb=torch.cuda.memory_allocated() / 2**20,
                reserved_mb=torch.cuda.memory_reserved() / 2**20, peak_request_mb=STATS["peak_mb"], requests=STATS["requests"],
                images=STATS["images"], errors=STATS["errors"], total_s_p50=float(np.median(tv)) if tv else None,
                cached_images=len(CACHE), started=STATS["started"])


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
        if not self.path.startswith("/segment"):
            return self._send(404, dict(status="error", error="not_found"))
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._send(400, dict(status="error", error="invalid_json"))
        with LOCK:
            try:
                r = segment(req)
            except Exception as e:      # reported to the caller, never crashes the server
                STATS["errors"] += 1
                log("error", traceback.format_exc()[-800:])
                r = dict(status="error", error=f"{type(e).__name__}: {e}"[:500])
            STATS["requests"] += 1
        self._send(200 if r["status"] == "ok" else 422, r)


srv = ThreadingHTTPServer((args.host, args.port), H)
srv.daemon_threads = True
if args.status:
    with open(args.status, "w") as fh:
        fh.write(f"UP pid={os.getpid()} port={args.port} gpu={os.environ.get('CUDA_VISIBLE_DEVICES')} "
                 f"since={time.strftime('%F %T %Z')} load_s={LOAD_S:.1f} missing_non_rope={LOAD['missing_non_rope']} "
                 f"unexpected={LOAD['unexpected']}\n")
log(f"listening on {args.host}:{args.port}")
srv.serve_forever()
