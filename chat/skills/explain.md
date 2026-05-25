---
name: Explain DC
slash_command: /explain
button_label: Explain DC
icon: "\U0001F50D"
description: Explain a specific draw call in detail
---

Explain draw call #{api_id} in snapshot {snapshot_id} in detail.
Use get_dc_detail to fetch full information, then explain:
1. What this draw call does (based on shader, textures, vertex count)
2. How expensive it is relative to the frame (clocks vs total)
3. What the render targets suggest about its rendering pass
4. Any potential optimization opportunities
