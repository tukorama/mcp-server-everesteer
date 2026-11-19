# Everesteer Scoring Rules

This server intentionally excludes `numpy`, `pandas`, and `scipy` so it remains a lightweight, fast control plane. Offline metric computation belongs in your own modeling/Python workspace, using the exact rules below.

## Core Metrics

1. **FIT20**: Covariance of Gaussianized prediction ranks with the mean-centered 20-day target (`target_everest` or dataset `primary_target`). Only prediction rank order matters.
2. **UNQ20**: Covariance of your prediction ranks orthogonalized against the platform benchmark model (`train_benchmark_models`). Rewards signals that are both accurate and differentiated from the crowd/benchmark.
3. **INOV**: Covariance of your prediction ranks orthogonalized against the equal-weight average of core features (treating missing `-1` features as `NaN`). Rewards signal beyond trivial linear feature averages.
4. **Feature Exposure**: Max `|Spearman(prediction, feature_i)|`. Values above `0.30` indicate heavy dependence on a single feature.

## Payout Formulas

Always fetch `weights.score_multiple_constant` (`b`) at runtime via `explain_scoring` rather than hardcoding a constant.

- **Live Tournament Round Score**:
  `score = b * arctan((FIT + UNQ + INOV) / b)`
  `payout = stake * score`
  *(Zero rule applies: on expeds where the benchmark or core-feature average lost money, UNQ or INOV is 0.)*

- **Hackathon / Event Round Score**:
  `S = FIT + 3 * UNQ + INOV - (benchmark_FIT + benchmark_INOV)`
  `score = b * arctan(3 * S / b)`
  *(Zero rule is OFF on event rounds, UNQ carries 3x weight, and the benchmark's own line scores 0.)*
