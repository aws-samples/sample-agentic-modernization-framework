---
name: risk_scoring
description: Migration risk scoring skill — deterministically scores an application 0-100 (higher = worse) across Risk/EOSL, Complexity, Team Readiness and Business Readiness during Discovery.
metadata:
  type: skill
  phase: discovery
  agent: discovery
---

# Skill: Migration Risk Scoring

## When this skill fires

- During Discovery, after the Business Readiness and Team Readiness questionnaires have been captured from the user.
- User requests "score this app", "risk score", "how risky is this migration".

## Procedure

### Step 1 — Ensure readiness inputs exist
The two questionnaires are collected in the Discovery UI and stored on the app record under
`business_readiness` and `team_readiness` (a status per question). Unanswered questions are scored as
the **highest** risk, so encourage completion but never block.

### Step 2 — Call the scoring tool (deterministic)
Call `compute_risk_score(app_id)`. **Do not compute the score yourself** — the tool is the single source
of truth (`backend/src/core/risk_scoring.py`, vendored into the documents tool Lambda). It reads the app's
discovery data + readiness answers, persists an `ANALYSIS#risk-score` record, and mirrors `risk_score` /
`risk_tier` onto the app. It returns the full Category/Score/Reason table.

### Step 3 — Surface the result
Present the returned table (Category, Score, Reason) and the overall tier. Do not paraphrase the numbers.

## Scoring rubric (documented; enforced in code)

This section documents `backend/src/core/risk_scoring.py` exactly — the code is the
deterministic implementation (the same file is vendored into the `compute_risk_score`
tool Lambda and the discovery post-hook). If the two ever disagree, the code wins.

**Direction: higher = worse (more risk / less ready). Four categories, each 0–25, summing to 0–100.**

| Category | 0 (best) | mid | 25 (worst) |
|---|---|---|---|
| **Risk (EOSL)** | no EOSL recorded, or EOSL year ≥ 2032 | EOSL year before 2032 → **13** | EOSL date already passed → **25** |
| **Complexity** | sum of the four sub-scores below, capped at 25 | | |
| **Team Readiness** | all 5 questions "Completed" | per-question gradient | all "Not Started" / blank |
| **Business Readiness** | all 5 questions "Completed" | per-question gradient | all "Not Started" / blank |

**Risk (EOSL)** reads `eosl_date` (or `eosl` / `end_of_support`) on the app, else `constraints.eosl_date`.

**Complexity sub-scores** (max 10 + 5 + 5 + 5 = 25):

| Input | Points |
|---|---|
| Dependencies (upstream + downstream) | < 5 → **0** · 5–10 → **5** · > 10 → **10** |
| Repositories | ≤ 1 → **0** · 2–3 → **2** · > 3 → **5** |
| Test types present (unit, QA, functional) | 3 → **0** · 2 → **2** · 1 → **3** · 0 → **5** (`round((3 − present) / 3 × 5)`) |
| Machines | 0–1 → **0** · 2–3 → **2** · 4–9 → **4** · ≥ 10 → **5** |

- Dependencies: an explicit `dependencies_count` / `dependency_count` wins; otherwise the
  lengths of `dependencies_upstream`, `dependencies_downstream` and `dependencies.{upstream,downstream}`
  are added (comma/newline-separated strings are split).
- Repositories: `repo_count` / `repository_count`, else the length of `repos`.
- Test types: boolean-ish flags (`unit_tests`/`has_unit_tests`, `qa_tests`, `functional_tests`, …),
  else keywords in `testing_strategy` / `tests` / `test_coverage` (`unit`; `qa`/`quality`;
  `functional`/`e2e`/`integration`).
- Machines: `machine_count`, else the length of `machine_ids`.

**Readiness status → risk points** (per question, 5 questions × 5 = 25 max):
`Completed 0 · In Progress 2 · Initiated 3 · Pending 4 · Not Started 5` (unknown/blank → 5).

**Overall tier bands:** `0–33 Low · 34–66 Medium · 67–100 High`.

### Questionnaires

**Business Readiness** (`business_readiness.<key>`):
`stakeholder_approval` · `cost_approval` · `dr_signoff` · `compliance_signoff` · `migration_window`

**Team Readiness** (`team_readiness.<key>`):
`team_availability` · `knowledge_transfer` · `aws_skills` · `runbook_ownership` · `tooling_access`

## Output Contract

The `compute_risk_score` tool returns and persists:
```json
{
  "overall": 58, "max": 100, "tier": "Medium",
  "categories": [{"category": "Risk (EOSL)", "score": 25, "max": 25, "reason": "EOSL expired (2020)"}, ...],
  "inputs_complete": false
}
```

## Quality Bar
- The score is deterministic and reproducible — identical inputs always yield identical output.
- Never invent category scores; always call the tool.
- Missing data is scored conservatively (as higher risk), never silently skipped.

## Anti-patterns
- ❌ Computing or estimating the score in prose instead of calling `compute_risk_score`.
- ❌ Treating a missing EOSL as expired (a missing EOSL is scored 0 — assumed supported).
- ❌ Blocking Discovery on an incomplete questionnaire (score it, flag `inputs_complete: false`).
