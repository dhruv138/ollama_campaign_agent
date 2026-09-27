#!/usr/bin/env python3
"""Campaign Agent V4.14.3 validation + strategy-gated human repair orchestration."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parent
AGENT=ROOT/"campaign_agent.py"
HARNESS=ROOT/"campaign_agent_regression.py"
REPORTS=ROOT/"_test_runs"
PIPELINE_HARNESS=ROOT/"campaign_agent_pipeline_regression.py"
DET=(30,0,1); FULL=(41,0,0); PIPELINE=(24,0)

def sha(path:Path):
    if not path.is_file(): return None
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""): h.update(b)
    return h.hexdigest()

def run(cmd):
    print("\n$ "+" ".join(map(str,cmd)),flush=True)
    q=subprocess.Popen(cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                       text=True,bufsize=1)
    lines=[]
    assert q.stdout
    for line in q.stdout:
        print(line,end="",flush=True); lines.append(line)
    return q.wait(),"".join(lines)

def newest(root:Path,mode:str,since:int):
    xs=[x for x in root.glob(f"*_{mode}*/regression.json")
        if x.stat().st_mtime_ns>=since]
    if not xs: raise FileNotFoundError(f"No new {mode} regression.json under {root}")
    path=max(xs,key=lambda x:x.stat().st_mtime_ns)
    data=json.loads(path.read_text())
    return path,data

def cnt(r):
    c=r.get("counts") or {}
    return int(c.get("passed",0)),int(c.get("failed",0)),int(c.get("skipped",0))

def stage_report(rc,actual,expected,path,report):
    return {
      "status":"pass" if rc==0 and actual==expected else "fail",
      "returncode":rc,
      "expected_counts":dict(zip(("passed","failed","skipped"),expected)),
      "actual_counts":dict(zip(("passed","failed","skipped"),actual)),
      "report":str(path),
      "failures":[x for x in (report.get("failures") or []) if isinstance(x,dict)]
    }

def pipeline_counts(output:str):
    import re
    p=re.search(r"(?m)^Passed:\s*(\d+)\s*$",output)
    f=re.search(r"(?m)^Failed:\s*(\d+)\s*$",output)
    if not p or not f:
        raise ValueError("Pipeline regression output did not contain Passed/Failed summary counts.")
    return int(p.group(1)),int(f.group(1))

def make_handoff(r):
    failures=[]
    for name,s in r["stages"].items():
        if s.get("status")!="pass":
            failures.append({"stage":name,"error":s.get("error"),
                             "expected_counts":s.get("expected_counts"),
                             "actual_counts":s.get("actual_counts")})
        for row in s.get("failures") or []:
            failures.append({"stage":name,**row})
    if r["result"]=="ready-for-review":
        action="All validation passed. Human review is required before promotion."
    elif r["result"]=="ready-for-review-fast-only":
        action="Fast validation passed. Run the full pipeline before release review."
    else:
        action=("Diagnose only the reported failures, prepare a candidate change, "
                "then rerun validation. Do not modify campaign canon.")
    return {
      "schema_version":1,"kind":"campaign-agent-development-handoff",
      "result":r["result"],"failure_stage":r.get("failure_stage"),
      "next_action":action,
      "safety_boundary":{"vault_writes":False,"automatic_agent_replacement":False,
        "automatic_patch_application":False,"automatic_git_commit":False,
        "human_review_required":True},
      "inputs":{"agent":r["agent"],"agent_sha256":r["agent_sha256"],
                "harness":r["harness"],"harness_sha256":r["harness_sha256"]},
      "stages":r["stages"],"failures":failures,"artifacts":r["artifacts"]
    }

def md(h):
    x=["# Campaign Agent Development Handoff","",
       f"**Result:** `{h['result']}`",
       f"**Failure stage:** `{h.get('failure_stage') or 'none'}`","","## Validation",""]
    for name,s in h["stages"].items():
        x.append(f"- **{name}:** {s.get('status','unknown').upper()}")
        if "actual_counts" in s: x.append(f"  - actual: `{json.dumps(s['actual_counts'],sort_keys=True)}`")
        if "expected_counts" in s: x.append(f"  - expected: `{json.dumps(s['expected_counts'],sort_keys=True)}`")
        if s.get("error"): x.append(f"  - error: `{s['error']}`")
    x += ["","## Failures",""]
    if h["failures"]:
        for f in h["failures"]:
            label=f.get("label") or f.get("stage") or "failure"
            detail=f.get("detail") or f.get("error") or ""
            x.append(f"- **{label}**"+(f": {detail}" if detail else ""))
    else: x.append("- None.")
    x += ["","## Safety boundary","",
          "- No campaign-vault writes.","- No automatic agent replacement.",
          "- No automatic patch application.","- No automatic git commit/tag/push.",
          "- Human review is required before promotion.","","## Next action","",
          h["next_action"],""]
    return "\n".join(x)

def finish(r,dev,started):
    r["finished_at"]=datetime.now(timezone.utc).isoformat()
    r["duration_seconds"]=round((datetime.now(timezone.utc)-started).total_seconds(),3)
    rp=dev/"dev_report.json"; hj=dev/"handoff.json"; hm=dev/"handoff.md"
    r["artifacts"].update(dev_report=str(rp),handoff_json=str(hj),handoff_markdown=str(hm))
    h=make_handoff(r)
    rp.write_text(json.dumps(r,indent=2)+"\n")
    hj.write_text(json.dumps(h,indent=2)+"\n")
    hm.write_text(md(h))
    ready=r["result"].startswith("ready-for-review")
    lines=["","Development Pipeline Summary","----------------------------"]
    lines += [f"{n:<14} {s.get('status','unknown').upper()}" for n,s in r["stages"].items()]
    lines += ["","READY FOR REVIEW" if ready else "FAILED",
              f"Report : {rp}",f"Handoff: {hm}",f"JSON   : {hj}"]
    if r["result"]=="ready-for-review-fast-only":
        lines.append("Note: full Ollama regression was intentionally skipped.")
    summary="\n".join(lines)+"\n"; print(summary,end="")
    (dev/"summary.txt").write_text(summary)
    return 0 if ready else 1

def validation_main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--agent",default=str(AGENT)); ap.add_argument("--harness",default=str(HARNESS))
    ap.add_argument("--report-root",default=str(REPORTS)); ap.add_argument("--fast-only",action="store_true")
    a=ap.parse_args(argv); agent=Path(a.agent).expanduser().resolve()
    harness=Path(a.harness).expanduser().resolve(); reports=Path(a.report_root).expanduser().resolve()
    started=datetime.now(timezone.utc); stamp=started.astimezone().strftime("%Y%m%d_%H%M%S")
    dev=reports/f"{stamp}_dev"; i=1
    while dev.exists(): dev=reports/f"{stamp}_dev_{i}"; i+=1
    dev.mkdir(parents=True)
    r={"schema_version":2,"pipeline_version":"4.14.2","started_at":started.isoformat(),
       "agent":str(agent),"agent_sha256":sha(agent),"harness":str(harness),
       "harness_sha256":sha(harness),"fast_only":a.fast_only,"stages":{},"artifacts":{},
       "result":"failed"}
    print("Campaign Agent Development Pipeline V4.14.3\n=========================================\nValidation engine: frozen campaign baselines + V4.14 pipeline meta-regression")
    print(f"Agent   : {agent.name}\nHarness : {harness.name}\nReports : {reports}\nRun     : {dev.name}")

    syntax_targets=[str(agent),str(harness)]
    if PIPELINE_HARNESS.is_file(): syntax_targets.append(str(PIPELINE_HARNESS))
    rc,o=run([sys.executable,"-m","py_compile",*syntax_targets])
    sl=dev/"syntax_output.txt"; sl.write_text(o); r["artifacts"]["syntax_log"]=str(sl)
    r["stages"]["syntax"]={"status":"pass" if rc==0 else "fail","returncode":rc}
    if rc: r["failure_stage"]="syntax"; return finish(r,dev,started)
    print("PASS  Python syntax validation")

    since=time.time_ns()
    rc,o=run([sys.executable,str(harness),"--agent",str(agent),"--report-root",str(reports)])
    dl=dev/"deterministic_output.txt"; dl.write_text(o); r["artifacts"]["deterministic_log"]=str(dl)
    try:
        path,rep=newest(reports,"deterministic",since); actual=cnt(rep)
        r["artifacts"]["deterministic_report"]=str(path)
        r["stages"]["deterministic"]=stage_report(rc,actual,DET,path,rep)
    except Exception as e:
        r["stages"]["deterministic"]={"status":"fail","returncode":rc,"error":str(e)}
    if r["stages"]["deterministic"]["status"]!="pass":
        r["failure_stage"]="deterministic"; return finish(r,dev,started)
    if a.fast_only:
        r["result"]="ready-for-review-fast-only"; return finish(r,dev,started)

    since=time.time_ns()
    rc,o=run([sys.executable,str(harness),"--full","--agent",str(agent),"--report-root",str(reports)])
    fl=dev/"full_output.txt"; fl.write_text(o); r["artifacts"]["full_log"]=str(fl)
    try:
        path,rep=newest(reports,"full",since); actual=cnt(rep)
        r["artifacts"]["full_report"]=str(path)
        r["stages"]["full"]=stage_report(rc,actual,FULL,path,rep)
    except Exception as e:
        r["stages"]["full"]={"status":"fail","returncode":rc,"error":str(e)}
    if r["stages"]["full"]["status"]!="pass":
        r["failure_stage"]="full"; return finish(r,dev,started)

    # V4.14.3 meta-regression: protect the repair/development safety policy.
    if not PIPELINE_HARNESS.is_file():
        r["stages"]["pipeline_regression"]={"status":"fail",
            "error":f"Pipeline regression harness not found: {PIPELINE_HARNESS}"}
        r["failure_stage"]="pipeline_regression"; return finish(r,dev,started)

    rc,o=run([sys.executable,str(PIPELINE_HARNESS)])
    pl=dev/"pipeline_regression_output.txt"; pl.write_text(o)
    r["artifacts"]["pipeline_regression_log"]=str(pl)
    try:
        actual=pipeline_counts(o)
        r["stages"]["pipeline_regression"]={
            "status":"pass" if rc==0 and actual==PIPELINE else "fail",
            "returncode":rc,
            "expected_counts":{"passed":PIPELINE[0],"failed":PIPELINE[1]},
            "actual_counts":{"passed":actual[0],"failed":actual[1]}}
    except Exception as e:
        r["stages"]["pipeline_regression"]={"status":"fail","returncode":rc,"error":str(e)}
    if r["stages"]["pipeline_regression"]["status"]!="pass":
        r["failure_stage"]="pipeline_regression"; return finish(r,dev,started)

    r["result"]="ready-for-review"; return finish(r,dev,started)

DIAGNOSER=ROOT/"campaign_agent_diagnose.py"
ANALYZER=ROOT/"campaign_agent_analyze.py"
REPAIRER=ROOT/"campaign_agent_repair.py"

def _load_json(path:Path):
    data=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data,dict): raise ValueError(f"Expected JSON object: {path}")
    return data

def _resolve_artifact(raw):
    if not raw: return None
    p=Path(str(raw)).expanduser()
    if not p.is_absolute(): p=(ROOT/p).resolve()
    return p

def _run_capture(cmd, log_path:Path):
    rc,out=run(cmd)
    log_path.write_text(out)
    return rc,out

def _candidate_from_manifest(path:Path):
    m=_load_json(path)
    if m.get("kind")!="campaign-agent-candidate-manifest":
        raise ValueError("Repairer did not produce a candidate manifest.")
    p=_resolve_artifact((m.get("candidate") or {}).get("path"))
    if not p or not p.is_file(): raise FileNotFoundError("Generated candidate is unavailable.")
    expected=(m.get("candidate") or {}).get("sha256")
    if expected and sha(p)!=expected:
        raise RuntimeError("Generated candidate SHA-256 does not match its manifest.")
    return p,m

def repair_main(handoff_arg:str, report_root:str|None=None, yes_test:bool=False):
    handoff=Path(handoff_arg).expanduser().resolve()
    if not handoff.is_file(): raise FileNotFoundError(f"Handoff not found: {handoff}")
    h=_load_json(handoff)
    if h.get("kind")!="campaign-agent-development-handoff":
        raise ValueError("Input is not a Campaign Agent development handoff.")
    if h.get("result")!="failed":
        raise RuntimeError("Repair mode requires a failed handoff.")

    for tool in (DIAGNOSER,ANALYZER,REPAIRER):
        if not tool.is_file(): raise FileNotFoundError(f"Required V4.10/V4.11 tool missing: {tool}")

    reports=Path(report_root).expanduser().resolve() if report_root else REPORTS
    started=datetime.now(timezone.utc)
    stamp=started.astimezone().strftime("%Y%m%d_%H%M%S")
    repair_run=reports/f"{stamp}_repair"
    i=1
    while repair_run.exists(): repair_run=reports/f"{stamp}_repair_{i}"; i+=1
    repair_run.mkdir(parents=True)

    state={"schema_version":1,"kind":"campaign-agent-repair-orchestration",
           "pipeline_version":"4.13.1","started_at":started.isoformat(),
           "source_handoff":str(handoff),"stages":{},"artifacts":{},
           "safety":{"trusted_agent_replacement":False,"git_commit":False,
                     "vault_writes":False,"fixture_changes":False,
                     "human_test_approval_required":True}}

    print("Campaign Agent Repair Orchestrator V4.14.3")
    print("========================================")
    print(f"Handoff : {handoff}")
    print(f"Run     : {repair_run}")

    # 1) Package diagnosis into this orchestration run.
    diagnosis_dir=repair_run/"diagnosis"
    rc,_=_run_capture([sys.executable,str(DIAGNOSER),str(handoff),
                       "--output-dir",str(diagnosis_dir)],repair_run/"diagnose_output.txt")
    state["stages"]["diagnose"]={"status":"pass" if rc==0 else "fail","returncode":rc}
    if rc:
        state["result"]="failed"; state["failure_stage"]="diagnose"
        (repair_run/"repair_orchestration.json").write_text(json.dumps(state,indent=2)+"\n")
        return 1
    diagnosis=diagnosis_dir/"diagnosis.json"
    state["artifacts"]["diagnosis"]=str(diagnosis)

    # 2) Analyze evidence and produce a repair plan.
    rc,_=_run_capture([sys.executable,str(ANALYZER),str(diagnosis),
                       "--output-dir",str(diagnosis_dir)],repair_run/"analyze_output.txt")
    state["stages"]["analyze"]={"status":"pass" if rc==0 else "fail","returncode":rc}
    if rc:
        state["result"]="failed"; state["failure_stage"]="analyze"
        (repair_run/"repair_orchestration.json").write_text(json.dumps(state,indent=2)+"\n")
        return 1
    plan=diagnosis_dir/"repair_plan.json"
    state["artifacts"]["repair_plan"]=str(plan)

    # V4.14.3 strategy gate: unsupported strategies stop safely before candidate generation.
    plan_data=_load_json(plan)
    strategy=plan_data.get("repair_strategy") or {}
    state["repair_strategy"]=strategy
    if strategy.get("automatic_candidate_allowed") is not True:
        state["result"]="review-required"
        state["stages"]["strategy_gate"]={"status":"review-only",
            "strategy":strategy.get("strategy"),"reason":strategy.get("reason")}
        state["finished_at"]=datetime.now(timezone.utc).isoformat()
        report=repair_run/"repair_orchestration.json"
        report.write_text(json.dumps(state,indent=2)+"\n")
        print("\nV4.14.3 Strategy Gate")
        print("-------------------")
        print(f"Strategy : {strategy.get('strategy') or 'unknown'}")
        print(f"Confidence: {strategy.get('confidence') or 'unknown'}")
        print("Candidate: NOT GENERATED")
        print("Reason   : "+str(strategy.get("reason") or "unsupported repair strategy"))
        print(f"Plan     : {plan}")
        print(f"Report   : {report}")
        print("\nREVIEW REQUIRED")
        return 3
    state["stages"]["strategy_gate"]={"status":"pass","strategy":strategy.get("strategy")}

    # 3) Generate a NEW candidate. V4.14.3 repairer re-verifies the strategy and source hash.
    candidate_root=repair_run/"candidates"
    rc,_=_run_capture([sys.executable,str(REPAIRER),str(plan),
                       "--candidate-root",str(candidate_root)],repair_run/"repair_output.txt")
    state["stages"]["generate_candidate"]={"status":"pass" if rc==0 else "fail","returncode":rc}
    if rc:
        state["result"]="failed"; state["failure_stage"]="generate_candidate"
        (repair_run/"repair_orchestration.json").write_text(json.dumps(state,indent=2)+"\n")
        return 1

    manifests=sorted(candidate_root.glob("*/candidate_manifest.json"),
                     key=lambda p:p.stat().st_mtime_ns,reverse=True)
    if not manifests: raise FileNotFoundError("Repairer completed but no candidate manifest was found.")
    candidate,manifest=_candidate_from_manifest(manifests[0])
    diff=_resolve_artifact((manifest.get("artifacts") or {}).get("diff"))
    state["artifacts"].update(candidate=str(candidate),candidate_manifest=str(manifests[0]),
                              candidate_diff=str(diff) if diff else None)
    state["candidate_sha256"]=sha(candidate)

    print("\nCandidate diff")
    print("--------------")
    if diff and diff.is_file(): print(diff.read_text(),end="")
    else: print("(diff unavailable)")

    # 4) Human gate. --yes-test is explicit CLI approval, useful for scripted/manual reruns.
    approved=yes_test
    if not approved:
        if not sys.stdin.isatty():
            print("\nCandidate generated but testing requires explicit approval.")
            print(f"Review: {diff}")
            print(f"Then rerun with: python3 {Path(__file__).name} --repair {handoff} --yes-test")
            state["result"]="awaiting-test-approval"
            state["stages"]["human_test_approval"]={"status":"pending"}
            (repair_run/"repair_orchestration.json").write_text(json.dumps(state,indent=2)+"\n")
            return 2
        ans=input("\nRun the frozen V4.9 validation pipeline against this generated candidate? [y/N] ").strip().casefold()
        approved=ans in {"y","yes"}

    if not approved:
        state["result"]="awaiting-test-approval"
        state["stages"]["human_test_approval"]={"status":"declined"}
        report=repair_run/"repair_orchestration.json"
        report.write_text(json.dumps(state,indent=2)+"\n")
        print(f"\nStopped before testing. Candidate preserved at: {candidate}")
        print(f"Report: {report}")
        return 2

    state["stages"]["human_test_approval"]={"status":"approved"}

    # 5) Validate candidate with the frozen validation path. No promotion follows.
    print("\nApproval received. Running frozen validation pipeline against candidate only.")
    validation_args=["--agent",str(candidate),"--report-root",str(reports)]
    rc=validation_main(validation_args)
    state["stages"]["candidate_validation"]={"status":"pass" if rc==0 else "fail","returncode":rc}
    state["result"]="ready-for-review" if rc==0 else "candidate-validation-failed"
    state["finished_at"]=datetime.now(timezone.utc).isoformat()
    report=repair_run/"repair_orchestration.json"
    report.write_text(json.dumps(state,indent=2)+"\n")

    print("\nV4.14.3 Repair Summary")
    print("--------------------")
    print(f"Candidate : {candidate}")
    print(f"Diff      : {diff}")
    print(f"Validation: {'PASS' if rc==0 else 'FAIL'}")
    print("Promotion : NOT PERFORMED")
    print("Git       : NOT MODIFIED")
    print("Vault     : NOT MODIFIED")
    print(f"Report    : {report}")
    if rc==0: print("\nREADY FOR HUMAN REVIEW")
    return rc

def main():
    ap=argparse.ArgumentParser(description="Campaign Agent V4.14.3 development + strategy-gated repair orchestrator.")
    ap.add_argument("--repair",help="Failed V4.9/V4.12 handoff.json to diagnose, analyze, repair, and optionally test.")
    ap.add_argument("--yes-test",action="store_true",
                    help="Explicitly approve testing the generated candidate (does not approve promotion).")
    ap.add_argument("--agent",default=str(AGENT))
    ap.add_argument("--harness",default=str(HARNESS))
    ap.add_argument("--report-root",default=str(REPORTS))
    ap.add_argument("--fast-only",action="store_true")
    a=ap.parse_args()
    if a.repair:
        if a.fast_only:
            raise SystemExit("--fast-only is not supported in repair mode; repaired candidates require full validation.")
        return repair_main(a.repair,a.report_root,a.yes_test)
    argv=["--agent",a.agent,"--harness",a.harness,"--report-root",a.report_root]
    if a.fast_only: argv.append("--fast-only")
    return validation_main(argv)

if __name__=="__main__":
    raise SystemExit(main())
