#!/usr/bin/env python3
"""
Campaign Agent V4.10 diagnostic packager.

Consumes a V4.9 development handoff (handoff.json) and its referenced artifacts,
then produces a compact diagnosis package for human/LLM analysis.

This program is intentionally read-only with respect to:
  - the campaign vault
  - campaign_agent.py
  - regression fixtures
  - git

It does not diagnose by inventing causes and it does not generate/apply patches.
It packages the observed failure evidence and identifies the files/logs relevant
to the next analysis step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def resolve_artifact(raw: Any, base: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def tail_text(path: Path | None, max_chars: int = 12000) -> str | None:
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def relevant_stage(handoff: dict[str, Any]) -> str:
    failure = handoff.get("failure_stage")
    if isinstance(failure, str) and failure:
        return failure
    stages = handoff.get("stages") or {}
    for name, stage in stages.items():
        if isinstance(stage, dict) and stage.get("status") != "pass":
            return str(name)
    return "none"


def collect_failures(handoff: dict[str, Any], regression: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    sources = [handoff.get("failures") or []]
    if regression:
        sources.append(regression.get("failures") or [])

    for source in sources:
        for row in source:
            if not isinstance(row, dict):
                continue
            compact = {
                k: row.get(k)
                for k in ("stage", "label", "detail", "error", "status")
                if row.get(k) is not None
            }
            key = json.dumps(compact, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                rows.append(compact)
    return rows


def markdown(d: dict[str, Any]) -> str:
    lines = [
        "# Campaign Agent Diagnostic Package",
        "",
        f"**Status:** `{d['status']}`",
        f"**Pipeline result:** `{d['pipeline_result']}`",
        f"**Failure stage:** `{d['failure_stage']}`",
        "",
        "## Observed failures",
        "",
    ]
    failures = d.get("observed_failures") or []
    if failures:
        for row in failures:
            label = row.get("label") or row.get("stage") or "failure"
            detail = row.get("detail") or row.get("error") or ""
            lines.append(f"- **{label}**" + (f": {detail}" if detail else ""))
    else:
        lines.append("- No explicit failed check was recorded.")

    lines += [
        "",
        "## Inputs",
        "",
        f"- Agent: `{d['inputs'].get('agent')}`",
        f"- Agent SHA-256: `{d['inputs'].get('agent_sha256')}`",
        f"- Harness: `{d['inputs'].get('harness')}`",
        f"- Harness SHA-256: `{d['inputs'].get('harness_sha256')}`",
        f"- Handoff: `{d['inputs'].get('handoff')}`",
        "",
        "## Evidence artifacts",
        "",
    ]
    for name, value in (d.get("evidence_artifacts") or {}).items():
        lines.append(f"- {name}: `{value}`")

    lines += [
        "",
        "## Diagnostic rules",
        "",
        "- Treat failed regression checks and captured logs as evidence, not as proof of a root cause.",
        "- Do not weaken or delete a regression expectation merely to make the suite pass.",
        "- Prefer the smallest general fix that preserves previously green behavior.",
        "- Do not modify campaign canon.",
        "- Do not modify the trusted agent in place.",
        "- Any candidate patch must be tested through the development pipeline.",
        "- Human review is required before promotion.",
        "",
        "## Next step",
        "",
        d["next_step"],
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Package V4.9 regression evidence for V4.10 diagnosis."
    )
    ap.add_argument("handoff", help="Path to a V4.9 handoff.json")
    ap.add_argument(
        "--output-dir",
        help="Destination directory. Defaults to <handoff-dir>/diagnosis",
    )
    args = ap.parse_args()

    handoff_path = Path(args.handoff).expanduser().resolve()
    if not handoff_path.is_file():
        raise FileNotFoundError(f"Handoff does not exist: {handoff_path}")

    handoff = load_json(handoff_path)
    if handoff.get("kind") != "campaign-agent-development-handoff":
        raise ValueError("Input is not a Campaign Agent development handoff.")

    base = handoff_path.parent
    output = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else base / "diagnosis"
    )
    output.mkdir(parents=True, exist_ok=True)

    stage = relevant_stage(handoff)
    artifacts = handoff.get("artifacts") or {}

    if stage.startswith("full"):
        report_key, log_key = "full_report", "full_log"
    elif stage.startswith("deterministic"):
        report_key, log_key = "deterministic_report", "deterministic_log"
    elif stage.startswith("syntax"):
        report_key, log_key = None, "syntax_log"
    else:
        # A green handoff is still packageable for review, but has no failure report.
        report_key, log_key = "full_report", "full_log"

    report_path = resolve_artifact(artifacts.get(report_key), base) if report_key else None
    log_path = resolve_artifact(artifacts.get(log_key), base) if log_key else None
    regression = load_json(report_path) if report_path and report_path.is_file() else None

    inputs = handoff.get("inputs") or {}
    agent_path = resolve_artifact(inputs.get("agent"), base)
    harness_path = resolve_artifact(inputs.get("harness"), base)

    observed = collect_failures(handoff, regression)
    pipeline_result = str(handoff.get("result") or "unknown")
    failed = pipeline_result == "failed" or stage != "none"

    diagnosis = {
        "schema_version": 1,
        "kind": "campaign-agent-diagnostic-package",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "diagnosis-ready" if failed else "review-ready",
        "pipeline_result": pipeline_result,
        "failure_stage": stage,
        "observed_failures": observed,
        "inputs": {
            "handoff": str(handoff_path),
            "agent": str(agent_path) if agent_path else inputs.get("agent"),
            "agent_sha256": sha256_file(agent_path) if agent_path else inputs.get("agent_sha256"),
            "harness": str(harness_path) if harness_path else inputs.get("harness"),
            "harness_sha256": sha256_file(harness_path) if harness_path else inputs.get("harness_sha256"),
        },
        "evidence_artifacts": {
            "regression_report": str(report_path) if report_path else None,
            "captured_log": str(log_path) if log_path else None,
            "captured_log_tail": "log_tail.txt" if tail_text(log_path) is not None else None,
        },
        "constraints": {
            "vault_writes": False,
            "modify_trusted_agent": False,
            "modify_regression_fixture": False,
            "automatic_patch_application": False,
            "automatic_git_commit": False,
            "human_review_required": True,
        },
        "next_step": (
            "Analyze the observed failure evidence and source code, identify a root-cause "
            "hypothesis, and propose the smallest general candidate change. Keep the trusted "
            "agent unchanged; validate any candidate with campaign_agent_dev.py --agent <candidate>."
            if failed
            else
            "No failed stage is present. This package can be used for review, but no corrective "
            "candidate is indicated."
        ),
    }

    tail = tail_text(log_path)
    if tail is not None:
        (output / "log_tail.txt").write_text(tail, encoding="utf-8")

    (output / "diagnosis.json").write_text(
        json.dumps(diagnosis, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output / "diagnosis.md").write_text(markdown(diagnosis), encoding="utf-8")

    print("Campaign Agent Diagnostic Packager V4.10")
    print("========================================")
    print(f"Input  : {handoff_path}")
    print(f"Stage  : {stage}")
    print(f"Status : {diagnosis['status']}")
    print(f"Output : {output}")
    print(f"JSON   : {output / 'diagnosis.json'}")
    print(f"Review : {output / 'diagnosis.md'}")
    if tail is not None:
        print(f"Log    : {output / 'log_tail.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
