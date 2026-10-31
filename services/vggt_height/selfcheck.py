#!/usr/bin/env python3
"""End-to-end self-check of a running VGGT height service (and its SAM 3.1 backend). Standard library only.

  python3 selfcheck.py                                  # http://127.0.0.1:8331, bundled test frame
  python3 selfcheck.py --url http://127.0.0.1:18331     # another instance
  python3 selfcheck.py --compare-url http://host:8331   # also send the same requests to a reference instance and
                                                        # report the largest numeric difference per endpoint
  python3 selfcheck.py --no-sam31                       # SAM 3.1 backend not installed (SAM 2.1 grid only)

Checks: GET /health (versions), POST /height (4 object pixels), POST /object_table without and with caller labels,
POST /points (npz payload), then the answers against testdata/reference_v5.json (outputs of the reference deployment
on the same frame; a large difference means wrong weights, wrong package versions or a different service version).
Exit code: 0 = all checks passed, 1 = a check failed, 2 = service unreachable.
"""
import argparse
import base64
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED = {"version": "vggt-height-v2", "object_table": "object-table-v5", "points": "scene3d-points-v1",
            "sam31": "sam31-text-v1"}
# object-table-v5-hp1 adds optional request fields and POST /held; requests without them (all of the requests below)
# take the v5 code path, so the same reference answers apply
ACCEPTED = {"object_table": ("object-table-v5", "object-table-v5-hp1")}
# fields that legitimately differ between runs / instances (timing, cache state, memory)
VOLATILE = {"timing", "queue_wait_s", "cache_hit", "gpu_peak_allocated_mb", "sam31_gpu_peak_allocated_mb", "npz_b64"}


def call(url, path, body=None, timeout=300.0):
    req = urllib.request.Request(url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ans = json.loads(r.read())
    except urllib.error.HTTPError as err:          # 422 carries a JSON error body
        ans = json.loads(err.read() or b"{}")
        ans.setdefault("http_status", err.code)
    return ans, time.time() - t


def requests_for(frame, image_b64):
    base = dict(image_b64=image_b64, K=frame["K"], T_world_camera=frame["T_world_camera"],
                support_z_m=frame["support_z_m"])
    return [("height", "/height", dict(base, pixels=frame["pixels"], method="sam_point")),
            ("object_table", "/object_table", dict(base)),
            ("object_table_labels", "/object_table", dict(base, labels=frame["labels"])),
            ("points", "/points", dict(base, voxel_m=0.005))]


def numeric_diff(a, b, path="", out=None):
    """Largest absolute difference over the numeric leaves of two JSON trees; structural mismatches are listed."""
    out = out if out is not None else {"max_abs": 0.0, "where": None, "mismatch": []}
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in VOLATILE:
                continue
            if k not in a or k not in b:
                out["mismatch"].append(f"{path}/{k}: missing on one side")
                continue
            numeric_diff(a[k], b[k], f"{path}/{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out["mismatch"].append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            numeric_diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, bool) or isinstance(b, bool) or a is None or b is None or isinstance(a, str) or isinstance(b, str):
        if a != b:
            out["mismatch"].append(f"{path}: {str(a)[:60]!r} != {str(b)[:60]!r}")
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not (math.isfinite(a) and math.isfinite(b)):
            if not (a != a and b != b) and a != b:
                out["mismatch"].append(f"{path}: {a} != {b}")
        elif abs(a - b) > out["max_abs"]:
            out["max_abs"], out["where"] = abs(a - b), path
    elif a != b:
        out["mismatch"].append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
    return out


class Report:
    def __init__(self):
        self.fails, self.lines = 0, []

    def check(self, ok, what, detail=""):
        self.lines.append(("PASS" if ok else "FAIL", what, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {what}" + (f"  ({detail})" if detail else ""), flush=True)
        self.fails += 0 if ok else 1
        return ok

    def note(self, what, detail=""):
        self.lines.append(("INFO", what, detail))
        print(f"[INFO] {what}" + (f"  ({detail})" if detail else ""), flush=True)


def check_height(rep, ans, frame):
    if not rep.check(ans.get("status") == "ok", "/height status ok", ans.get("error", "")):
        return
    px = ans.get("pixels") or []
    rep.check(len(px) == len(frame["pixels"]), "/height one answer per pixel", f"{len(px)}")
    for p in px:
        ok = (p.get("status") == "ok" and p.get("method") in ("sam_point_mask", "window")
              and all(isinstance(p.get(k), (int, float)) for k in ("top_z_m", "center_z_m", "height_m", "n_points"))
              and -0.01 <= p["height_m"] <= 0.35 and isinstance(p.get("flags"), list))
        rep.check(ok, f"/height pixel {p.get('pixel')}",
                  f"status={p.get('status')} method={p.get('method')} height_m={p.get('height_m')}")
    rep.check(isinstance(ans.get("scale"), (int, float)) and ans["scale"] > 0 and isinstance(ans.get("support_fit"), dict),
              "/height support plane fit", f"scale={ans.get('scale')} tilt_deg={(ans.get('support_fit') or {}).get('tilt_deg')}")


def check_table(rep, ans, name, want_text):
    if not rep.check(ans.get("status") == "ok", f"{name} status ok", ans.get("error", "")):
        return
    objs = ans.get("objects") or []
    rep.check(len(objs) >= 1, f"{name} objects found", f"n={len(objs)} backend={ans.get('backend')}")
    bad = [i for i, o in enumerate(objs) if not (len(o.get("centre_m") or []) == 3 and isinstance(o.get("height_m"), (int, float))
                                                 and len(o.get("bbox_px") or []) == 4 and isinstance(o.get("flags"), list))]
    rep.check(not bad, f"{name} object fields (centre_m, height_m, bbox_px, flags)", f"bad={bad}")
    if want_text:
        rep.check(str(ans.get("backend", "")).startswith("sam31_text"), f"{name} uses the SAM 3.1 text backend",
                  f"backend={ans.get('backend')} error={ans.get('backend_error')}")
    if name.endswith("labels"):
        st = [(x.get("label"), x.get("status")) for x in ans.get("labels") or []]
        ok = any(s == "ok" for _, s in st) if want_text else all(s in ("backend_unavailable", "unsupported_backend") for _, s in st)
        rep.check(bool(st) and ok, f"{name} per-label report", f"{st}")


def check_points(rep, ans):
    if not rep.check(ans.get("status") == "ok", "/points status ok", ans.get("error", "")):
        return
    try:
        names = zipfile.ZipFile(io.BytesIO(base64.b64decode(ans["npz_b64"]))).namelist()
    except Exception as ex:          # noqa: BLE001
        names = [f"decode error {ex}"]
    rep.check(ans.get("n_points", 0) > 0 and {"xyz.npy", "conf.npy", "uv.npy"} <= set(names), "/points npz payload",
              f"n_points={ans.get('n_points')} members={names}")


def check_reference(rep, answers, ref, tol_h, tol_c, tables=True):
    h, rh = answers.get("height") or {}, ref.get("height") or {}
    for p, q in zip(h.get("pixels") or [], rh.get("pixels") or []):
        if p.get("status") == q.get("status") == "ok":
            d = abs(p["top_z_m"] - q["top_z_m"])
            rep.check(d <= tol_h, f"reference /height top_z at {q['pixel']}", f"|d|={d * 100:.2f} cm (tol {tol_h * 100:.1f} cm)")
    for key in ("object_table", "object_table_labels") if tables else ():
        a, r = answers.get(key) or {}, ref.get(key) or {}
        if a.get("status") != "ok" or r.get("status") != "ok":
            continue
        ra, rb = [o["centre_m"] for o in r["objects"]], [o["centre_m"] for o in a["objects"]]
        matched = sum(1 for c in ra if rb and min(math.dist(c, d) for d in rb) <= tol_c)
        rep.check(matched >= max(1, math.ceil(0.8 * len(ra))), f"reference {key} objects within {tol_c * 100:.0f} cm",
                  f"{matched}/{len(ra)} reference objects matched, {len(rb)} returned")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("VGGT_ENDPOINT", "http://127.0.0.1:8331"))
    ap.add_argument("--frame", default=os.path.join(HERE, "testdata", "organize_table_head.json"))
    ap.add_argument("--reference", default=os.path.join(HERE, "testdata", "reference_v5.json"))
    ap.add_argument("--no-reference", action="store_true", help="skip the comparison with the bundled reference answers")
    ap.add_argument("--no-sam31", action="store_true", help="the SAM 3.1 backend is not installed; expect the SAM 2.1 grid")
    ap.add_argument("--compare-url", default="", help="second instance: same requests, report numeric differences")
    ap.add_argument("--compare-tol", type=float, default=1e-3, help="max abs numeric difference allowed vs --compare-url")
    ap.add_argument("--tol-height", type=float, default=0.01, help="m, /height top_z vs reference")
    ap.add_argument("--tol-centre", type=float, default=0.02, help="m, object centres vs reference")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--write-reference", default="", help="(maintainers) write the answers of --url as a new reference file")
    ap.add_argument("--json-out", default="", help="write all answers and the check list to this file")
    a = ap.parse_args()

    frame = json.load(open(a.frame))
    image_b64 = base64.b64encode(open(os.path.join(os.path.dirname(a.frame), frame["image"]), "rb").read()).decode()
    rep = Report()
    try:
        health, dt = call(a.url, "/health", timeout=10)
    except Exception as ex:          # noqa: BLE001
        print(f"[FAIL] {a.url}/health unreachable: {ex}")
        return 2
    rep.note("health", json.dumps({k: health.get(k) for k in ("version", "object_table", "points", "object_table_backend",
                                                                "sam31", "sam31_endpoint", "device", "cuda_visible_devices")}))
    rep.check(health.get("status") == "ok", "/health status ok")
    for k in ("version", "object_table", "points"):
        allowed = ACCEPTED.get(k, (EXPECTED[k],))
        rep.check(health.get(k) in allowed, f"/health {k} in {'/'.join(allowed)}", f"got {health.get(k)}")
    want_text = not a.no_sam31
    if want_text:
        rep.check(health.get("object_table_backend") == "sam31" and health.get("sam31") == EXPECTED["sam31"],
                  "/health SAM 3.1 backend reachable", f"backend={health.get('object_table_backend')} sam31={health.get('sam31')} "
                  f"endpoint={health.get('sam31_endpoint')}")

    answers, timings = {}, {}
    for name, path, body in requests_for(frame, image_b64):
        answers[name], timings[name] = call(a.url, path, dict(body, timeout_s=a.timeout), timeout=a.timeout + 10)
        rep.note(f"{name} answered", f"{timings[name]:.2f} s, status={answers[name].get('status')}")
    check_height(rep, answers["height"], frame)
    check_table(rep, answers["object_table"], "/object_table", want_text)
    check_table(rep, answers["object_table_labels"], "/object_table labels", want_text)
    check_points(rep, answers["points"])

    if a.write_reference:
        slim = {k: {kk: vv for kk, vv in v.items() if kk not in VOLATILE} for k, v in answers.items()}
        json.dump(dict(created=time.strftime("%F %T %Z"), service=health.get("version"), object_table=health.get("object_table"),
                       device=health.get("device"), frame=os.path.basename(a.frame), answers=slim),
                  open(a.write_reference, "w"), indent=1)
        rep.note("reference written", a.write_reference)
    elif not a.no_reference and os.path.exists(a.reference):
        # the bundled reference tables come from the SAM 3.1 backend; with --no-sam31 only /height is compared
        check_reference(rep, answers, json.load(open(a.reference))["answers"], a.tol_height, a.tol_centre, tables=want_text)

    if a.compare_url:
        for name, path, body in requests_for(frame, image_b64):
            other, _ = call(a.compare_url, path, dict(body, timeout_s=a.timeout), timeout=a.timeout + 10)
            d = numeric_diff(answers[name], other)
            rep.check(d["max_abs"] <= a.compare_tol and not d["mismatch"], f"compare {name} with {a.compare_url}",
                      f"max_abs={d['max_abs']:.3g} at {d['where']}; mismatches={d['mismatch'][:5]}")
    if a.json_out:
        json.dump(dict(url=a.url, health=health, answers=answers, timings=timings, checks=rep.lines), open(a.json_out, "w"), indent=1)
    print(f"selfcheck {'PASSED' if rep.fails == 0 else 'FAILED'}: {sum(1 for x in rep.lines if x[0] == 'PASS')} passed, "
          f"{rep.fails} failed")
    return 0 if rep.fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
