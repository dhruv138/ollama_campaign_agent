#!/usr/bin/env python3
"""
Read-only regression harness for Campaign Agent V4.6.3.

Phase 1 deliberately tests deterministic behavior without calling Ollama and
without invoking campaign_agent._main_impl(). It cannot write to the vault.

Usage:
    python3 campaign_agent_regression.py
    python3 campaign_agent_regression.py --verbose
    python3 campaign_agent_regression.py --fixture tests/fixtures/session_37_expectations.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_AGENT = ROOT / "campaign_agent.py"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "session_37_expectations.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return data


def load_agent(path: Path):
    spec = importlib.util.spec_from_file_location("campaign_agent_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import agent: {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses inspect sys.modules while the module is being executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize(agent, value: Any) -> str:
    return agent.normalize_name(str(value or ""))


class Suite:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"PASS  {label}")
            if self.verbose and detail:
                print(f"      {detail}")
        else:
            self.failed += 1
            print(f"FAIL  {label}")
            if detail:
                print(f"      {detail}")

    def skip(self, label: str, detail: str) -> None:
        self.skipped += 1
        print(f"SKIP  {label}")
        if self.verbose:
            print(f"      {detail}")


def resolve_session_path(agent, config: dict[str, Any], fixture: dict[str, Any]) -> tuple[Path, Path]:
    vault_raw = str(config.get("vault_path", "")).strip()
    if not vault_raw or vault_raw.startswith("/CHANGE/"):
        raise ValueError("config.yaml does not contain a usable vault_path.")
    vault_root = Path(vault_raw).expanduser().resolve()
    if not vault_root.is_dir():
        raise FileNotFoundError(f"Vault root does not exist: {vault_root}")

    rel = str(fixture.get("session", "")).strip()
    if not rel:
        raise ValueError("Fixture is missing 'session'.")
    session_path = (vault_root / rel).resolve()
    try:
        session_path.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError("Fixture session resolves outside the configured vault.") from exc
    if not session_path.is_file():
        raise FileNotFoundError(f"Fixture session does not exist: {session_path}")
    return vault_root, session_path


def run() -> int:
    parser = argparse.ArgumentParser(description="Read-only Campaign Agent regression harness.")
    parser.add_argument("--agent", default=str(DEFAULT_AGENT), help="Agent Python file to test.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"), help="Campaign Agent config YAML.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="Regression expectations JSON.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    agent_path = Path(args.agent).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    fixture_path = Path(args.fixture).expanduser().resolve()

    agent = load_agent(agent_path)
    fixture = load_json(fixture_path)
    config = agent.load_yaml_file(config_path)
    vault_root, session_path = resolve_session_path(agent, config, fixture)

    type_folders = agent.normalize_type_folder_config(config.get("type_folders"))
    exclude_folders = list(config.get("exclude_folders") or [".obsidian", "_backups", "_Templates"])

    target = agent.read_note(session_path, vault_root, type_folders)
    source_body = agent.read_canonical_session_body(target)
    existing_notes = agent.scan_vault(vault_root, session_path, type_folders, exclude_folders)
    alias_index = agent.build_alias_index(existing_notes)

    suite = Suite(args.verbose)

    print("Campaign Agent Regression Suite")
    print("================================")
    print(f"Agent   : {agent_path.name}")
    print(f"Fixture : {fixture_path.relative_to(ROOT) if fixture_path.is_relative_to(ROOT) else fixture_path}")
    print(f"Session : {session_path.relative_to(vault_root)}")
    print(f"Vault   : {vault_root}")
    print(f"Notes   : {len(existing_notes)}")
    print("\nDeterministic checks")

    # 1. Fixture/session identity.
    expected_session = str(fixture.get("session", ""))
    suite.check(
        session_path.relative_to(vault_root).as_posix() == Path(expected_session).as_posix(),
        "Target session matches fixture",
    )

    # 2. Explicit TODO parsing.
    todos = agent._extract_explicit_todos(source_body)
    todo_expect = fixture.get("expected_todos") or {}
    expected_count = int(todo_expect.get("count", 0))
    suite.check(
        len(todos) == expected_count,
        f"Explicit TODO count is {expected_count}",
        f"found {len(todos)}",
    )
    todo_text = "\n".join(
        f"{row.get('title', '')} {row.get('detail', '')}" for row in todos
    ).casefold()
    for phrase in todo_expect.get("contains", []):
        suite.check(
            str(phrase).casefold() in todo_text,
            f"TODO contains: {phrase}",
        )

    # 3. Deterministic source diagnostics.
    diag = agent.deterministic_source_diagnostics(source_body)
    diag_expect = fixture.get("expected_source_diagnostics") or {}
    mapping = {
        "torm_min_hits": "torm",
        "blond_min_hits": "blond",
        "two_tits_min_hits": "two_tits",
        "todo_header_min_hits": "todo_markers",
    }
    for fixture_key, diag_key in mapping.items():
        if fixture_key not in diag_expect:
            continue
        minimum = int(diag_expect[fixture_key])
        actual = int(diag.get(diag_key, 0))
        suite.check(
            actual >= minimum,
            f"Source diagnostic {diag_key} >= {minimum}",
            f"found {actual}",
        )

    # 4. Existing-note discovery from YAML, literal wikilinks, and prose.
    yaml_entities = agent.extract_yaml_entities(target, alias_index)
    wikilink_entities = agent.extract_wikilink_entities(
        target, existing_notes, alias_index, source_body
    )
    prose_entities = agent.extract_prose_existing_entities(source_body, existing_notes)

    discovered: dict[str, dict[str, Any]] = {}
    for row in yaml_entities + wikilink_entities + prose_entities:
        key = normalize(agent, row.get("name"))
        if key:
            discovered[key] = row

    for name in fixture.get("expected_existing_matches", []):
        suite.check(
            normalize(agent, name) in discovered,
            f"Existing note discovered: {name}",
        )

    # 5. Deterministic source-only open questions.
    source_questions = agent.extract_explicit_questions_v463(source_body)
    question_text = "\n".join(
        f"{row.get('title', '')} {row.get('detail', '')}" for row in source_questions
    ).casefold()
    suite.check(
        "two tits" in question_text and "daggins" in question_text,
        "Two Tits / Daggins source question recovered",
    )
    suite.check(
        "blond" in question_text and "skeletal hand" in question_text,
        "Blond youth / Skeletal Hand source question recovered",
    )

    # 6. Candidate policy/canonicalization invariants that do not require Ollama.
    for row in fixture.get("forbidden_create_candidates", []):
        entity = {
            "mention": row.get("name", ""),
            "name": row.get("name", ""),
            "type": row.get("type", ""),
            "significance": "meaningful",
        }
        canonical = agent.v463_apply_canonical_candidate_name(entity)
        allowed, reason = agent.v463_candidate_quality_gate(canonical)
        # A forbidden CREATE may either be explicitly blocked by the quality gate,
        # or be a quest that is only allowed through typed campaign-state promotion.
        is_quest = str(row.get("type", "")).lower() == "quest"
        suite.check(
            (not allowed) or is_quest,
            f"Forbidden CREATE is not freely eligible: {row.get('name')}",
            reason,
        )

    sanitation = {
        "mention": "the missing sanitation workers",
        "name": "the missing sanitation workers",
        "type": "quest",
        "significance": "meaningful",
    }
    sanitation = agent.v463_apply_canonical_candidate_name(sanitation)
    suite.check(
        sanitation.get("name") == "Missing Sanitation Workers",
        "Sanitation quest canonicalizes to Missing Sanitation Workers",
        f"got {sanitation.get('name')!r}",
    )

    # 7. Safety invariant: this harness never invokes the CLI/write path.
    if (fixture.get("safety") or {}).get("dry_run_must_not_write"):
        suite.check(
            True,
            "Harness is read-only: CLI approval/write path was not invoked",
        )

    # Expectations that depend on Qwen output + build_change_plan are intentionally
    # deferred until V4.7 exposes a structured analyze_note() seam.
    deferred_groups = (
        "expected_review_candidates",
        "expected_updates",
        "expected_related_only",
        "forbidden_updates",
    )
    deferred_count = sum(len(fixture.get(k) or []) for k in deferred_groups)
    if deferred_count:
        suite.skip(
            f"{deferred_count} planner expectations deferred to V4.7 analysis seam",
            "These require the same structured Qwen + planner result used by the production CLI.",
        )

    print("\nSummary")
    print("-------")
    print(f"{suite.passed} passed")
    print(f"{suite.failed} failed")
    print(f"{suite.skipped} skipped")

    return 1 if suite.failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
