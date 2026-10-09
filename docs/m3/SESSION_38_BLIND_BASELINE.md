# M3 — Session 38 Blind Baseline (DM-reviewed 2026-10-08)

**DM decisions:**
1. Session 38 "Peter" is Peter Owens, son of the late Lord Richard Owens (Session 36).
2. "Andrew" is only a *suspected* link to `Blond-Haired Youth`, not an alias. Expected handling: Andrew as his own REVIEW candidate, plus an open question linking him to Blond-Haired Youth / Skeletal Hand.
3. "Velvet Glove" is an alias of `Velvet Glove Brothel`. The alias was added in the vault (commit `e46c8a5`, branch `claude_ollama_session37`), so F2 is a vault-data gap, not a code defect. The blind score below still reflects the vault before the alias.
4. Atheela, Zelda, Karyn, Peter Owens, Siobhan, Union of Many and Andrew are all worth tracking.

- **Run:** `session_38_run1_dryrun.txt` (2026-10-08, agent V4.16.1, `qwen3:1.7b`)
- **Vault state:** E2E vault *after* the accepted Session 37 run (`20261008_214712_116253`)
- **Mode:** dry run, no agent changes made after seeing Session 38

**How to review:** the expected result below is Claude's draft from the
session text. Edit it directly: strike or fix rows, add anything missed, and
change ✓/½/✗ where the call is wrong. The score is recomputed from your
corrected table, and the corrected version becomes the permanent Session 38
fixture (M5).

Marks: ✓ = correct, ½ = partly right or acceptable but not ideal, ✗ = missed or wrong.

---

## 1. Entity discovery

### Existing notes that should be matched

| Entity | Run 1 | Note |
|---|---|---|
| Deanira | ✓ | Also proposed a duplicate `Deaníra` (see failure F1) |
| Daddy Dildo Daggins | ✓ | "Brother Daggins" |
| The Two Tits | ✓ | |
| Riffle | ✓ | |
| Sabrina | ✓ | |
| Velvet Glove Brothel | ½ | Not matched. `Velvet Glove` went to REVIEW as a possible duplicate (F2) |
| Heaven's Touch | ✓ | |
| Skeletal Hand | ✓ | |
| Theo'din Corvis | ✓ | |
| Talir Rengavi | ✓ | |
| Bianca Onyxcrest | ✓ | via "Bianca's assistant" |

Blond-Haired Youth is not expected as a match. Per DM decision 2, Andrew is only a suspected link to it (scored under Relationships).

### New entities (should be CREATE or REVIEW)

| Candidate | Expected | Run 1 | Note |
|---|---|---|---|
| The Union of Many [faction] | REVIEW/CREATE | ✓ REVIEW | |
| Atheela [npc] | CREATE | ✓ CREATE | |
| Zelda [npc] | CREATE | ✓ CREATE | |
| Karyn [npc] | CREATE | ✓ CREATE | |
| Peter Owens [npc] | CREATE | ½ | Split into `Peter` (CREATE) and `Owen` (blocked). Session 36 says the son of the late Lord Richard Owens is "Peter? Owens" (F6) |
| Archivist Siobhan [npc] | REVIEW | ✗ | Witness in the stolen-documents accusation (F7) |
| Andrew [npc] | REVIEW, with suspected link to Blond-Haired Youth | ✗ | (F5) |

### Should NOT be created

| Candidate | Run 1 | Note |
|---|---|---|
| Cloaked hooded figure [npc] | ✗ proposed CREATE | Unnamed one-off; REVIEW at most (F8) |
| Mausoleum [location] | ✗ proposed CREATE | Generic name; at most "Owens Family Mausoleum" as REVIEW (F6) |
| Deaníra [npc] | ½ REVIEW as duplicate | Should resolve to Deanira (F1) |
| Jeffrey, Angela, Gala, deities | ✓ not created | Incidental (deities could become Lore later) |
| The Gauntlet [quest] | ½ REVIEW | Real campaign concept (`[[Gauntlet]]` is a dangling link in Amberhold); event rather than quest |

## 2. Facts and placement (where each fact should land)

| # | Fact (source-supported) | Correct home | Run 1 |
|---|---|---|---|
| 1 | The Union of Many: an order of fertility/love deities (Shialliah, Lethander, Melorah) meeting weekly in the Two Tits basement | Union of Many, The Two Tits | ✓ on Two Tits (source-grounded) |
| 2 | Daggins ("Brother Daggins") was a member, which answers the Session 37 open question on the Two Tits | Daddy Dildo Daggins, The Two Tits | ✗ RELATED only; the open question was not resolved |
| 3 | Atheela is a member; Deanira showed her the silver dildo Daggins entrusted to her | Atheela, Deanira | ½ extracted, RELATED only |
| 4 | Sabrina: blond man "Andrew" visited Velvet Glove worker Angela; party suspects the Skeletal Hand recruiter | Blond-Haired Youth (open question) | ½ placed on Skeletal Hand as model interpretation |
| 5 | Heaven's Touch: converted waterfront warehouse; enchantment aura on staff; no-magic zone; Zelda moved there from the Velvet Glove | Heaven's Touch, Zelda | ✗ **placed on The Two Tits** (wrong entity) |
| 6 | Theo cast Dispel Magic, so the party was ejected from Heaven's Touch | Theo'din Corvis / Heaven's Touch | ✗ |
| 7 | Peter Owens: graduate student; parents just died; leave of absence; seen at family mausoleum; rumor that the cook poisoned them | Peter Owens | ½ extracted with the Peter/Owen confusion; not planned |
| 8 | Gauntlet: 4 non-competing teams, 5 themed rounds | Gauntlet | ½ extracted, **placed on Riffle** |
| 9 | Cloaked figure entered the Owens mausoleum and vanished | open question | ½ extracted as a development; no open question |
| 10 | Talir accuses Riffle of stealing documents from his safe; Archivist Siobhan claims she saw Riffle (the party was elsewhere, which echoes the Session 37 red-robe framing) | Riffle, Talir (open question) | ½ mentioned, not planned |
| 11 | Session 37 TODO "shop for the Gala" was done (gown, jewels) | Session TODO lifecycle | ½ extracted as a quest; no lifecycle (deferred feature) |

## 3. Relationships

| Expected link | Run 1 |
|---|---|
| Union of Many ↔ The Two Tits | ½ in fact text only |
| Union of Many ↔ Daddy Dildo Daggins | ✗ |
| Atheela ↔ Union of Many | ✗ |
| Andrew ↔ Blond-Haired Youth ↔ Skeletal Hand (suspected, open question) | ✗ |
| Zelda ↔ Velvet Glove Brothel / Heaven's Touch | ✗ |
| Karyn ↔ Bianca Onyxcrest | ✗ |
| Peter Owens ↔ Academy / Owens family | ✗ |
| Siobhan / Talir ↔ Riffle (accusation) | ✗ |

## 4. Blind baseline score (DM-reviewed)

| Category | Target | Run 1 |
|---|---|---|
| Entity discovery | ≥85% | **~83%** (15/18, counting ½ marks) |
| Fact recall (extracted anywhere) | ≥85% | **~60%** |
| Correct placement of proposed updates | ≥90% | **~30%** (1 right, 2 wrong entity, 1 off-target) |
| Relationship recall | ≥85% | **~6%** |
| Unsupported material | ≤5% | **~15%** (2 bad creates, 1 wrong-entity fact) |
| Critical canon errors | 0 | **0 written** (dry run; the wrong-entity fact defaults to NO) |

**Verdict:** the blind baseline fails the targets. Discovery is close, but
placement and linkage are far below. Under the M3 exit criterion, each miss is
classified below so it can drive a general fix in M4.

## 5. Failure classes (drive M4; none are session-specific)

| ID | Class | Cause found in code / run |
|---|---|---|
| F1 | Bad canonicalization | `normalize_name("Deaníra")` returns `"dean ra"`: non-ASCII letters become spaces, so accented spellings never match |
| F2 | Wrong entity resolution (vault data) | `Velvet Glove` did not resolve to `Velvet Glove Brothel`. Fixed in the vault by adding the alias (DM decision 3) |
| F3 | Missed fact / weak grounding | The source-evidence index searches only Session 37 topics (`torm_sect`, `blond_recruiter`, `two_tits_basement`, `academy_robes`, ...). Session 38 got 2 windows, and they're whole paragraphs because its lines are long |
| F4 | Bad note placement | Facts attached to a co-mentioned entity instead of their subject (Heaven's Touch facts → Two Tits; Gauntlet → Riffle) |
| F5 | Missed relationship | A new description of a known unnamed NPC (blond man "Andrew") is not linked to `Blond-Haired Youth`. Needs alias/description matching (agent-side RAG is a deferred feature) |
| F6 | Bad canonicalization | One person split into two candidates (`Peter` / `Owen`); generic location name (`Mausoleum`) |
| F7 | Missed entity / fact | The end-of-session plot (stolen documents, Archivist Siobhan) was not promoted to an entity or open question |
| F8 | Unsupported inference | Unnamed one-off figure proposed as CREATE. The quality gate's NPC rules are literal Session 37 strings (`goliath sailor`, `sailors`) |
| F9 | Missed lifecycle | A Session 37 open question (Two Tits ↔ Daggins' Order) is answered but not resolved (deferred knowledge-lifecycle feature) |
