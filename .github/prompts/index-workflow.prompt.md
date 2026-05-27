---
name: index-workflow
tools: [search, edit/createFile, edit/editFiles]
description: Run the full code index workflow: locate or discover a module, build or refresh its module index, and update the repository root index.
---

# Index Workflow

Use this workflow to turn a short module hint, symptom, log snippet, path hint, or feature clue into updated code index artifacts.

This workflow is built for the repository code index system:

- root code index: `/INDEX.md`
- module indexes: `docs/index/modules/*.md`

---

## Goal

Given a short target such as:

- a symptom
- a log keyword
- a callstack symbol
- an assert location
- a path hint
- a feature or subsystem name

the workflow should:

1. identify the most likely module
2. build or refresh the module index under `docs/index/modules/`
3. update the repository root code index at `/INDEX.md`

---

## Preferred Input Format

Use this compact form when possible:

`index-workflow <mode> <target> | key=value | key=value`

Supported modes:

- `locate`
- `discover`
- `refresh`
- `build-only`
- `root-only`

Examples:

- `index-workflow locate vkCreateInstance failed | platform=android | paths=Engine/Render,Vulkan`
- `index-workflow discover shader cook | paths=tools/pipeline/core | logs=asset job complete bundles`
- `index-workflow refresh Render.Pipeline.Graph`
- `index-workflow build-only Asset.Cook.Texture | scope=Engine/Asset/Cook/Texture`
- `index-workflow root-only Render.Pipeline.Graph | index=docs/index/modules/Render.Pipeline.Graph.md`

If the user provides freeform input instead of the compact form:

- infer the most likely mode
- normalize it internally
- only ask for clarification if ambiguity materially blocks progress

---

## Mode Semantics

### locate

Use when the user has runtime evidence or symptoms, such as:

- logs
- callstacks
- assert locations
- crash keywords
- platform-specific failure descriptions

Workflow:

1. locate the most likely module
2. if confidence is sufficient, build or refresh the module index
3. update `/INDEX.md`

---

### discover

Use when the user has source or subsystem clues, such as:

- folder path
- subsystem name
- feature name
- entry symbol
- common logs

Workflow:

1. discover the module boundary
2. generate or confirm the ModuleKey
3. build module index
4. update `/INDEX.md`

---

### refresh

Use when a module index already exists and should be updated.

Workflow:

1. read the existing module index
2. re-scan module scope
3. rebuild the module index
4. refresh the corresponding row in `/INDEX.md` if needed

---

### build-only

Use when the ModuleKey and SourceScope are already known and only the module index should be generated or refreshed.

Workflow:

1. build or refresh the module index only

---

### root-only

Use when the module index already exists and only the repository root index should be updated.

Workflow:

1. update `/INDEX.md` only

---

## Workflow Steps

### Step 1 — Normalize Input

Convert the request into an internal normalized task object with:

- mode
- target
- key hints
- optional filters such as:
  - paths
  - symbols
  - logs
  - stack
  - assert
  - platform
  - scope
  - index

---

### Step 2 — Resolve Module

Depending on mode:

- `locate` → resolve the most likely module from runtime evidence
- `discover` → identify the module boundary and ModuleKey
- `refresh` → recover the module from existing module index and source scope
- `build-only` → use provided ModuleKey and SourceScope directly
- `root-only` → use provided ModuleKey and module index path directly

At the end of this step, the workflow should have as many of these as possible:

- ModuleKey
- SourceScope
- confidence
- entry symbols
- anchor symbols
- likely module index path

---

### Step 3 — Build or Refresh Module Index

When required by mode:

- create or update `docs/index/modules/<ModuleKey>.md`
- include strong anchors
- include entry points
- include key classes
- include key methods
- include log-to-code mapping when relevant

Do not overscan the entire repository once module boundary is clear.

---

### Step 4 — Update Root Index

When required by mode:

- update `/INDEX.md`
- add or refresh the router row
- keep the root index concise
- avoid rewriting unrelated rows

---

### Step 5 — Return Workflow Summary

Always summarize:

- resolved mode
- ModuleKey
- confidence
- SourceScope
- module index path
- whether `/INDEX.md` was updated
- next recommended action

---

## Confidence and Confirmation

If `locate` or `discover` produces low confidence:

- stop before build/update
- present the best candidate plus alternatives
- ask for confirmation only if uncertainty materially affects correctness

If confidence is sufficient:

- continue automatically

---

## Stop Conditions

Stop once all relevant goals for the chosen mode are complete.

You may stop early if:

- module boundary is still unclear after reasonable search
- multiple module candidates remain equally likely
- required inputs for build-only or root-only are missing
- the repository lacks enough evidence to ground a stable ModuleKey

In those cases:

- return the best partial result
- specify the minimum missing input

---

## Constraints

- do not overscan the entire repo
- stop once module boundary is clear
- every anchor symbol must include file + line when possible
- prefer navigational intelligence over prose
- do not write explanations, tutorials, or speculative essays
- do not rewrite unrelated module indexes or unrelated root index rows

---

## Output Requirements

Always return these fields in the final response:

- `Mode`
- `ModuleKey`
- `Confidence`
- `SourceScope`
- `Module Index`
- `Root Index Updated`
- `Next Action`

If there are alternatives, include up to 2 alternatives.

If a file was updated, include the target path.

---

## Notes

This workflow is intended to orchestrate module indexing tasks.

It should reuse the repository's indexing conventions:

- `/INDEX.md` as root routing index
- `docs/index/modules/*.md` as authoritative per-module routing docs

Use existing module docs when present.
Prefer refresh over duplicate creation.
