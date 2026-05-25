---
name: Metric Correlations
slash_command: /correlate
button_label: Correlate
icon: "\U0001F4C8"
description: Find which metrics best explain GPU clock time
---

Analyze metric correlations for snapshot {snapshot_id}.
Use get_clock_correlation to find which metrics have the highest R² with GPU clocks.
Present:
1. Top 5 correlated metrics (metric name, R², interpretation)
2. What this means for optimization (e.g. "fragment-bound" if fragment metrics dominate)
3. Any surprising low correlations that might indicate a mixed workload
