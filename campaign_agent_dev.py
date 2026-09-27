#!/usr/bin/env python3
"""Campaign Agent V4.9 validation + diagnostic handoff orchestrator."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parent
AGENT=ROOT/"campaign_agent.py"
HARNESS=ROOT/"campaign_agent_regression.py"
REPORTS=ROOT/"_test_runs"
DET=(30,0,1); FULL=(41,0,0)

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

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--agent",default=str(AGENT)); ap.add_argument("--harness",default=str(HARNESS))
    ap.add_argument("--report-root",default=str(REPORTS)); ap.add_argument("--fast-only",action="store_true")
    a=ap.parse_args(); agent=Path(a.agent).expanduser().resolve()
    harness=Path(a.harness).expanduser().resolve(); reports=Path(a.report_root).expanduser().resolve()
    started=datetime.now(timezone.utc); stamp=started.astimezone().strftime("%Y%m%d_%H%M%S")
    dev=reports/f"{stamp}_dev"; i=1
    while dev.exists(): dev=reports/f"{stamp}_dev_{i}"; i+=1
    dev.mkdir(parents=True)
    r={"schema_version":2,"pipeline_version":"4.9","started_at":started.isoformat(),
       "agent":str(agent),"agent_sha256":sha(agent),"harness":str(harness),
       "harness_sha256":sha(harness),"fast_only":a.fast_only,"stages":{},"artifacts":{},
       "result":"failed"}
    print("Campaign Agent Development Pipeline V4.9\n========================================")
    print(f"Agent   : {agent.name}\nHarness : {harness.name}\nReports : {reports}\nRun     : {dev.name}")

    rc,o=run([sys.executable,"-m","py_compile",str(agent),str(harness)])
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
    r["result"]="ready-for-review"; return finish(r,dev,started)

if __name__=="__main__": raise SystemExit(main())
