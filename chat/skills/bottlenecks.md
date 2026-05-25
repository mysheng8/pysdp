---
name: Top Bottlenecks
slash_command: /bottlenecks
button_label: Bottlenecks
icon: "\U0001F525"
description: Find the most expensive draw calls and explain why
---

Find the top 10 most expensive draw calls in snapshot {snapshot_id}.
For each one, explain:
1. Why it's expensive (which metrics are high)
2. What category it belongs to
3. Potential optimization suggestions

Use get_draw_calls first to find the top DCs by clocks, then use get_dc_detail for the top 3 to examine their shaders and textures.
