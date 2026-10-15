"""
Your defense. Implement register(ctx) and a handler per event type.
See ../README.md for the full interface + toolkit reference, and
../RULES.md before you start.

Strategy
--------
* Exactly one metered call per event, always. A missed fault costs
  ~0.5/n_faulty of the score while one credit over budget costs only
  0.2/budget, so skipping events to stay under budget loses more TPR than it
  saves in overage (public: full scan ~240 vs 220 budget -> ~-1.8 pts).
* Every metric is turned into a z-score. ctx.baseline publishes mean +/- 3 sigma
  bounds: two-sided metrics give center and sigma directly; for one-sided
  metrics (only a max is published) the center is the running median of
  values from events we judged clean, so sigma = (max - center) / 3.
* Alert when any single z clears Z_SINGLE (slightly under 3 sigma: a missed
  fault is ~4x more expensive than a false alarm), or when several moderate
  deviations together clear Z_COMPOSITE (subtle multi-dimension faults).
* Hard structural checks (contract violations, missing upstream, orphan
  output) alert unconditionally.
"""
import math

from api import Verdict

Z_SINGLE = 2.6
Z_COMPOSITE = 3.4
MIN_HISTORY = 8

# Clean-stream centers for the one-sided metrics, used until enough clean
# history has accumulated in ctx.state to take a running median instead.
PRIOR_CENTER = {
    "staleness_min": 4.2,
    "freshness_delay_min": 5.0,
    "duration_ms": 4100.0,
    "null_rate": 0.0045,
    "std_amount": 14.9,
    "centroid_shift": 0.02,
    "avg_doc_age_days": 26.0,
}

# feature_drift's mean_shift_sigma is a |shift| (half-normal on clean data)
# and the published 0.41 max is exceeded by clean batches; real skew sits
# well above ~0.9, so split the gap.
FEATURE_SHIFT_THRESHOLD = 0.6

# dbt:stg_orders enriches orders with customers; dropping either input breaks
# the model even though the run still "succeeds".
EXPECTED_UPSTREAM = {"dbt:stg_orders": {"raw.orders", "raw.customers"}}


def register(ctx):
    ctx.on("data_batch", check_data_batch)
    ctx.on("contract_checkpoint", check_contract_checkpoint)
    ctx.on("lineage_run", check_lineage_run)
    ctx.on("feature_materialization", check_feature_materialization)
    ctx.on("embedding_batch", check_embedding_batch)


# ---------------------------------------------------------------- helpers
# (statistics is on the import allowlist but itself imports `numbers`,
# which isn't, so these two are hand-rolled.)

def _median(xs):
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _pstdev(xs):
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def _center(ctx, metric):
    hist = ctx.state.get("clean", {}).get(metric, [])
    if len(hist) >= MIN_HISTORY:
        return _median(hist)
    return PRIOR_CENTER[metric]


def _remember_clean(ctx, values):
    clean = ctx.state.setdefault("clean", {})
    for metric, value in values.items():
        clean.setdefault(metric, []).append(value)


def _z_upper(ctx, metric, value, upper):
    """z-score of value for a metric where only mean + 3 sigma is published."""
    center = _center(ctx, metric)
    sigma = max((upper - center) / 3.0, 1e-9)
    return (value - center) / sigma


def _z_two_sided(value, lo, hi):
    center = (lo + hi) / 2.0
    sigma = (hi - lo) / 6.0
    return (value - center) / sigma


def _judge(pillar, zs, extra_reasons=()):
    """zs: {name: z}. Positive z = deviation in the faulty direction."""
    reasons = list(extra_reasons)
    worst = max(zs, key=lambda k: zs[k]) if zs else None
    if worst is not None and zs[worst] >= Z_SINGLE:
        reasons.append(f"{worst} z={zs[worst]:.2f}")
    composite = math.sqrt(sum(max(z, 0.0) ** 2 for z in zs.values()))
    if not reasons and composite >= Z_COMPOSITE:
        reasons.append(f"composite z={composite:.2f} ({', '.join(f'{k}={v:.1f}' for k, v in zs.items())})")
    if reasons:
        conf = min(1.0, max(composite, 3.0) / 6.0)
        return Verdict(alert=True, pillar=pillar, confidence=conf, reason="; ".join(reasons))
    return Verdict(alert=False, pillar=pillar, confidence=1.0 - composite / Z_COMPOSITE / 2)


def _error_verdict(pillar, result):
    return Verdict(alert=False, pillar=pillar, confidence=0.0, reason=f"tool error: {result.get('error')}")


# ---------------------------------------------------------------- handlers

def check_data_batch(payload, ctx):
    b = ctx.baseline
    r = ctx.tools.batch_profile(payload["batch_id"])
    if "error" in r:
        return _error_verdict("checks", r)

    null_rate = (r.get("null_rate") or {}).get("customer_id", 0.0)
    values = {
        "null_rate": null_rate,
        "staleness_min": r["staleness_min"],
        "std_amount": r["std_amount"],
    }
    zs = {
        "row_count": abs(_z_two_sided(r["row_count"], b["row_count_min"], b["row_count_max"])),
        "mean_amount": abs(_z_two_sided(r["mean_amount"], b["mean_amount_min"], b["mean_amount_max"])),
        "null_rate": _z_upper(ctx, "null_rate", null_rate, b["null_rate_max"]),
        "staleness_min": _z_upper(ctx, "staleness_min", r["staleness_min"], b["staleness_min_max"]),
    }
    # std_amount has no published bound: only use it once we have our own
    # clean history to estimate its spread from.
    std_hist = ctx.state.get("clean", {}).get("std_amount", [])
    if len(std_hist) >= MIN_HISTORY:
        spread = _pstdev(std_hist) or 1e-9
        zs["std_amount"] = abs(r["std_amount"] - _median(std_hist)) / spread

    verdict = _judge("checks", zs)
    if not verdict.alert:
        _remember_clean(ctx, values)
    return verdict


def check_contract_checkpoint(payload, ctx):
    b = ctx.baseline
    r = ctx.tools.contract_diff(payload["contract_id"], payload["checkpoint_batch_id"])
    if "error" in r:
        return _error_verdict("contracts", r)

    reasons = [f"violation: {v}" for v in r.get("violations") or []]
    delay = r["freshness_delay_min"]
    declared = (payload.get("declared_sla") or {}).get("freshness_min")
    if declared is not None and delay > declared:
        reasons.append(f"freshness {delay:.1f}min > declared SLA {declared}min")

    zs = {"freshness_delay_min": _z_upper(ctx, "freshness_delay_min", delay, b["freshness_delay_max_min"])}
    verdict = _judge("contracts", zs, reasons)
    if not verdict.alert:
        _remember_clean(ctx, {"freshness_delay_min": delay})
    return verdict


def check_lineage_run(payload, ctx):
    b = ctx.baseline
    r = ctx.tools.lineage_graph_slice(payload["run_id"])
    if "error" in r:
        return _error_verdict("lineage", r)

    job = payload.get("job", "")
    actual = set(r.get("actual_upstream") or [])
    learned = ctx.state.setdefault("upstream", {})
    expected = EXPECTED_UPSTREAM.get(job, set()) | learned.get(job, set())

    reasons = []
    missing = expected - actual
    if missing:
        reasons.append(f"missing upstream: {sorted(missing)}")
    if r.get("actual_downstream_count", 1) == 0:
        reasons.append("orphan output: no downstream consumers")

    zs = {"duration_ms": _z_upper(ctx, "duration_ms", r["duration_ms"], b["lineage_duration_ms_max"])}
    verdict = _judge("lineage", zs, reasons)
    if not verdict.alert:
        learned[job] = learned.get(job, set()) | actual
        _remember_clean(ctx, {"duration_ms": r["duration_ms"]})
    return verdict


def check_feature_materialization(payload, ctx):
    r = ctx.tools.feature_drift(payload["feature_view"], payload["batch_id"])
    if "error" in r:
        return _error_verdict("ai_infra", r)

    shift = r["mean_shift_sigma"]
    if shift >= FEATURE_SHIFT_THRESHOLD:
        return Verdict(alert=True, pillar="ai_infra", confidence=min(1.0, shift / 1.0),
                       reason=f"training-serving skew: mean shift {shift:.2f} sigma")
    return Verdict(alert=False, pillar="ai_infra")


def check_embedding_batch(payload, ctx):
    b = ctx.baseline
    r = ctx.tools.embedding_drift(payload["corpus"], payload["chunk_batch_id"])
    if "error" in r:
        return _error_verdict("ai_infra", r)

    values = {"centroid_shift": r["centroid_shift"], "avg_doc_age_days": r["avg_doc_age_days"]}
    zs = {
        "centroid_shift": _z_upper(ctx, "centroid_shift", r["centroid_shift"], b["embedding_centroid_shift_max"]),
        "avg_doc_age_days": _z_upper(ctx, "avg_doc_age_days", r["avg_doc_age_days"], b["corpus_avg_doc_age_days_max"]),
    }
    verdict = _judge("ai_infra", zs)
    if not verdict.alert:
        _remember_clean(ctx, values)
    return verdict
