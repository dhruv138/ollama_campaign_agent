#!/usr/bin/env python3
"""
Score one Campaign Agent ingestion against a golden (DM-curated) vault.

The golden vault is a git repository whose history contains a commit before a
session was ingested and a commit after the DM curated it. This tool:

  1. Rebuilds the *before* vault in a scratch directory (`git archive`; the
     golden repository is never modified).
  2. Places the raw player notes for the session into that scratch vault.
  3. Runs the agent's read-only planner (`analyze_note`) on it, or reuses a
     saved plan with --plan-json so scoring changes don't need Ollama.
  4. Derives the expected outcome from the golden before..after diff and scores
     the plan using the M2 rubric categories.

Only notes whose added lines mention the session link count as "touched", so
unrelated vault-wide edits in the same golden commit are ignored. Scores are
structural (which notes, which links) plus an approximate token-overlap fact
recall. Golden notes are prose, so facts are never compared verbatim.

Nothing here writes to the trusted vault, the golden vault, or campaign canon.

Example:
  python3 campaign_agent_score.py --golden ~/goldenVault/IceMooreVault \\
      --before 751ba89 --after 1146c6d --session-name "Session 38" \\
      --raw-session ~/Icemoor_Obsidian_Vault/Sessions/Session_38.md
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SKIP_PREFIXES = (".obsidian/", "Sessions/", "00 Campaign/", "_Templates/", "_backups/")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def load_agent(path: Path):
    spec = importlib.util.spec_from_file_location("campaign_agent_under_score", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses resolve types via sys.modules
    spec.loader.exec_module(module)
    return module


def git(golden: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(golden), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def materialize(golden: Path, ref: str, dest: Path) -> None:
    """Extract a golden commit into dest without touching the golden repo."""
    data = subprocess.run(
        ["git", "-C", str(golden), "archive", "--format=tar", ref],
        check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


# ---------------------------------------------------------------------------
# Expected outcome from the golden diff
# ---------------------------------------------------------------------------

def added_lines(golden: Path, before: str, after: str, path: str) -> list[str]:
    diff = git(golden, "diff", "-U0", before, after, "--", path)
    return [
        line[1:] for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def is_prose(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#") or s == "---":
        return False
    if re.match(r"^[A-Za-z_]+:", s):            # YAML key
        return False
    if re.match(r'^-\s*"?\[\[[^\]]+\]\]"?\s*$', s):  # YAML/list link only
        return False
    return len(s.split()) >= 5


def expected_outcome(agent, golden: Path, before: str, after: str, session_name: str,
                     type_folders: dict[str, str]) -> dict[str, Any]:
    folder_type = {v: k for k, v in type_folders.items()}
    session_key = agent.normalize_name(session_name)

    def mentions_session(lines: list[str]) -> bool:
        return any(
            agent.normalize_name(m.group(1)) == session_key
            for line in lines for m in WIKILINK.finditer(line)
        )

    created: dict[str, dict[str, Any]] = {}
    touched: dict[str, dict[str, Any]] = {}
    for row in git(golden, "diff", "--name-status", before, after).splitlines():
        status, _, path = row.partition("\t")
        if not path.endswith(".md") or path.startswith(SKIP_PREFIXES):
            continue
        title = Path(path).stem
        etype = folder_type.get(path.split("/", 1)[0], "")
        lines = added_lines(golden, before, after, path)
        entry = {
            "path": path, "title": title, "type": etype,
            "links": sorted({
                m.group(1).strip() for line in lines for m in WIKILINK.finditer(line)
                if agent.normalize_name(m.group(1)) not in {session_key, agent.normalize_name(title)}
            }),
            "facts": [line.strip() for line in lines if is_prose(line)],
        }
        if status.startswith("A"):
            text = git(golden, "show", f"{after}:{path}")
            fm, _ = agent.split_frontmatter(text)
            aliases = fm.get("aliases") or []
            entry["aliases"] = [aliases] if isinstance(aliases, str) else list(aliases)
            created[title] = entry
        elif status.startswith("M") and mentions_session(lines):
            touched[title] = entry

    session_links: list[str] = []
    try:
        text = git(golden, "show", f"{after}:Sessions/{session_name}.md")
        fm, _ = agent.split_frontmatter(text)
        for key in ("npcs", "locations", "factions", "quests", "items"):
            for value in fm.get(key) or []:
                m = WIKILINK.search(str(value))
                if m:
                    session_links.append(m.group(1).strip())
    except subprocess.CalledProcessError:
        pass

    return {"created": created, "touched": touched, "session_links": session_links}


# ---------------------------------------------------------------------------
# Agent plan
# ---------------------------------------------------------------------------

def run_agent_plan(agent, config: dict[str, Any], vault: Path, session_rel: str) -> dict[str, Any]:
    type_folders = agent.normalize_type_folder_config(config.get("type_folders"))
    exclude = list(config.get("exclude_folders") or [".obsidian", "_backups", "_Templates"])
    session_path = (vault / session_rel).resolve()
    target = agent.read_note(session_path, vault, type_folders)
    existing = agent.scan_vault(vault, session_path, type_folders, exclude)
    analysis = agent.analyze_note(
        target=target, existing_notes=existing, config=config,
        vault_root=vault, type_folders=type_folders,
    )
    return {
        "change_plan": analysis["change_plan"],
        "campaign_state": analysis["campaign_state"],
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def make_matcher(agent):
    def key(name: str) -> str:
        n = agent.normalize_name(name)
        return re.sub(r"^(?:the|a|an) ", "", n)

    def match(a: str, b: str) -> float:
        ka, kb = key(a), key(b)
        if not ka or not kb:
            return 0.0
        if ka == kb:
            return 1.0
        # Partial: one name is a contiguous part of the other
        # ("Peter" / "Peter Owens", "Black Feathers" / "Order of Black Feathers").
        ta, tb = ka.split(), kb.split()
        short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
        if any(long_[i:i + len(short)] == short for i in range(len(long_) - len(short) + 1)):
            return 0.5
        return 0.0

    return match


def best(match, name: str, candidates: list[str]) -> tuple[float, str | None]:
    scored = [(match(name, c), c) for c in candidates]
    scored = [s for s in scored if s[0] > 0]
    return max(scored) if scored else (0.0, None)


def pct(num: float, den: float) -> float | None:
    return round(100.0 * num / den, 1) if den else None


def score(agent, expected: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    match = make_matcher(agent)
    change_plan = plan["change_plan"]
    proposed_new = [
        str(r.get("entity") or "") for r in
        (change_plan.get("creates") or []) + (change_plan.get("reviews") or [])
    ]
    updates = change_plan.get("updates") or []
    updated_any = [str(u.get("entity") or "") for u in updates]
    updated_semantic = [str(u.get("entity") or "") for u in updates if u.get("proposals")]

    # 1. Entity discovery: expected new notes proposed (CREATE or REVIEW), and
    #    expected touched existing notes matched (at least session history).
    created_rows = []
    for title, e in expected["created"].items():
        names = [title] + [str(a) for a in e.get("aliases") or []]
        got = max((best(match, n, proposed_new) for n in names), key=lambda x: x[0])
        created_rows.append({"expected": title, "score": got[0], "proposed_as": got[1]})
    touched_rows = []
    for title in expected["touched"]:
        got = best(match, title, updated_any)
        sem = best(match, title, updated_semantic)
        touched_rows.append({"expected": title, "matched": got[0], "semantic_update": sem[0] > 0})
    discovery_num = sum(r["score"] for r in created_rows) + sum(r["matched"] for r in touched_rows)
    discovery_den = len(created_rows) + len(touched_rows)

    # 2. Placement: share of semantic proposals that land on a note the golden
    #    vault actually changed (or created) for this session.
    valid_homes = list(expected["touched"]) + list(expected["created"])
    placements = []
    for u in updates:
        for p in u.get("proposals") or []:
            ok = best(match, str(u.get("entity") or ""), valid_homes)[0] >= 1.0
            placements.append({"note": u.get("entity"), "title": p.get("title"), "ok": ok})

    # 3. Unsupported creations: proposed new entities the golden vault never created.
    expected_new = [
        n for e in expected["created"].values()
        for n in [e["title"]] + [str(a) for a in e.get("aliases") or []]
    ]
    unsupported = [n for n in proposed_new if best(match, n, expected_new)[0] == 0]

    # 4. Session links: golden session-note entity links that the agent either
    #    puts in session metadata or proposes as a new note.
    meta = change_plan.get("session_metadata", {}).get("add", {}) or {}
    agent_session_links = [x for v in meta.values() for x in (v or [])] + proposed_new
    link_rows = [
        {"expected": t, "score": best(match, t, agent_session_links)[0]}
        for t in expected["session_links"]
    ]

    # 5. Relationships: links added to entity notes in the golden delta, versus
    #    links the agent proposes (frontmatter related: proposals and wikilinks
    #    inside proposed entries). A golden link counts in either direction.
    surfaced = updated_any + proposed_new
    rel_pairs = [
        (e["title"], link)
        for group in (expected["created"], expected["touched"])
        for e in group.values() for link in e["links"]
    ]
    rel_surfaced = sum(
        1 for a, b in rel_pairs
        if best(match, a, surfaced)[0] > 0 and best(match, b, surfaced + list(expected["touched"]))[0] > 0
    )
    agent_pairs = {
        (str(r.get("source") or ""), str(r.get("target") or ""))
        for r in change_plan.get("relationships") or []
    }
    for u in updates:
        src = str(u.get("entity") or "")
        for r in u.get("relationships") or []:
            agent_pairs.add((src, str(r.get("target") or "")))
        for p in u.get("proposals") or []:
            for link in p.get("entry_links") or []:
                agent_pairs.add((src, str(link[1])))

    def pair_hit(a: str, b: str) -> bool:
        return any(
            (match(a, x) >= 1 and match(b, y) >= 1) or (match(a, y) >= 1 and match(b, x) >= 1)
            for x, y in agent_pairs
        )

    golden_unique = sorted({tuple(sorted((a, b))) for a, b in rel_pairs})
    rel_hits = sum(1 for a, b in golden_unique if pair_hit(a, b))
    agent_correct = sum(
        1 for x, y in agent_pairs
        if any((match(x, a) >= 1 and match(y, b) >= 1) or (match(x, b) >= 1 and match(y, a) >= 1)
               for a, b in golden_unique)
    )

    # 6. Fact recall (approximate): a golden fact counts as covered when some
    #    extracted campaign-state row shares enough distinctive words with it.
    rows = [
        f"{r.get('title') or ''} {r.get('detail') or ''}"
        for section in ("quests", "clues", "developments", "rumors", "open_questions")
        for r in plan["campaign_state"].get(section, []) or [] if isinstance(r, dict)
    ]
    row_tokens = [agent._evidence_tokens(r) for r in rows]
    facts = [
        (e["title"], f)
        for group in (expected["created"], expected["touched"])
        for e in group.values() for f in e["facts"]
    ]
    covered = 0
    for _, fact in facts:
        ft = agent._evidence_tokens(fact)
        if ft and any(len(ft & rt) >= max(3, len(ft) // 3) for rt in row_tokens):
            covered += 1

    return {
        "metrics": {
            "entity_discovery_pct": pct(discovery_num, discovery_den),
            "fact_recall_approx_pct": pct(covered, len(facts)),
            "placement_pct": pct(sum(p["ok"] for p in placements), len(placements)),
            "session_link_recall_pct": pct(sum(r["score"] for r in link_rows), len(link_rows)),
            "relationship_endpoints_surfaced_pct": pct(rel_surfaced, len(rel_pairs)),
            "relationship_recall_pct": pct(rel_hits, len(golden_unique)),
            "relationship_precision_pct": pct(agent_correct, len(agent_pairs)),
            "relationships_proposed": len(agent_pairs),
            "unsupported_new_entity_pct": pct(len(unsupported), len(proposed_new)),
        },
        "detail": {
            "created": created_rows,
            "touched": touched_rows,
            "placements": placements,
            "unsupported_new_entities": unsupported,
            "session_links": link_rows,
            "facts_total": len(facts),
            "facts_covered": covered,
            "relationship_pairs": len(rel_pairs),
            "relationship_pairs_unique": len(golden_unique),
            "agent_relationship_pairs": sorted(agent_pairs),
        },
    }


def render(result: dict[str, Any], args) -> str:
    m = result["metrics"]
    d = result["detail"]
    lines = [
        f"Golden score: {args.session_name}  ({args.before}..{args.after})",
        "-" * 60,
        f"Entity discovery           : {m['entity_discovery_pct']}%   (target >= 85)",
        f"Fact recall (approx)       : {m['fact_recall_approx_pct']}%   (target >= 85; {d['facts_covered']}/{d['facts_total']})",
        f"Placement                  : {m['placement_pct']}%   (target >= 90)",
        f"Session link recall        : {m['session_link_recall_pct']}%",
        f"Relationship recall        : {m['relationship_recall_pct']}%   (target >= 85; {d['relationship_pairs_unique']} golden links)",
        f"Relationship precision     : {m['relationship_precision_pct']}%   ({m['relationships_proposed']} proposed)",
        f"Relationship endpoints     : {m['relationship_endpoints_surfaced_pct']}%   (upper bound: both ends surfaced)",
        f"Unsupported new entities   : {m['unsupported_new_entity_pct']}%   (target <= 5)",
        "",
        "Expected new notes:",
        *[f"  {'OK ' if r['score'] == 1 else ('1/2' if r['score'] else '-- ')} {r['expected']}"
          + (f"  <- {r['proposed_as']}" if r['proposed_as'] and r['score'] < 1 else "")
          for r in d["created"]],
        "Expected updated notes:",
        *[f"  {'OK ' if r['matched'] else '-- '} {r['expected']}"
          + ("  (semantic)" if r["semantic_update"] else "")
          for r in d["touched"]],
        "Semantic proposals:",
        *[f"  {'OK ' if p['ok'] else 'BAD'} [[{p['note']}]] {p['title']}" for p in d["placements"]],
        "Unsupported new entities: " + (", ".join(d["unsupported_new_entities"]) or "(none)"),
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", required=True, type=Path)
    ap.add_argument("--before", required=True, help="Golden commit before the session.")
    ap.add_argument("--after", required=True, help="Golden commit after the session was curated.")
    ap.add_argument("--session-name", required=True, help='Golden session title, e.g. "Session 38".')
    ap.add_argument("--raw-session", required=True, type=Path, help="Raw player notes to ingest.")
    ap.add_argument("--config", default=str(ROOT / "config.e2e.yaml"))
    ap.add_argument("--agent", default=str(ROOT / "campaign_agent.py"))
    ap.add_argument("--plan-json", type=Path, help="Reuse a saved plan instead of calling Ollama.")
    ap.add_argument("--out", type=Path, help="Output directory (default: _test_runs/<timestamp>_score).")
    args = ap.parse_args()

    agent = load_agent(Path(args.agent))
    golden = args.golden.expanduser().resolve()
    out = (args.out or ROOT / "_test_runs" / f"{datetime.now():%Y%m%d_%H%M%S}_score").resolve()
    out.mkdir(parents=True, exist_ok=True)

    config = agent.load_yaml_file(Path(args.config))
    type_folders = agent.normalize_type_folder_config(config.get("type_folders"))
    expected = expected_outcome(agent, golden, args.before, args.after, args.session_name, type_folders)

    if args.plan_json:
        plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    else:
        vault = out / "base_vault"
        if vault.exists():
            shutil.rmtree(vault)
        vault.mkdir(parents=True)
        materialize(golden, args.before, vault)
        session_rel = f"Sessions/{args.session_name}.md"
        (vault / "Sessions").mkdir(exist_ok=True)
        shutil.copyfile(args.raw_session.expanduser(), vault / session_rel)
        config = dict(config, vault_path=str(vault))
        plan = run_agent_plan(agent, config, vault, session_rel)
        (out / "plan.json").write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")

    result = score(agent, expected, plan)
    result["expected"] = expected
    (out / "score.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    summary = render(result, args)
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
