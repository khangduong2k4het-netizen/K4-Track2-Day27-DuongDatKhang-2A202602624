# Reflection (≤1 page)

**Results:** practice 50.0 (TP 33, FP 0, FN 0) · public 46.9 (TPR 0.974,
FPR 0, 240/220 credits, all pillars `high`) · **private 32.92 (TPR 0.667,
FPR 0.0137, no overage)**. The drop from public to private is the subtle tier:
a third of private faults sat inside the band my thresholds treat as normal,
and two clean events crossed them.

**Which fault types were hardest to catch, and why?**

- **Subtle `data_batch` faults (checks).** Volume spikes, null spikes and
  freshness lags at "obvious" magnitude fall far outside the published 3σ
  band. The subtle ones can stay inside it on every single dimension. I turn
  each metric into a z-score: two-sided metrics (rows, mean amount) get center
  and σ from the baseline bounds, and one-sided ones (null rate, staleness) get
  a running median of clean history as center. I then alert on the Euclidean
  norm of the positive deviations (≥ 3.4) as well as on any single z ≥ 2.6.
  This was the part that needed real statistics rather than `value > max`.
- **Embedding drift near the boundary.** Clean centroid shifts reach ~0.039.
  The published max is 0.0435, and a mild drift sits just above the clean
  range. The gap between the two classes is a few thousandths, so a static
  threshold has almost no margin either way.
- **Feature skew.** Here the published baseline is *wrong in the dangerous
  direction*: clean batches exceed `feature_mean_shift_sigma_max` (0.41).
  Using it as a hard threshold would have produced false positives, so I split
  the gap between the largest clean shift and the smallest skew instead
  (threshold 0.6).
- **Lineage `missing_upstream`.** This one is not statistical, but it is a
  trap. The event payload's `inputs` lists only `raw.orders` even on clean
  runs, so comparing `actual_upstream` to the payload is useless. The
  reference has to be the model's true dependency set (`raw.orders` +
  `raw.customers`), merged with what was observed on clean runs.

I did not tune thresholds to chase the one public miss, and I did not re-run
private after seeing its score. Fitting to that feedback is exactly the overfit
RULES.md warns about.

**What would you change about your cost/coverage tradeoff, if you had another pass?**

I would keep the decision to make one metered call on every event, even over
budget. A missed fault costs ~0.5/n_faulty of the score (≈0.9 pts on private),
and one credit of overage costs 0.2/budget (≈0.09 pts on public). Skipping a
2-credit event to save 0.18 pts while giving up ~0.25 pts of expected TPR is a
bad trade. On public the full scan cost 1.8 pts of overage, and no skip policy
recovers that without losing more in TPR.

What I would change is the *thresholds*, not the coverage. The same
asymmetry means a false positive (≈0.2 pts) is about 4× cheaper than a miss.
My single-metric cut at 2.6σ is still conservative. With more clean history I
would estimate each metric's spread online (median/MAD of events judged clean)
instead of trusting the published 3σ bounds. I would also move thresholds
toward an explicit expected-score optimum (alert when P(fault) > ~0.2), rather
than the "FPR = 0 at all costs" stance the course material suggests.

**What would you improve in the `ctx.state` statistics with more time?**

Right now `ctx.state` keeps a clean-history list per metric, but only for the
center (running median). The spread still comes from the published 3σ bounds.
I would:
1. Estimate spread online too, using a robust MAD of events judged clean. Two
   private FPs suggest the published σ is not the stream's σ for some metrics.
2. Track the joint covariance of the `data_batch` metrics, so the composite
   becomes a Mahalanobis distance instead of independent z's. Correlated
   normal variation (more rows *and* more staleness) would then stop inflating
   the norm, and the threshold could go lower without extra FPs.
3. Keep short per-stream drift accumulators (CUSUM on embedding centroid shift
   and feature shift). A subtle fault that is borderline for one batch would
   become clear against the recent trend.
