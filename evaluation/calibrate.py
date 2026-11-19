"""Choose how to turn scores into "possibly damaged" / "damaged" so thresholds transfer between places.

Compares, on DEV (leave-one-city-out models) and then TEST:
  raw    - absolute model probability
  zone_z - robust z-score of the probability within the zone's built-up pixels
           (z = (p - median) / (1.4826 * MAD)), removing per-place offsets
Selection uses DEV only: the method whose per-city false-positive rate stays
closest to the 2% target wins.

    python evaluation/calibrate.py --data DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import experiments as X  # noqa: E402
import radar_eval as ev  # noqa: E402
import radar_damage as rd  # noqa: E402

TARGETS = {"possibly_damaged": 0.02, "damaged": 0.005}


def zone_z(score: np.ndarray, built: np.ndarray) -> np.ndarray:
    return rd.zone_robust_z(score, built)


def per_site_scores(model_for_site, sites):
    out = {}
    for s in sites:
        m = model_for_site(s["meta"]["site"])
        p = X.predict_map(m, s)
        built = s["worldcover"] == rd.BUILT_UP_CLASS
        pos, neg, _ = ev.eval_masks(s)
        out[s["meta"]["site"]] = {"raw": (p[pos], p[neg]), "zone_z": (zone_z(p, built)[pos], zone_z(p, built)[neg])}
    return out


def thresholds_from(scores, method):
    N = np.concatenate([v[method][1] for v in scores.values()])
    return {k: float(np.quantile(N, 1 - fpr)) for k, fpr in TARGETS.items()}


def report(scores, method, thr, label):
    fprs, rows = [], []
    for site, v in scores.items():
        p, n = v[method]
        f2, t2 = float((n >= thr["possibly_damaged"]).mean()), float((p >= thr["possibly_damaged"]).mean())
        f05, t05 = float((n >= thr["damaged"]).mean()), float((p >= thr["damaged"]).mean())
        fprs.append(f2)
        rows.append(f"   {site:16s} possibly: TPR {t2:.2f} FPR {f2:.3f} | damaged: TPR {t05:.2f} FPR {f05:.3f}")
    spread = float(np.mean(np.abs(np.array(fprs) - TARGETS["possibly_damaged"])))
    print(f"{label} [{method}] mean |FPR - 2%| = {spread:.3f}")
    for r in rows:
        print(r)
    return spread


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", default="models/radar_damage_model.joblib")
    args = ap.parse_args()
    import joblib

    dev, test = X.load(Path(args.data))
    rng = np.random.default_rng(0)
    data = {s["meta"]["site"]: X.sample_xy(s, rng) for s in dev}
    loo_models = {}
    for held in data:
        Xs = np.vstack([data[k][0] for k in data if k != held])
        ys = np.concatenate([data[k][1] for k in data if k != held])
        loo_models[held] = X.make_model().fit(Xs, ys)

    dev_scores = per_site_scores(lambda name: loo_models[name], dev)
    final = joblib.load(args.model)
    test_scores = per_site_scores(lambda name: final, test)

    spreads = {}
    for method in ("raw", "zone_z"):
        thr = thresholds_from(dev_scores, method)
        spreads[method] = report(dev_scores, method, thr, "DEV (leave-one-out)")
    best = min(spreads, key=spreads.get)
    thr = thresholds_from(dev_scores, best)
    print(f"\nChosen on DEV: {best}, thresholds {thr}")
    report(test_scores, best, thr, "TEST")

    meta_path = Path(args.model).with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["score_transform"] = best
    meta["thresholds"] = thr
    test_at = {}
    for site, v in test_scores.items():
        p, n = v[best]
        test_at[site] = {
            "tpr_possibly": round(float((p >= thr["possibly_damaged"]).mean()), 3),
            "fpr_possibly": round(float((n >= thr["possibly_damaged"]).mean()), 3),
            "tpr_damaged": round(float((p >= thr["damaged"]).mean()), 3),
            "fpr_damaged": round(float((n >= thr["damaged"]).mean()), 3),
        }
    meta["test_at_thresholds"] = test_at
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("updated", meta_path)


if __name__ == "__main__":
    main()
