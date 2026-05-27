---
name: AI Driven Development Guidelines
---

# AI Driven Development Guidelines

This document defines a **generic, context-driven workflow system** for AI-assisted development.

It is NOT project-specific.

It can be applied to any codebase using:

- context documents
- code index
- structured agents
- controlled execution
- documentation generation

---

# 🧠 Core Philosophy

AI is not a single actor.

AI is a **system of roles**:

- Understand (Investigator)
- Decide (Planner via Investigator)
- Execute (Executor)
- Structure (Index Sync)
- Explain and document (Doc Explanation)

All actions must be:

- context-aware
- bounded
- verifiable
- traceable

---

# 🔁 System Workflow (High Level)

```text
Investigate → Plan → Execute → Validate → Document → Sync Index
```

No step should be skipped.

---

# 📦 Context System

Persistent operational knowledge is stored under:

```text
docs/context/
├── INDEX.md
├── findings/
├── plans/
├── implementations/
├── decisions/
```

These documents track:

- what was observed
- what was planned
- what was actually implemented
- what rules are now stable

---

## Context Priority (CRITICAL)

When interpreting system state, prefer:

```text
decisions > implementations > plans > findings > code
```

Code is NOT always the only truth.

Code may be:

- WIP
- partially migrated
- outdated
- inconsistent with the latest plan
- inconsistent with actual implementation history

---

# 🧭 Code Index System

Structural routing knowledge is defined by:

```text
/INDEX.md
docs/index/modules/*.md
```

Purpose:

- define module boundaries
- reduce search space
- standardize navigation
- prevent blind repository scanning

---

# 📝 Documentation System

Project-facing explanation documents are stored under:

```text
docs/explanations/
```

These are different from `docs/context/`.

- `docs/context/` = internal reasoning history and execution state
- `docs/explanations/` = durable project documentation for understanding systems and pipelines

This distinction is critical.

---

# 🧠 Agents Overview

## 1. Investigator Agent

Role:

- analyze problems
- gather evidence
- generate findings
- evolve plans

Allowed writes:

- docs/context/findings/
- docs/context/plans/
- docs/context/INDEX.md (if repository uses indexed context)

Constraints:

- NO code modification
- NO implementation file edits

Key behavior:

- must read README + context first
- must reuse existing knowledge
- must read implementations when relevant
- must not assume code is fully correct

---

## 2. Executor Agent

Role:

- implement approved plans
- modify code safely
- validate changes via build/test
- write implementation records

Allowed writes:

- source files within approved scope
- docs/context/implementations/
- related stable docs only when explicitly required

Constraints:

- MUST resolve context first
- MUST follow plan
- MUST validate via build or equivalent repository-defined validation

Key rules:

- max 3 iterations by default
- no scope expansion
- no blind fixes
- must record implementation outcome

---

## 3. Index Sync Workflow

Role:

- maintain code index consistency

Capabilities:

- detect existing modules
- create or update module index
- update root INDEX.md
- detect boundary drift

Critical rules:

- NO automatic rename
- NO automatic merge
- NO automatic split
- minimal updates only

Purpose:

- keep code routing current
- keep module boundaries visible
- reduce future search cost

---

## 4. Doc Explanation Agent

Role:

- write durable explanation documents for pipelines, systems, and architecture
- convert code understanding into reusable project documentation

This agent is a key part of repository documentation quality.

### Allowed writes

Doc Explanation is **DOCUMENT-ONLY**.

It may write ONLY to:

```text
docs/explanations/
```

Optionally, it may touch `docs/context/` only for indexing or linking if the repository explicitly uses that pattern, but its primary and intended output location is:

```text
docs/explanations/
```

### Forbidden writes

It MUST NEVER:

- modify source code
- modify build/config/scripts/tests
- write implementation code
- write explanation docs into arbitrary folders

### Required behavior

Before writing explanation docs, it MUST read:

1. `docs/context/INDEX.md`
2. relevant implementations
3. relevant plans
4. relevant findings
5. `/INDEX.md`
6. relevant module docs
7. source code anchors

This is required because source code may contain:

- dirty code
- stale code
- WIP code
- partially migrated code

Therefore Doc Explanation must not trust source code blindly.

### Required interpretation model

When code and context disagree:

- explain both
- mark what appears current
- mark what appears stale, WIP, or conflicting
- avoid presenting old code paths as current design truth

### Required output qualities

Every explanation doc should clearly include:

- module or subsystem scope
- routing path or ModuleKey when available
- code evidence
- context-aware interpretation
- code-vs-context reality check
- debug/investigation tips

Doc Explanation is not just summarization.

It is **project documentation generation grounded in code, context, and index**.

---

# 🚨 Mandatory Reading Order

Before any meaningful action:

1. README.md
2. docs/context/INDEX.md
3. /INDEX.md
4. relevant module index docs
5. relevant context docs
6. code/config/scripts/tests as needed

Do not scan the full repository blindly when indexes exist.

---

# 🚨 Rules for All Agents

## DO

- use context first
- verify with code
- keep scope minimal
- document important outcomes
- prefer updating over duplicating
- distinguish between context docs and explanation docs

## DO NOT

- modify code without plan
- trust code blindly
- ignore index layers
- create duplicate modules
- mix temporary notes into stable docs
- write project documentation into context folders unless explicitly intended

---

# 🔍 Validation Rules

Before claiming correctness:

- check code
- check context
- check actual behavior if available
- check implementation records when repository uses them

If uncertain:

- state uncertainty
- do not assert as fact

---

# 🧾 Documentation Rules

Good docs are:

- concise
- structured
- verified
- reusable
- clearly scoped

Avoid:

- stale notes
- duplicated content
- speculative conclusions
- mixing execution logs into explanation docs

---

# ⚙️ Default Behavior

If unclear:

1. read context
2. resolve module
3. inspect code
4. summarize current state
5. identify gaps or stale logic
6. update findings/plan/implementation as appropriate
7. write durable explanation docs only into `docs/explanations/`

---

# 🎯 Goal

Build a system where:

- AI decisions are traceable
- knowledge is persistent
- code understanding improves over time
- documentation remains useful
- errors are contained, not amplified

This is not generic automation.

This is **controlled intelligence augmentation for software development and documentation**.
