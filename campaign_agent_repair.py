#!/usr/bin/env python3
# Campaign Agent V4.11 candidate repair generator.
from __future__ import annotations
import argparse, ast, difflib, hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def load_json(p: Path) -> dict[str, Any]:
    x=json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(x,dict): raise ValueError("Expected JSON object")
    return x

def sha256_file(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def norm(x: Any)->str: return re.sub(r"\s+"," ",str(x or "")).strip()

def positions(source:str,value:str):
    tree=ast.parse(source); out=[]
    for n in ast.walk(tree):
        if isinstance(n,ast.Constant) and isinstance(n.value,str) and n.value==value:
            out.append((n.lineno,n.col_offset,int(n.end_lineno),int(n.end_col_offset)))
    return out

def funcs(source:str):
    tree=ast.parse(source); out=[]
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            out.append((n.lineno,int(n.end_lineno),n.name))
    return out

def enclosing(line:int, ranges):
    x=[r for r in ranges if r[0]<=line<=r[1]]
    return min(x,key=lambda r:r[1]-r[0])[2] if x else None

def span(source:str,pos):
    starts=[0]
    starts += [m.end() for m in re.finditer("\n",source)]
    sl,sc,el,ec=pos
    return starts[sl-1]+sc, starts[el-1]+ec

def encode_string(value:str, old_literal:str)->str:
    # Preserve ordinary single/double quote style. Refuse exotic literals.
    s=old_literal
    prefix=""
    while s and s[0] in "rRuUbBfF":
        prefix+=s[0]; s=s[1:]
    if prefix:
        raise RuntimeError("Refusing prefixed/f/raw string repair.")
    if s.startswith('"'): q='"'
    elif s.startswith("'"): q="'"
    else: raise RuntimeError("Unsupported string literal.")
    if s.startswith(q*3): raise RuntimeError("Refusing triple-quoted repair.")
    v=value.replace("\\","\\\\").replace(q,"\\"+q).replace("\n","\\n")
    return q+v+q

def choose(plan,source):
    suspected=set(plan.get("suspected_functions") or [])
    ranges=funcs(source); viable=[]
    for row in plan.get("failed_checks") or []:
        if not isinstance(row,dict): continue
        exp,act=norm(row.get("expected")),norm(row.get("actual"))
        if not exp or not act or exp==act: continue
        for p in positions(source,act):
            fn=enclosing(p[0],ranges)
            viable.append({"expected":exp,"actual":act,"position":p,"function":fn,
                           "label":norm(row.get("label")),
                           "suspected":bool(fn and fn in suspected)})
    preferred=[v for v in viable if v["suspected"]]
    pool=preferred if preferred else viable
    if len(pool)!=1:
        raise RuntimeError(f"Ambiguous/unsupported repair: found {len(pool)} candidate literal locations.")
    return pool[0]

def unique_dir(root:Path)->Path:
    root.mkdir(parents=True,exist_ok=True)
    stem=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")+"_v4_11"
    p=root/stem; i=1
    while p.exists():
        p=root/f"{stem}_{i:02d}"; i+=1
    return p

def main()->int:
    ap=argparse.ArgumentParser(description="Generate a safe V4.11 repaired candidate.")
    ap.add_argument("repair_plan")
    ap.add_argument("--candidate-root",default="_test_runs/candidates")
    a=ap.parse_args()
    plan_path=Path(a.repair_plan).expanduser().resolve()
    plan=load_json(plan_path)
    if plan.get("kind")!="campaign-agent-repair-plan": raise ValueError("Wrong repair plan kind.")

    boundary=plan.get("proposed_change") or {}
    if boundary.get("target")!="candidate-agent-only": raise RuntimeError("Candidate-only authorization missing.")
    if boundary.get("fixture_change_allowed") is not False: raise RuntimeError("Unsafe fixture boundary.")
    if boundary.get("trusted_agent_change_allowed") is not False: raise RuntimeError("Unsafe trusted-agent boundary.")
    if boundary.get("automatic_patch_allowed") is not False: raise RuntimeError("Unexpected automatic patch authorization.")

    ci=plan.get("candidate") or {}
    source_path=Path(ci.get("path") or "").expanduser().resolve()
    expected_hash=norm(ci.get("sha256"))
    if not source_path.is_file(): raise FileNotFoundError(f"Candidate unavailable: {source_path}")
    if not expected_hash: raise ValueError("Repair plan lacks candidate SHA-256.")
    current_hash=sha256_file(source_path)
    if current_hash!=expected_hash:
        raise RuntimeError("SOURCE HASH MISMATCH: analyzed source changed; refusing stale repair.")

    original=source_path.read_text(encoding="utf-8")
    repair=choose(plan,original)
    start,end=span(original,repair["position"])
    old_literal=original[start:end]
    new_literal=encode_string(repair["expected"],old_literal)
    repaired=original[:start]+new_literal+original[end:]
    ast.parse(repaired)
    if repaired==original: raise RuntimeError("Repair produced no change.")

    root=Path(a.candidate_root).expanduser()
    if not root.is_absolute(): root=(Path.cwd()/root).resolve()
    d=unique_dir(root); d.mkdir()
    candidate=d/"campaign_agent.py"; diff=d/"candidate.diff"; manifest=d/"candidate_manifest.json"
    candidate.write_text(repaired,encoding="utf-8")
    new_hash=sha256_file(candidate)
    diff_text="".join(difflib.unified_diff(
        original.splitlines(keepends=True),repaired.splitlines(keepends=True),
        fromfile=str(source_path),tofile=str(candidate),n=3))
    diff.write_text(diff_text,encoding="utf-8")

    m={
      "schema_version":1,"kind":"campaign-agent-candidate-manifest","generator_version":"4.11",
      "created_at":datetime.now(timezone.utc).isoformat(),"result":"candidate-created",
      "inputs":{"repair_plan":str(plan_path),"source_candidate":str(source_path),"source_sha256":current_hash},
      "candidate":{"path":str(candidate),"sha256":new_hash,"syntax_valid":True},
      "repair":{"classification":plan.get("classification"),"failed_check":repair["label"],
                "function":repair["function"],"actual":repair["actual"],"expected":repair["expected"],
                "old_literal":old_literal,"new_literal":new_literal,"source_line":repair["position"][0],
                "method":"exact-python-string-literal-replacement"},
      "artifacts":{"candidate":str(candidate),"diff":str(diff),"manifest":str(manifest)},
      "safety":{"source_modified":False,"trusted_agent_modified":False,
                "regression_fixture_modified":False,"vault_writes":False,"git_modified":False,
                "regressions_run":False,"automatic_promotion":False,"human_review_required":True},
      "next_step":f"Review {diff}, then run: python3 campaign_agent_dev.py --agent {candidate}"
    }
    manifest.write_text(json.dumps(m,indent=2)+"\n",encoding="utf-8")
    print("Campaign Agent Candidate Repair V4.11")
    print("====================================")
    print(f"Source    : {source_path}")
    print(f"Source SHA: {current_hash}")
    print(f"Function  : {repair['function']}")
    print(f"Observed  : {repair['actual']}")
    print(f"Expected  : {repair['expected']}")
    print(f"Candidate : {candidate}")
    print(f"New SHA   : {new_hash}")
    print(f"Diff      : {diff}")
    print(f"Manifest  : {manifest}")
    print("\nNo source, fixture, vault, or git state was modified. Regressions were NOT run.")
    print(f"\nNext:\n  cat {diff}\n  python3 campaign_agent_dev.py --agent {candidate}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
