# Campaign Agent --- Routine Session Ingestion Milestones

**Roadmap status:** Active\
**Primary objective:** Reach repeatable **≥85% accurate ingestion of
previously unseen session notes** into the Obsidian campaign vault,
including relevant information, correct note placement, and expected
linkages, with a short human approval step and no session-specific code
changes.

## Current Baseline

As of V4.15.1:

-   Campaign deterministic regression: **30 passed / 0 failed / 1
    skipped**
-   Campaign full semantic/planner regression: **41 passed / 0 failed /
    0 skipped**
-   Repair/development pipeline regression: **39 passed / 0 failed**
-   Development pipeline: **PASS**
-   Session 37 is the primary known semantic regression fixture.
-   Repair tooling remains human-gated; no automatic trusted-agent
    promotion.
-   Vault-integrity hardening is intentionally deferred while ingestion
    generalization is evaluated.

------------------------------------------------------------------------

## Success Definition

For a previously unseen session, routine ingestion should achieve:

-   **Relevant information recall:** ≥85%
-   **Expected relationship/linkage recall:** ≥85%
-   **Correct entity/note placement:** ≥90%
-   **Unsupported material:** ≤5%
-   **Critical canon errors:** 0
-   No session-specific code changes should be required merely to make
    the tested session pass.

These thresholds may be refined after Sessions 37 and 38 are formally
scored.

A passing ingestion should identify relevant campaign entities and
facts, resolve them to the correct existing or new notes, create useful
Obsidian relationships, distinguish operations such as CREATE / UPDATE /
RELATED / REVIEW appropriately, and avoid unsupported campaign canon.

------------------------------------------------------------------------

## M1 --- Session 37 End-to-End Acceptance

**Goal:** Prove that the already-tested Session 37 analysis can be
converted into a correct physical vault state.

### Plan

-   Create a disposable copy of the campaign vault.
-   Ingest Session 37 using the current trusted Campaign Agent.
-   Review proposed CREATE / UPDATE / RELATED / REVIEW decisions.
-   Approve the intended operations.
-   Audit the resulting notes and links against Session 37 and the
    existing Session 37 regression fixture.
-   Confirm rollback/transaction behavior remains available.

### Exit Criteria

-   Known Session 37 expectations are represented correctly in the
    disposable vault.
-   Expected notes receive the correct information.
-   Expected relationships/linkages are present.
-   Known forbidden/unsupported writes are absent.
-   No material corruption or unintended unrelated changes occur.

**Status:** ☐ Not started ☐ In progress ☑ Complete (2026-10-08)

**Findings / notes:**

Accepted run: `20261008_214712_116253` on a fresh copy of the trusted
vault (`Icemoor_Obsidian_Vault_E2E`), agent V4.16.1. Approval policy:
all session history; semantic updates only when `source-grounded`;
session YAML + body wikilinks; creations judged per candidate.

-   All fixture expectations hold in the physical vault: 10 expected
    matches linked, 6 TODOs, `Missing Sanitation Workers` created via
    REVIEW, no forbidden creates, Two Tits / Daggins' Order and Skeletal
    Hand / Blond-Haired Youth open questions written, Furgus tattoo kept
    RELATED-only, no Cornhusk Dolls / Temple update.
-   Body wikilinks are alias-aware (`[[Talir Rengavi|talir]]`,
    `[[Theo'din Corvis|Theo]]`, `[[Ester|Esther]]`).
-   Rollback verified twice: `--rollback` restored every note
    byte-for-byte and deleted created files.
-   Defects found and fixed generally during M1 (V4.16 / V4.16.1):
    1.  The SAFE CHANGE PLAN did not apply the candidate quality gate, so
        gate-blocked candidates (e.g. a descriptive prop item) appeared
        in CREATE NEW; once the plan became the creation source this
        would let `--auto` create them. The plan now moves gate-blocked
        rows to IGNORE.
    2.  `main()` still used the pre-V4.16 creation selector, so
        planner-only REVIEW rows (campaign-state quest promotions) were
        displayed but never offered. Creation is now plan-driven.
    3.  Created notes kept template placeholder wikilinks (`[[NPC Name]]`,
        `[[Session XX]]`), creating phantom graph nodes; they are now
        unlinked and the source session is filled in (`first_seen`,
        Session History).
    4.  Body-only edits re-serialized YAML in every touched note (quote
        and indent churn). Frontmatter is now preserved byte-for-byte.
-   Observations to carry into M2/M3 (not fixed):
    -   qwen3:1.7b extraction is not deterministic run to run: the same
        session proposed `The Temple of the Raven Queen`, `The Shrine of
        the Raven Queen`, or nothing; `The Black Feathers` and
        `Wellview Helm` appeared only in some runs. The rubric should
        score a single recorded run, and M3 may want 2--3 runs.
    -   Near-duplicate semantic proposals can share identical evidence
        (two Deanira sailor/map clues).
    -   Several quality-gate and canonicalization rules are literal
        Session 37 strings (`phallic pin`, `goliath sailor`,
        `missing sanitation workers`). They are the most likely M3
        generalization gaps.
    -   Created notes are structurally clean but have no content beyond
        the template; populating them stays a human/approved step.

------------------------------------------------------------------------

## M2 --- Formalize the Ingestion Accuracy Rubric

**Goal:** Make "85% accurate" measurable and repeatable.

### Scoring Categories

1.  **Entity discovery** --- important people, places, organizations,
    quests, items, mysteries, etc.
2.  **Fact extraction** --- campaign-relevant information from the
    session.
3.  **Entity resolution / placement** --- information attached to the
    correct existing or newly created note.
4.  **Relationships / linkages** --- expected Obsidian relationships are
    created.
5.  **Precision / safety** --- unsupported facts, entities,
    relationships, and canon are avoided.

### Safety Gate

High recall must not compensate for dangerous semantic errors. A session
fails acceptance if it introduces a critical canon error even if its
aggregate recall exceeds 85%.

### Exit Criteria

-   A reusable scoring worksheet/checklist exists.
-   Session 37 can be scored with it.
-   Misses can be categorized consistently.

**Status:** ☐ Not started ☑ In progress ☐ Complete

**Findings / notes:**

-   `campaign_agent_score.py` scores the agent's read-only plan against the
    DM-curated golden vault (`~/goldenVault/IceMooreVault`): it rebuilds the
    golden *before* commit in a scratch dir, ingests the raw session, and
    compares with the golden *after* commit. Only notes whose added lines
    link the session count as touched, so vault-wide edits in the same
    commit are ignored. `--plan-json` rescores a saved plan without Ollama.
-   Golden refs: Session 37 = `87f1fcc..751ba89`, Session 38 =
    `751ba89..1146c6d`. Baselines (V4.17) are in `docs/golden/`.
-   Fact recall is a token-overlap approximation because golden notes are
    prose; placement, discovery and links are structural.
-   Remaining: confirm the Furgus / Skeletal Hand tattoo conflict between
    the golden vault and the Session 37 fixture (see below).

------------------------------------------------------------------------

## M3 --- Session 38 Blind Generalization Test

**Goal:** Measure how well the existing system handles a session it was
not specifically built around.

### Rules

-   Do **not** modify the agent specifically for Session 38 before the
    first test.
-   Run Session 38 through the normal analysis/planning path.
-   Record proposed operations before correcting them manually.
-   Compare the output against a human-reviewed expected result.
-   Score using the M2 rubric.

### Failure Taxonomy

Track failures as categories rather than one-off bugs:

-   Missed entity
-   Missed fact
-   Wrong entity resolution
-   Duplicate entity
-   Missed relationship
-   Wrong relationship
-   Wrong CREATE / UPDATE / RELATED / REVIEW classification
-   Unsupported inference
-   Bad note placement
-   Bad canonicalization
-   Other generalizable semantic failure

### Exit Criteria

Either:

-   Session 38 reaches the target thresholds on the first blind attempt,
    **or**
-   Every material miss is documented and classified so it can drive a
    general improvement.

**Status:** ☐ Not started ☐ In progress ☑ Complete (2026-10-08; second
exit criterion: every miss classified)

**Baseline score:** discovery ~83%, fact recall ~60%, placement ~30%,
relationships ~6%, unsupported ~15%, critical canon errors written 0
(dry run). Fails the targets.

**Findings / notes:** see `docs/m3/SESSION_38_BLIND_BASELINE.md`
(DM-reviewed expected result, raw run, and failure classes F1--F9).
The largest general gaps are accent-insensitive name matching (F1),
Session-37-specific evidence windows (F3), fact placement on co-mentioned
entities (F4), and linking new descriptions to known entities (F5, F6).

------------------------------------------------------------------------

## M4 --- Generalize From Session 38 Failures

**Goal:** Fix general ingestion weaknesses rather than making Session
38-specific patches.

### Rules

-   Prefer general semantic/planner rules.
-   Do not introduce `if session == 38`-style behavior.
-   Preserve Session 37 behavior.
-   Preserve software safety boundaries.
-   Add regression expectations for newly discovered failure classes.

### Required Validation

After changes:

-   Session 37 deterministic baseline remains green.
-   Session 37 full semantic/planner baseline remains green.
-   Repair-pipeline/meta-regression remains green.
-   Session 38 improves according to the M2 rubric.

### Exit Criteria

-   General fixes address the identified Session 38 failure categories.
-   No known regression is introduced.
-   Session 38 reaches or exceeds the acceptance target after general
    improvements.

**Status:** ☐ Not started ☐ In progress ☐ Complete

**Findings / notes:**

------------------------------------------------------------------------

## M5 --- Establish Multi-Session Regression Coverage

**Goal:** Stop relying almost entirely on Session 37 as the semantic
regression specimen.

### Plan

-   Preserve Session 37 as the original fixture.
-   Convert human-reviewed Session 38 expectations into a second
    permanent ingestion fixture.
-   Ensure future changes can be tested against both sessions.
-   Keep regression expectations focused on campaign behavior rather
    than model wording.

### Exit Criteria

-   Sessions 37 and 38 are independently represented in permanent
    regression coverage.
-   Both remain green after subsequent changes.
-   New semantic rules are protected against regression.

**Status:** ☐ Not started ☐ In progress ☐ Complete

**Findings / notes:**

------------------------------------------------------------------------

## M6 --- Session 39 Blind Acceptance Test

**Goal:** Demonstrate that improvements made using Sessions 37 and 38
generalize to another unseen session.

### Rules

-   Session 39 must be tested before session-specific development.
-   Score the first attempt using the same rubric.
-   Do not count fixes made after seeing Session 39 toward the blind
    baseline.

### Primary Acceptance Target

-   Relevant information recall ≥85%
-   Relationship/linkage recall ≥85%
-   Correct entity/note placement ≥90%
-   Unsupported material ≤5%
-   Critical canon errors = 0

### Exit Criteria

**Preferred:** Session 39 reaches the acceptance target on its first
blind attempt without session-specific code changes.

This is the key evidence that the Campaign Agent is becoming suitable
for routine ingestion rather than merely reproducing Sessions 37/38.

**Status:** ☐ Not started ☐ In progress ☐ Complete

**Blind score:** \_\_\_\_\_\_

**Findings / notes:**

------------------------------------------------------------------------

## M7 --- Simple Routine Ingestion Workflow

**Goal:** Make adding a normal future session an operational task rather
than a development project.

### Target Workflow

``` text
New session note
      ↓
Campaign Agent analysis
      ↓
Concise proposed changes
      ↓
CREATE / UPDATE / RELATED / REVIEW
      ↓
Human approve / reject / edit
      ↓
Transactional vault write
      ↓
Post-write verification
      ↓
Done
```

The user should not normally need to modify Python, create regression
tests, or run the development/repair pipeline simply to add a session.

### Exit Criteria

-   One clear command/workflow initiates ingestion.
-   Review is concise and understandable.
-   Approved changes are applied transactionally.
-   Uncertain material remains reviewable rather than becoming canon
    automatically.
-   Normal successful ingestion requires no code changes.

**Status:** ☐ Not started ☐ In progress ☐ Complete

**Findings / notes:**

------------------------------------------------------------------------

## M8 --- Reliability and Vault-Integrity Hardening

**Goal:** Strengthen routine ingestion after generalization has been
demonstrated.

### Candidate Work

-   Deterministic pre/post vault-integrity signature.
-   Automated proof that regression/dev tooling does not modify campaign
    canon.
-   Post-write verification of expected files and links.
-   Stronger transaction/rollback acceptance tests.
-   Additional multi-session fixtures as new semantic patterns appear.

### Exit Criteria

-   Vault safety is tested as an invariant rather than inferred only
    from architecture.
-   Routine ingestion failures can be detected and recovered without
    silently corrupting canon.

**Status:** ☐ Not started ☐ In progress ☐ Complete

**Findings / notes:**

------------------------------------------------------------------------

# Overall Readiness Gates

## Gate A --- Known Session E2E

Session 37 successfully passes disposable-vault ingestion and audit.

**Status:** ☑ (2026-10-08, run `20261008_214712_116253`)

## Gate B --- First Generalization Measurement

Session 38 receives a blind baseline score using the formal rubric.

**Status:** ☑ (2026-10-08; scored with the draft rubric in `docs/m3/`)

## Gate C --- Multi-Session Learning

General improvements are protected by Session 37 + Session 38 regression
coverage.

**Status:** ☐

## Gate D --- Unseen Session ≥85%

Session 39 or another genuinely unseen session reaches the target
thresholds without session-specific code changes.

**Status:** ☐

## Gate E --- Routine Ingestion Ready

Adding a new session is normally:

**analyze → review → approve → transactional write → verify**

rather than a software-development cycle.

**Status:** ☐

------------------------------------------------------------------------

# Immediate Next Action

**Begin M1: Session 37 End-to-End Acceptance Test using a disposable
copy of the vault.**

Do not modify the trusted vault for the initial acceptance run. Audit
the resulting disposable vault before deciding whether to perform the
real Session 37 ingestion.

------------------------------------------------------------------------

## Continuation Notes

Use this section when pausing development so the next work session can
resume quickly.

**Current milestone:** M4 --- Generalize From Session 38 Failures
(M1, M3 complete; M2 rubric drafted inline in `docs/m3/`)\
**Current Campaign Agent version:** V4.21 (V4.20 plus deterministic
named-entity candidates from naming patterns and unique head-word resolution,
e.g. "the Academy" -> Mistra's Academy)\
**Current development orchestrator:** V4.15.1\
**Known frozen baselines:** 72/0/1 deterministic; 80/0/0 full; pipeline
PASS. Golden scores per version: `docs/golden/`\
**Last completed action:** M1 Session 37 E2E acceptance passed on a
disposable vault (run `20261008_214712_116253`).\
**Next action:** Golden (V4.21, cached model output): entity discovery
81% / 75%, session links 71% / 71%, relationship recall 30.6% / 16.8%
(Sessions 38 / 37). Fact recall (33% / 48%) is now the largest gap and is
mostly model capacity. Re-measure on a larger model once available. Set
`CAMPAIGN_AGENT_LLM_CACHE=_test_runs/llm_cache` to replay cached model
output when comparing code changes. The DM ruled the golden vault
authoritative; real Session 37 ingestion waits until the agent is ~95% of
golden by `campaign_agent_score.py`.

**Additional notes:**

-   E2E vault: `/Users/dhruvmukul/Icemoor_Obsidian_Vault_E2E` (fresh copy
    made 2026-10-08; the older copy is kept as
    `Icemoor_Obsidian_Vault_E2E_old_20260927`). Re-copy the trusted vault
    before each new acceptance run.
-   Moving Ollama to another machine: set `ollama_url` (and `model`, if
    larger) in both configs. Treat the first `--full` run on a new model
    as a new baseline, since extraction results are model-dependent.
