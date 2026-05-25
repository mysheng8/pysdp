---
name: Category Breakdown
slash_command: /breakdown
button_label: Breakdown
icon: "\U0001F4CA"
description: Show GPU clock distribution by draw call category
---

Show the category breakdown for snapshot {snapshot_id}.
Use get_label_agg to get clock sums per category, then present:
1. A ranked table (category, clocks, percentage of total, DC count)
2. Brief insight: which category dominates and by how much
