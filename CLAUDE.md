# Campaign Agent — notes for Claude Code

Ingests D&D session notes into the Icemoor Obsidian vault using a local Ollama
model. Read `docs/ARCHITECTURE.md` (design + safety model) and
`docs/CAMPAIGN_AGENT_INGESTION_MILESTONES.md` (roadmap; M1 is complete, current
milestone is M2; see its "Continuation Notes" for where work left off) before
making non-trivial changes.

## Environment

- Runs on the user's Intel Mac. Ollama at `http://localhost:11434`, model `qwen3:1.7b`.
- Vault: path set by `vault_path` in `config.yaml` (trusted vault) and
  `config.e2e.yaml` (disposable copy for end-to-end tests). The vault is also the
  GitHub repo `dhruv138/IceMooreVault`.
- Python 3.10+, only dependency is PyYAML.

## Commands

```bash
# Deterministic regression (no Ollama, read-only)
python3 campaign_agent_regression.py
# Full semantic/planner regression (calls Ollama, read-only, several minutes)
python3 campaign_agent_regression.py --full
# Repair/dev pipeline meta-regression
python3 campaign_agent_pipeline_regression.py
# Ingest one session: dry run first, then interactive approval
python3 campaign_agent.py "Sessions/Session_37.md" --dry-run
python3 campaign_agent.py "Sessions/Session_37.md" --config config.e2e.yaml
# Undo a write run
python3 campaign_agent.py --rollback <RUN_ID>
```

Regression output goes to `_test_runs/` (git-ignored).

## Rules

- Never run a real (non-dry-run) ingestion against the trusted vault in
  `config.yaml` unless the user explicitly asks. Use `config.e2e.yaml` and a
  disposable vault copy for testing.
- Do not weaken regression expectations in `tests/fixtures/` to make a change pass.
- No session-specific code (e.g. `if session == 38`); fixes must generalize.
- The LLM suggests; Python validates; the human approves every semantic write.
- `archive/` holds old agent versions for reference only; don't edit them.

## Known issue to check

`config.yaml` maps `campaign: "Campaign"` and `clues: "Clues_&_Mysteries"`, but the
vault folders are `00 Campaign` and `Clues & Mysteries`. Those two types are not
written yet, so this is currently harmless.
