#!/usr/bin/env python3
"""
Campaign Agent development validation orchestrator.

Runs the frozen validation pipeline without modifying the campaign vault:
  1. Python syntax validation
  2. Deterministic regression
  3. Full Ollama-backed regression
  4. Combined machine-readable development report

This tool does NOT run campaign_agent.py's CLI write path, approve changes,
commit to git, or modify campaign canon.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_AGENT = ROOT / "campaign_agent.py"
DEFAULT_HARNESS = ROOT / "campaign_agent_regression.py"
DEFAULT_REPORT_ROOT = ROOT / "_test_runs"


def run_command(command: list[str], cwd: Path) -> tuple[int, str]:
    """Run a child process while streaming and capturing combined output."""
    print("\n$ " + " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return process.wait(), "".join(lines)


def newest_report(report_root: Path, mode: str, after_ns: int) -> tuple[Path, dict[str, Any]]:
    """Find the newest regression.json for this run/mode."""
    candidates: list[Path] = []
    if report_root.exists():
        for path in report_root.glob(f"*_{mode}*/regression.json"):
            try:
                if path.stat().st_mtime_ns >= after_ns:
                    candidates.append(path)
            except OSError:
                continue
    if not candidates:
        raise FileNotFoundError(
            f"No new {mode} regression.json was produced under {report_root}"
        )
    path = max(candidates, key=lambda p: p.stat().st_mtime_ns)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Regression report is not a JSON object: {path}")
    return path, data


def counts(report: dict[str, Any]) -> tuple[int, int, int]:
    raw = report.get("counts") or {}
    return (
        int(raw.get("passed", 0)),
        int(raw.get("failed", 0)),
        int(raw.get("skipped", 0)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Campaign Agent development validation pipeline."
    )
    parser.add_argument("--agent", default=str(DEFAULT_AGENT))
    parser.add_argument("--harness", default=str(DEFAULT_HARNESS))
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    parser.add_argument(
        "--fast-only",
        action="store_true",
        help="Run syntax + deterministic regression only; skip Ollama/full regression.",
    )
    args = parser.parse_args()

    agent = Path(args.agent).expanduser().resolve()
    harness = Path(args.harness).expanduser().resolve()
    report_root = Path(args.report_root).expanduser().resolve()

    started = datetime.now(timezone.utc)
    run_stamp = started.astimezone().strftime("%Y%m%d_%H%M%S")
    dev_dir = report_root / f"{run_stamp}_dev"
    suffix = 1
    while dev_dir.exists():
        dev_dir = report_root / f"{run_stamp}_dev_{suffix}"
        suffix += 1
    dev_dir.mkdir(parents=True, exist_ok=False)

    result: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "agent": str(agent),
        "harness": str(harness),
        "fast_only": bool(args.fast_only),
        "stages": {},
        "result": "failed",
    }

    print("Campaign Agent Development Pipeline")
    print("===================================")
    print(f"Agent   : {agent.name}")
    print(f"Harness : {harness.name}")
    print(f"Reports : {report_root}")
    print(f"Run     : {dev_dir.name}")

    # Stage 1: syntax.
    syntax_cmd = [sys.executable, "-m", "py_compile", str(agent), str(harness)]
    rc, output = run_command(syntax_cmd, ROOT)
    result["stages"]["syntax"] = {
        "status": "pass" if rc == 0 else "fail",
        "returncode": rc,
    }
    if rc != 0:
        result["failure_stage"] = "syntax"
        (dev_dir / "syntax_output.txt").write_text(output, encoding="utf-8")
        return finish(result, dev_dir, started)

    print("PASS  Python syntax validation")

    # Stage 2: deterministic regression.
    before = time.time_ns()
    det_cmd = [
        sys.executable,
        str(harness),
        "--agent", str(agent),
        "--report-root", str(report_root),
    ]
    rc, output = run_command(det_cmd, ROOT)
    (dev_dir / "deterministic_output.txt").write_text(output, encoding="utf-8")
    try:
        det_path, det_report = newest_report(report_root, "deterministic", before)
        det_counts = counts(det_report)
    except Exception as exc:
        result["stages"]["deterministic"] = {
            "status": "fail",
            "returncode": rc,
            "error": str(exc),
        }
        result["failure_stage"] = "deterministic-report"
        return finish(result, dev_dir, started)

    det_ok = rc == 0 and det_counts == (30, 0, 1)
    result["stages"]["deterministic"] = {
        "status": "pass" if det_ok else "fail",
        "returncode": rc,
        "expected_counts": {"passed": 30, "failed": 0, "skipped": 1},
        "actual_counts": {
            "passed": det_counts[0], "failed": det_counts[1], "skipped": det_counts[2]
        },
        "report": str(det_path),
    }
    if not det_ok:
        result["failure_stage"] = "deterministic"
        return finish(result, dev_dir, started)

    if args.fast_only:
        result["result"] = "ready-for-review-fast-only"
        return finish(result, dev_dir, started)

    # Stage 3: full semantic/planner regression.
    before = time.time_ns()
    full_cmd = [
        sys.executable,
        str(harness),
        "--full",
        "--agent", str(agent),
        "--report-root", str(report_root),
    ]
    rc, output = run_command(full_cmd, ROOT)
    (dev_dir / "full_output.txt").write_text(output, encoding="utf-8")
    try:
        full_path, full_report = newest_report(report_root, "full", before)
        full_counts = counts(full_report)
    except Exception as exc:
        result["stages"]["full"] = {
            "status": "fail",
            "returncode": rc,
            "error": str(exc),
        }
        result["failure_stage"] = "full-report"
        return finish(result, dev_dir, started)

    full_ok = rc == 0 and full_counts == (41, 0, 0)
    result["stages"]["full"] = {
        "status": "pass" if full_ok else "fail",
        "returncode": rc,
        "expected_counts": {"passed": 41, "failed": 0, "skipped": 0},
        "actual_counts": {
            "passed": full_counts[0], "failed": full_counts[1], "skipped": full_counts[2]
        },
        "report": str(full_path),
        "failures": full_report.get("failures") or [],
    }
    if not full_ok:
        result["failure_stage"] = "full"
        return finish(result, dev_dir, started)

    result["result"] = "ready-for-review"
    return finish(result, dev_dir, started)


def finish(result: dict[str, Any], dev_dir: Path, started: datetime) -> int:
    finished = datetime.now(timezone.utc)
    result["finished_at"] = finished.isoformat()
    result["duration_seconds"] = round((finished - started).total_seconds(), 3)

    report_path = dev_dir / "dev_report.json"
    report_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    status = result.get("result")
    ready = status in {"ready-for-review", "ready-for-review-fast-only"}

    lines = [
        "",
        "Development Pipeline Summary",
        "----------------------------",
    ]
    for name, stage in result.get("stages", {}).items():
        lines.append(f"{name:<14} {str(stage.get('status', 'unknown')).upper()}")
    lines.extend([
        "",
        "READY FOR REVIEW" if ready else "FAILED",
        f"Report: {report_path}",
    ])
    if status == "ready-for-review-fast-only":
        lines.append("Note: full Ollama regression was intentionally skipped.")

    summary = "\n".join(lines) + "\n"
    print(summary, end="")
    (dev_dir / "summary.txt").write_text(summary, encoding="utf-8")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
