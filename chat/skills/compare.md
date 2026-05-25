---
name: Compare Snapshots
slash_command: /compare
button_label: Compare
icon: "\U0001F504"
description: Compare two snapshots side by side
---

Compare the snapshots: {snapshot_ids}.
For each snapshot, use get_label_agg to get per-category clock sums, then present:
1. Side-by-side category comparison table (category, clocks_A, clocks_B, delta, delta%)
2. Total clock comparison
3. Which categories improved or regressed
4. Brief conclusion: is the newer capture better or worse overall?
