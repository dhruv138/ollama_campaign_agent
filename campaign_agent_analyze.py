#!/usr/bin/env python3
"""
Campaign Agent V4.10 evidence-based regression analyzer.

Consumes diagnosis.json produced by campaign_agent_diagnose.py, inspects only
the candidate agent source plus referenced evidence, and produces a repair plan.

It does NOT modify source, fixtures, the vault, or git. It does NOT apply patches.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SOURCE_MATCHES = 12
CONTEXT_LINES = 10


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return data


def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def dedupe_failures(rows: list[Any]) -> list[dict[str, Any]]:
    """Deduplicate handoff/report copies by semantic failure identity."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = norm(row.get("label") or row.get("stage") or "failure")
        detail = norm(row.get("detail") or row.get("error") or "")
        key = (label.casefold(), detail.casefold())
        if key in seen:
            continue
        # Drop a generic stage-only row when a specific failure exists later.
        seen.add(key)
        out.append({"label": label, "detail": detail})
    specific = [r for r in out if r["detail"]]
    if specific:
        generic_labels = {r["label"].casefold() for r in specific}
        out = [
            r for r in out
            if r["detail"] or r["label"].casefold() not in {"deterministic", "full", "syntax"}
        ]
    return out


def expected_actual(failure: dict[str, Any]) -> tuple[str | None, str | None]:
    detail = failure.get("detail") or ""
    m = re.search(r"got\s+(['\"])(.*?)\1", detail, flags=re.I)
    actual = m.group(2) if m else None

    label = failure.get("label") or ""
    expected = None
    # Common regression wording: "... canonicalizes to X"
    m = re.search(r"\b(?:canonicalizes|resolves|equals|is)\s+to?\s*(.+)$", label, flags=re.I)
    if m:
        expected = m.group(1).strip(" .:'\"")
    if not expected:
        m = re.search(r"\bto\s+(.+)$", label, flags=re.I)
        if m:
            expected = m.group(1).strip(" .:'\"")
    return expected, actual


def source_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def find_literal_matches(lines: list[str], terms: list[str]) -> list[int]:
    hits: list[int] = []
    lowered = [x.casefold() for x in lines]
    for term in terms:
        t = norm(term).casefold()
        if len(t) < 4:
            continue
        for i, line in enumerate(lowered, 1):
            if t in line and i not in hits:
                hits.append(i)
                if len(hits) >= MAX_SOURCE_MATCHES:
                    return hits
    return hits


def function_ranges(path: Path) -> list[tuple[int, int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(text)
    rows: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            rows.append((node.lineno, int(end), node.name))
    return rows


def containing_function(line_no: int, funcs: list[tuple[int, int, str]]) -> str | None:
    candidates = [(a, b, n) for a, b, n in funcs if a <= line_no <= b]
    if not candidates:
        return None
    a, b, name = min(candidates, key=lambda x: x[1] - x[0])
    return name


def excerpts(lines: list[str], hits: list[int], funcs: list[tuple[int, int, str]]) -> list[dict[str, Any]]:
    result = []
    used: set[tuple[int, int]] = set()
    for hit in hits:
        start = max(1, hit - CONTEXT_LINES)
        end = min(len(lines), hit + CONTEXT_LINES)
        key = (start, end)
        if key in used:
            continue
        used.add(key)
        body = "\n".join(f"{i:5d}: {lines[i-1]}" for i in range(start, end + 1))
        result.append({
            "match_line": hit,
            "function": containing_function(hit, funcs),
            "start_line": start,
            "end_line": end,
            "source": body,
        })
    return result


def classify(stage: str, failures: list[dict[str, Any]]) -> str:
    if stage.startswith("syntax"):
        return "syntax-regression"
    if stage.startswith("deterministic"):
        return "behavioral-regression"
    if stage.startswith("full"):
        return "semantic-or-planner-regression"
    return "unknown-regression"


def make_plan(diag: dict[str, Any], diag_path: Path) -> dict[str, Any]:
    failures = dedupe_failures(diag.get("observed_failures") or [])
    inputs = diag.get("inputs") or {}
    agent_raw = inputs.get("agent")
    if not agent_raw:
        raise ValueError("Diagnosis package does not identify an agent source.")
    agent = Path(agent_raw).expanduser().resolve()
    if not agent.is_file():
        raise FileNotFoundError(f"Candidate agent source is unavailable: {agent}")

    stage = str(diag.get("failure_stage") or "unknown")
    lines = source_lines(agent)
    funcs = function_ranges(agent)

    terms: list[str] = []
    parsed: list[dict[str, Any]] = []
    for f in failures:
        expected, actual = expected_actual(f)
        parsed.append({**f, "expected": expected, "actual": actual})
        if actual:
            terms.append(actual)
        if expected:
            terms.append(expected)

    # Failure text is evidence too, but literal expected/actual values rank first.
    for f in failures:
        terms.extend(re.findall(r"[A-Z][A-Za-z0-9' -]{5,}", f.get("label") or ""))

    hits = find_literal_matches(lines, terms)
    src_excerpts = excerpts(lines, hits, funcs)
    functions = []
    for x in src_excerpts:
        if x["function"] and x["function"] not in functions:
            functions.append(x["function"])

    confidence = "high" if any(p.get("actual") for p in parsed) and hits else (
        "medium" if hits else "low"
    )

    hypothesis = (
        f"The observed value from the failed regression appears literally in the "
        f"candidate source near {', '.join(functions[:3])}."
        if functions
        else
        "The failed regression is recorded, but no direct literal match was found in the candidate source."
    )

    return {
        "schema_version": 1,
        "kind": "campaign-agent-repair-plan",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": classify(stage, failures),
        "failure_stage": stage,
        "candidate": {
            "path": str(agent),
            "sha256": inputs.get("agent_sha256"),
        },
        "failed_checks": parsed,
        "suspected_functions": functions,
        "source_evidence": src_excerpts,
        "root_cause_hypothesis": hypothesis,
        "root_cause_confidence": confidence,
        "proposed_change": {
            "target": "candidate-agent-only",
            "intent": (
                "Restore behavior required by the failed regression using the smallest "
                "general source change consistent with the captured evidence."
            ),
            "fixture_change_allowed": False,
            "trusted_agent_change_allowed": False,
            "automatic_patch_allowed": False,
        },
        "validation_required": [
            "Run campaign_agent_dev.py --agent <candidate>",
            "Require syntax validation to pass",
            "Require deterministic baseline 30/0/1",
            "Require full semantic/planner baseline 41/0/0 before promotion",
            "Require human review before replacing the trusted agent",
        ],
        "source_diagnosis": str(diag_path),
    }


def markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Campaign Agent Repair Plan",
        "",
        f"**Classification:** `{plan['classification']}`",
        f"**Failure stage:** `{plan['failure_stage']}`",
        f"**Root-cause confidence:** `{plan['root_cause_confidence']}`",
        "",
        "## Failed checks",
        "",
    ]
    for f in plan["failed_checks"]:
        detail = f.get("detail") or ""
        lines.append(f"- **{f['label']}**" + (f": {detail}" if detail else ""))
        if f.get("expected") is not None:
            lines.append(f"  - Expected: `{f['expected']}`")
        if f.get("actual") is not None:
            lines.append(f"  - Actual: `{f['actual']}`")

    lines += ["", "## Suspected source area", ""]
    if plan["suspected_functions"]:
        for fn in plan["suspected_functions"]:
            lines.append(f"- `{fn}`")
    else:
        lines.append("- No function could be identified automatically.")

    lines += [
        "",
        "## Root-cause hypothesis",
        "",
        plan["root_cause_hypothesis"],
        "",
        "## Source evidence",
        "",
    ]
    if plan["source_evidence"]:
        for ex in plan["source_evidence"]:
            lines += [
                f"### `{ex.get('function') or 'module scope'}` — lines {ex['start_line']}-{ex['end_line']}",
                "",
                "```python",
                ex["source"],
                "```",
                "",
            ]
    else:
        lines.append("No direct source excerpt was identified.")

    lines += [
        "## Proposed change boundary",
        "",
        "- Modify only a candidate copy of the agent.",
        "- Do not modify the trusted agent in place.",
        "- Do not change regression fixtures automatically.",
        "- Do not apply patches automatically.",
        "- Prefer the smallest general fix supported by evidence.",
        "",
        "## Required validation",
        "",
    ]
    for item in plan["validation_required"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Analyze a Campaign Agent diagnosis package and create a repair plan."
    )
    ap.add_argument("diagnosis", help="Path to diagnosis.json")
    ap.add_argument(
        "--output-dir",
        help="Destination directory; defaults to the diagnosis directory.",
    )
    args = ap.parse_args()

    diag_path = Path(args.diagnosis).expanduser().resolve()
    diag = load_json(diag_path)
    if diag.get("kind") != "campaign-agent-diagnostic-package":
        raise ValueError("Input is not a Campaign Agent diagnostic package.")

    output = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else diag_path.parent
    )
    output.mkdir(parents=True, exist_ok=True)

    plan = make_plan(diag, diag_path)
    json_path = output / "repair_plan.json"
    md_path = output / "repair_plan.md"
    json_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(markdown(plan), encoding="utf-8")

    print("Campaign Agent Regression Analyzer V4.10")
    print("========================================")
    print(f"Diagnosis : {diag_path}")
    print(f"Class     : {plan['classification']}")
    print(f"Confidence: {plan['root_cause_confidence']}")
    print("Functions : " + (", ".join(plan["suspected_functions"]) or "none"))
    print(f"JSON      : {json_path}")
    print(f"Review    : {md_path}")
    print()
    print("No source files, fixtures, vault files, or git state were modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
