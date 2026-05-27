---
name: index-sync
tools: [search, read/readFile, edit/createFile, edit/editFiles]
description: Automatically maintain and synchronize the repository code index by resolving modules, detecting existing entries, and applying safe updates.
---

# Index Sync Workflow

This workflow maintains the repository code index system in a **continuous and safe state**.

It extends index-workflow by adding:

- existing module detection
- automatic refresh / creation decisions
- root index synchronization
- boundary drift awareness
- safety constraints (NO rename / NO merge / NO split)

---

## Goal

Given any short input:

- log
- symptom
- callstack
- path hint
- feature name

The workflow should:

1. resolve the most likely module
2. detect whether the module already exists
3. decide whether to:
   - create
   - refresh
   - update root
   - stop for confirmation
4. apply minimal safe updates
5. keep index system consistent

---

## Input Format

Preferred:

`index-sync <target> | key=value | key=value`

Examples:

- `index-sync vkCreateInstance failed | platform=android`
- `index-sync shader cook | paths=tools/pipeline/core`
- `index-sync RenderGraph Execute`

Mode is inferred automatically:

- runtime evidence → locate
- source hint → discover

---

## Core Workflow

### Step 1 — Resolve Module

Resolve:

- ModuleKey
- SourceScope
- confidence
- anchors

---

### Step 2 — Detect Existing Index

Check:

- `docs/index/INDEX.md` → router entry
- `docs/index/modules/` → module file
- similarity with existing modules

Determine:

- existing module match
- missing module
- partial match
- multiple candidates

---

### Step 3 — Decision Engine

Choose ONE action:

#### Case A — New Module

Condition:
- no matching module
- high confidence

Action:
- create module index
- update root index

---

#### Case B — Existing Module

Condition:
- strong match
- scope consistent

Action:
- refresh module index
- update root if needed

---

#### Case C — Boundary Drift

Condition:
- existing module match
- but new evidence extends scope

Action:
- refresh module
- flag drift

---

#### Case D — Ambiguous

Condition:
- multiple candidates
- low confidence

Action:
- STOP
- return candidates
- request confirmation

---

## 🚨 Safety Rules (CRITICAL)

You MUST NOT automatically:

- rename ModuleKey
- merge modules
- split modules

If such action is required:

- STOP
- output suggestion
- require explicit confirmation

---

## Step 4 — Apply Updates

Perform minimal required updates:

- create or update:
  `docs/index/modules/<ModuleKey>.md`

- update:
  `docs/index/INDEX.md`

Do NOT modify unrelated modules.

---

## Step 5 — Boundary Drift Detection

Detect drift if:

- new anchors outside SourceScope
- new directories involved
- new entrypoints outside previous module
- topic mismatch

Output:

Boundary Drift: yes / no

---

## Step 6 — Output Summary

Return:

[Mode]
sync

[Resolved Module]
- ModuleKey:
- Confidence:
- SourceScope:

[Existing Index]
- module exists:
- root entry exists:
- matched module:

[Action]
- create / refresh / root-update / confirm-needed

[Files Updated]
- docs/index/modules/...
- /INDEX.md

[Boundary Drift]
- yes / no
- note:

[Next Action]
- none / confirm / review scope

---

## Constraints

- do not scan entire repo unnecessarily
- stop once module boundary is clear
- prefer precision over coverage
- avoid duplicate module creation
- reuse existing module when reasonable

---

## Philosophy

Index Sync ensures:

- index reflects real code state
- no redundant modules
- no silent drift
- safe evolution of index system

This is NOT just indexing.

This is **index lifecycle management**.
