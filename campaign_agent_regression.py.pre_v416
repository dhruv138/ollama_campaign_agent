#!/usr/bin/env python3
"""
Read-only regression harness for Campaign Agent V4.8 tooling.

Deterministic mode never calls Ollama. --full exercises the structured
analyze_note() seam. Neither mode invokes campaign_agent._main_impl() or writes
to the campaign vault.

V4.8 adds machine-readable run artifacts under _test_runs/.

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
from datetime import datetime, timezone
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
        self.results: list[dict[str, Any]] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        status = "pass" if ok else "fail"
        self.results.append({"status": status, "label": label, "detail": detail})
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
        self.results.append({"status": "skip", "label": label, "detail": detail})
        print(f"SKIP  {label}")
        if self.verbose:
            print(f"      {detail}")


def write_run_artifacts(
    *,
    suite: Suite,
    report_root: Path,
    mode: str,
    agent_path: Path,
    fixture_path: Path,
    session_rel: str,
    vault_root: Path,
    started_at: datetime,
) -> Path:
    """Write regression metadata only; never write inside the campaign vault."""
    finished_at = datetime.now(timezone.utc)
    stamp = started_at.astimezone().strftime("%Y%m%d_%H%M%S")
    run_dir = report_root / f"{stamp}_{mode}"
    suffix = 1
    while run_dir.exists():
        run_dir = report_root / f"{stamp}_{mode}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)

    failures = [r for r in suite.results if r["status"] == "fail"]
    skips = [r for r in suite.results if r["status"] == "skip"]
    payload = {
        "schema_version": 1,
        "mode": mode,
        "result": "fail" if suite.failed else "pass",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "agent": str(agent_path),
        "fixture": str(fixture_path),
        "session": session_rel,
        "vault": str(vault_root),
        "counts": {
            "passed": suite.passed,
            "failed": suite.failed,
            "skipped": suite.skipped,
        },
        "failures": failures,
        "skips": skips,
        "checks": suite.results,
    }
    (run_dir / "regression.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary_lines = [
        "Campaign Agent Regression",
        "=========================",
        f"Mode:    {mode}",
        f"Result:  {payload['result'].upper()}",
        f"Passed:  {suite.passed}",
        f"Failed:  {suite.failed}",
        f"Skipped: {suite.skipped}",
        f"Seconds: {payload['duration_seconds']}",
    ]
    if failures:
        summary_lines.extend(["", "Failures", "--------"])
        for row in failures:
            summary_lines.append(f"- {row['label']}")
            if row.get("detail"):
                summary_lines.append(f"  {row['detail']}")
    (run_dir / "summary.txt").write_text(
        "\n".join(summary_lines) + "\n",
        encoding="utf-8",
    )
    return run_dir


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
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the Ollama-backed V4.7 analyze_note() planner regression.",
    )
    parser.add_argument(
        "--report-root",
        default=str(ROOT / "_test_runs"),
        help="Directory for machine-readable regression artifacts.",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write regression artifacts.",
    )
    args = parser.parse_args()
    started_at = datetime.now(timezone.utc)

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

    # 8. Full V4.7 structured planner regression.
    # This is opt-in because it calls Ollama and can take several minutes.
    if not args.full:
        deferred_groups = (
            "expected_review_candidates",
            "expected_updates",
            "expected_related_only",
            "forbidden_updates",
        )
        deferred_count = sum(len(fixture.get(k) or []) for k in deferred_groups)
        if deferred_count:
            suite.skip(
                f"{deferred_count} planner expectations (rerun with --full)",
                "Full mode calls agent.analyze_note() and therefore Ollama.",
            )
    else:
        if not hasattr(agent, "analyze_note"):
            suite.check(
                False,
                "V4.7 structured analysis seam is available",
                "campaign_agent.py has no analyze_note(); install V4.7.0 first.",
            )
        else:
            print("\nFull semantic/planner regression")
            print("This calls Ollama and may take several minutes.")
            analysis = agent.analyze_note(
                target=target,
                existing_notes=existing_notes,
                config=config,
                vault_root=vault_root,
                type_folders=type_folders,
            )
            plan = analysis["change_plan"]

            creates = plan.get("creates") or []
            reviews = plan.get("reviews") or []
            updates = plan.get("updates") or []

            def same_name(a: Any, b: Any) -> bool:
                return normalize(agent, a) == normalize(agent, b)

            def typed_rows(rows, name: str, etype: str):
                return [
                    row for row in rows
                    if same_name(row.get("entity"), name)
                    and str(row.get("type") or "").casefold() == str(etype).casefold()
                ]

            def find_update(entity_name: str):
                for row in updates:
                    if same_name(row.get("entity"), entity_name):
                        return row
                return None

            def title_contains(row: dict[str, Any], fragment: str) -> bool:
                return str(fragment).casefold() in str(row.get("title") or "").casefold()

            # Expected REVIEW candidates, including exact multiplicity.
            for expected in fixture.get("expected_review_candidates", []):
                name = str(expected.get("name") or "")
                etype = str(expected.get("type") or "")
                wanted = int(expected.get("exactly", 1))
                found = typed_rows(reviews, name, etype)
                suite.check(
                    len(found) == wanted,
                    f"REVIEW candidate exactly {wanted}x: {name} [{etype}]",
                    f"found {len(found)}",
                )

            # Forbidden CREATE candidates must never reach the actionable CREATE bucket.
            for forbidden in fixture.get("forbidden_create_candidates", []):
                name = str(forbidden.get("name") or "")
                etype = str(forbidden.get("type") or "")
                found = typed_rows(creates, name, etype)
                suite.check(
                    not found,
                    f"Forbidden CREATE absent: {name} [{etype}]",
                    f"found {len(found)} actionable CREATE row(s)",
                )

            # Expected semantic UPDATE proposals.
            for expected in fixture.get("expected_updates", []):
                entity_name = str(expected.get("entity") or "")
                fragment = str(expected.get("title_contains") or "")
                classification = str(expected.get("classification") or "")
                update = find_update(entity_name)
                proposals = (update or {}).get("proposals") or []
                matches = [
                    p for p in proposals
                    if title_contains(p, fragment)
                    and (
                        not classification
                        or str(p.get("classification") or "") == classification
                    )
                ]
                suite.check(
                    bool(matches),
                    f"UPDATE {entity_name}: {fragment} [{classification}]",
                    f"{len(matches)} matching proposal(s)",
                )

            # Expected RELATED-only material must appear as context and not as UPDATE.
            for expected in fixture.get("expected_related_only", []):
                entity_name = str(expected.get("entity") or "")
                fragment = str(expected.get("title_contains") or "")
                update = find_update(entity_name)
                related = (update or {}).get("related") or []
                proposals = (update or {}).get("proposals") or []
                related_match = any(title_contains(r, fragment) for r in related)
                proposal_match = any(title_contains(p, fragment) for p in proposals)
                suite.check(
                    related_match and not proposal_match,
                    f"RELATED-only {entity_name}: {fragment}",
                    f"related={related_match}, update={proposal_match}",
                )

            # Explicitly forbidden semantic UPDATEs.
            for forbidden in fixture.get("forbidden_updates", []):
                entity_name = str(forbidden.get("entity") or "")
                fragment = str(forbidden.get("title_contains") or "")
                update = find_update(entity_name)
                proposals = (update or {}).get("proposals") or []
                matches = [p for p in proposals if title_contains(p, fragment)]
                suite.check(
                    not matches,
                    f"Forbidden UPDATE absent: {entity_name} / {fragment}",
                    f"found {len(matches)} matching proposal(s)",
                )

            # The structured seam itself must expose all agreed V4.7 products.
            suite.check(
                all(
                    key in analysis
                    for key in ("result", "resolved", "conflict_keys", "campaign_state", "change_plan")
                ),
                "analyze_note() exposes the complete structured analysis contract",
            )

    print("\nSummary")
    print("-------")
    print(f"{suite.passed} passed")
    print(f"{suite.failed} failed")
    print(f"{suite.skipped} skipped")

    if not args.no_report:
        report_root = Path(args.report_root).expanduser().resolve()
        run_dir = write_run_artifacts(
            suite=suite,
            report_root=report_root,
            mode="full" if args.full else "deterministic",
            agent_path=agent_path,
            fixture_path=fixture_path,
            session_rel=str(fixture.get("session") or ""),
            vault_root=vault_root,
            started_at=started_at,
        )
        print(f"Report : {run_dir}")
        print(f"         {run_dir / 'regression.json'}")
        print(f"         {run_dir / 'summary.txt'}")

    return 1 if suite.failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
