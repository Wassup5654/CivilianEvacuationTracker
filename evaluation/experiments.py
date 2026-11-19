"""Experiments on cached radar stacks (run evaluation/radar_eval.py fetch first).

    python evaluation/experiments.py --data DIR [--part ablate|ml|all]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import radar_eval as ev  # noqa: E402  (sets up Django)
import radar_damage as rd  # noqa: E402

CURRENT = {"n_base": 20, "n_post": 10, "tracks": 2, "track_combine": "max", "pol": "mean",
           "median": True, "stat": "welch", "radii": (50, 100, 150)}

ABLATIONS = {
    "current": {},
    "all scenes": {"n_base": None, "n_post": None},
    "1 track": {"tracks": 1},
    "all tracks (<=3)": {"tracks": 3},
    "tracks mean": {"track_combine": "mean"},
    "pol max": {"pol": "max"},
    "VV only": {"pol": "vv"},
    "VH only": {"pol": "vh"},
    "no median corr": {"median": False},
    "stat: z vs pre-war std": {"stat": "zpre"},
    "stat: dB difference": {"stat": "diff"},
    "no smoothing": {"radii": ()},
    "smooth 50 m": {"radii": (50,)},
    "smooth 20/50 m": {"radii": (20, 50)},
    "smooth 100/200/300 m": {"radii": (100, 200, 300)},
}


def load(data: Path):
    sites = [ev.load_site(p) for p in sorted((data / "stacks").glob("*.npz"))]
    dev = [s for s in sites if s["meta"]["site"] not in ev.TEST_CITIES]
    test = [s for s in sites if s["meta"]["site"] in ev.TEST_CITIES]
    return dev, test


def table(title, rows):
    print(f"\n{title}")
    print(f"{'config':28s} {'dev AUC':>8s} {'dev TPR@2%':>11s}")
    for name, (a, t) in rows:
        print(f"{name:28s} {a:8.3f} {t:11.3f}")


def ablate(dev, test):
    names = [s["meta"]["site"] for s in dev]
    for mask in ("osm", "wc"):
        rows = []
        for name, change in ABLATIONS.items():
            cfg = {**CURRENT, **change}
            rows.append((name, ev.summary(ev.evaluate(dev, cfg, mask), names)))
        table(f"Ablations on DEV sites, evaluated on {'OSM building footprints' if mask == 'osm' else 'WorldCover built-up'}", rows)


# ── Trained model ──

def features(site: dict) -> np.ndarray:
    """(n_features, h, w) per-pixel features; see radar_damage.pixel_features (kept identical)."""
    return rd.pixel_features(site["tracks"], site["worldcover"] == rd.BUILT_UP_CLASS)


def sample_xy(site, rng, max_per_class=4000, mask="osm"):
    f = features(site)
    pos, neg, _ = ev.eval_masks(site, mask)
    X = f.reshape(f.shape[0], -1).T
    pi, ni = np.flatnonzero(pos.ravel()), np.flatnonzero(neg.ravel())
    pi = rng.choice(pi, min(len(pi), max_per_class), replace=False) if len(pi) else pi
    ni = rng.choice(ni, min(len(ni), max_per_class * 3), replace=False) if len(ni) else ni
    return np.vstack([X[pi], X[ni]]), np.r_[np.ones(len(pi)), np.zeros(len(ni))]


def make_model():
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                          l2_regularization=1.0, min_samples_leaf=100, random_state=0)


def predict_map(model, site):
    f = features(site)
    prob = model.predict_proba(f.reshape(f.shape[0], -1).T)[:, 1].reshape(f.shape[1:])
    return ev.smooth(prob, (50,))


def eval_model(model, sites, mask="osm", collect=None):
    out = {}
    for s in sites:
        f = features(s)
        prob = model.predict_proba(f.reshape(f.shape[0], -1).T)[:, 1].reshape(f.shape[1:])
        prob = ev.smooth(prob, (50,))
        pos, neg, _ = ev.eval_masks(s, mask)
        p, n = prob[pos], prob[neg]
        if collect is not None:
            collect["pos"].append(p)
            collect["neg"].append(n)
        t2, _ = ev.tpr_at_fpr(p, n, 0.02) if len(p) and len(n) else (float("nan"), 0)
        out[s["meta"]["site"]] = {"auc": ev.auc(p, n), "tpr@2%": t2, "pos": int(pos.sum()), "neg": int(neg.sum())}
    return out


def ml(dev, test, best_cfg):
    rng = np.random.default_rng(0)
    data = {s["meta"]["site"]: sample_xy(s, rng) for s in dev}
    # leave-one-city-out on DEV
    loo = {}
    pooled = {"pos": [], "neg": []}
    for held in data:
        X = np.vstack([data[k][0] for k in data if k != held])
        y = np.concatenate([data[k][1] for k in data if k != held])
        m = make_model().fit(X, y)
        loo.update(eval_model(m, [s for s in dev if s["meta"]["site"] == held], collect=pooled))
    names = list(data)
    print("\nTrained model, leave-one-city-out on DEV:", "AUC %.3f  TPR@2%% %.3f" % ev.summary(loo, names))
    for n in names:
        print(f"   {n:16s} AUC {loo[n]['auc']:.3f}  TPR@2% {loo[n]['tpr@2%']:.3f}")

    final = make_model().fit(np.vstack([d[0] for d in data.values()]), np.concatenate([d[1] for d in data.values()]))
    P, N = np.concatenate(pooled["pos"]), np.concatenate(pooled["neg"])
    thresholds = {"possibly_damaged": float(np.quantile(N, 0.98)), "damaged": float(np.quantile(N, 0.995))}
    print("Calibrated on DEV leave-one-out: possibly >= %.3f (TPR %.2f at FPR 2%%), damaged >= %.3f (TPR %.2f at FPR 0.5%%)" % (
        thresholds["possibly_damaged"], (P >= thresholds["possibly_damaged"]).mean(),
        thresholds["damaged"], (P >= thresholds["damaged"]).mean()))
    return final, loo, thresholds


def report_test(test, best_cfg, model):
    names = [s["meta"]["site"] for s in test]
    cur = ev.evaluate(test, CURRENT)
    best = ev.evaluate(test, best_cfg)
    mod = eval_model(model, test) if model is not None else {}
    print("\nHELD-OUT TEST sites (never used for choices or training)")
    print(f"{'site':16s} {'current':>16s} {'tuned stats':>16s} {'trained model':>16s}   pos px")
    for n in names:
        row = [cur.get(n), best.get(n), mod.get(n)]
        cells = [f"{r['auc']:.3f}/{r['tpr@2%']:.2f}" if r else "-" for r in row]
        print(f"{n:16s} {cells[0]:>16s} {cells[1]:>16s} {cells[2]:>16s}   {cur[n]['pos']}")
    for label, res in (("current", cur), ("tuned stats", best), ("trained model", mod)):
        if res:
            ukr = [n for n in names if n != "El Fasher"]
            print(f"   {label:14s} mean AUC (Ukraine test) {ev.summary(res, ukr)[0]:.3f}  "
                  f"TPR@2% {ev.summary(res, ukr)[1]:.3f} | El Fasher AUC {res.get('El Fasher', {}).get('auc', float('nan')):.3f}")
    return cur, best, mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--part", default="all", choices=["ablate", "ml", "all"])
    ap.add_argument("--best", default="{}", help="JSON overrides for the tuned config")
    ap.add_argument("--save-model", default="")
    args = ap.parse_args()
    dev, test = load(Path(args.data))
    print("DEV:", [s["meta"]["site"] for s in dev], "| TEST:", [s["meta"]["site"] for s in test])
    if args.part in ("ablate", "all"):
        ablate(dev, test)
    if args.part in ("ml", "all"):
        best_cfg = {**CURRENT, **json.loads(args.best)}
        model, loo, thresholds = ml(dev, test, best_cfg)
        cur, best, mod = report_test(test, best_cfg, model)
        # tuned-statistic fallback thresholds, calibrated on DEV the same way
        negs = []
        for s_ in dev:
            sm_ = ev.score(s_, best_cfg)
            _, neg, _ = ev.eval_masks(s_)
            negs.append(sm_[neg])
        N = np.concatenate(negs)
        stat_thr = {"possibly_damaged": float(np.quantile(N, 0.98)), "damaged": float(np.quantile(N, 0.995))}
        test_hits = {}
        for s_ in test:
            prob = predict_map(model, s_)
            pos, neg, _ = ev.eval_masks(s_)
            test_hits[s_["meta"]["site"]] = {
                "tpr_possibly": float((prob[pos] >= thresholds["possibly_damaged"]).mean()),
                "fpr_possibly": float((prob[neg] >= thresholds["possibly_damaged"]).mean()),
                "tpr_damaged": float((prob[pos] >= thresholds["damaged"]).mean()),
                "fpr_damaged": float((prob[neg] >= thresholds["damaged"]).mean()),
            }
        print()
        print("TEST at the DEV-calibrated thresholds:")
        for k, v in test_hits.items():
            print(f"   {k:16s} possibly: TPR {v['tpr_possibly']:.2f} FPR {v['fpr_possibly']:.3f} | "
                  f"damaged: TPR {v['tpr_damaged']:.2f} FPR {v['fpr_damaged']:.3f}")
        if args.save_model:
            import joblib
            import sklearn
            joblib.dump(model, args.save_model)
            meta = {
                "features": rd.FEATURE_NAMES,
                "thresholds": thresholds,
                "fallback_statistic": {k: v for k, v in best_cfg.items() if k in ("stat", "tracks", "pol", "median")},
                "fallback_thresholds": stat_thr,
                "smooth_m": 50,
                "sklearn_version": sklearn.__version__,
                "trained_on": [s_["meta"]["site"] for s_ in dev],
                "dev_leave_one_out_auc": {k: round(v["auc"], 3) for k, v in loo.items()},
                "test_auc": {k: round(v["auc"], 3) for k, v in mod.items()},
                "test_auc_mean": round(ev.summary(mod, list(mod))[0], 3),
                "test_auc_mean_tuned_statistic": round(ev.summary(best, list(best))[0], 3),
                "test_auc_mean_previous_method": round(ev.summary(cur, list(cur))[0], 3),
                "test_at_thresholds": test_hits,
            }
            Path(args.save_model).with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            print("saved model + metadata to", args.save_model)


if __name__ == "__main__":
    main()
