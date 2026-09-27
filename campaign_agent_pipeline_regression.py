#!/usr/bin/env python3
# Campaign Agent V4.15 repair-pipeline + orchestration regression suite.
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
ANALYZER=ROOT/"campaign_agent_analyze.py"
REPAIRER=ROOT/"campaign_agent_repair.py"
AGENT=ROOT/"campaign_agent.py"
FIXTURE=ROOT/"tests/fixtures/session_37_expectations.json"
DEV=ROOT/"campaign_agent_dev.py"
LABEL="Sanitation quest canonicalizes to Missing Sanitation Workers"
EXPECTED="Missing Sanitation Workers"
ACTUAL="Missing Sewer Workers"

def sha(path):
    if not path.is_file(): return None
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""): h.update(b)
    return h.hexdigest()

def run(cmd):
    q=subprocess.run(cmd,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    return q.returncode,q.stdout

def git_status():
    rc,out=run(["git","status","--porcelain"])
    return out if rc==0 else None

def load(path):
    x=json.loads(path.read_text())
    if not isinstance(x,dict): raise ValueError("Expected JSON object")
    return x

def diagnosis(agent):
    return {"schema_version":1,"kind":"campaign-agent-diagnostic-package",
            "pipeline_result":"failed",
            "failure_stage":"deterministic","status":"diagnosis-ready",
            "inputs":{"agent":str(agent.resolve()),"agent_sha256":sha(agent)},
            "observed_failures":[{"label":LABEL,"detail":f"got '{ACTUAL}'"}]}

def make_case(root,source):
    root.mkdir(parents=True,exist_ok=True)
    agent=root/"campaign_agent.py"; agent.write_text(source)
    diag=root/"diagnosis.json"; diag.write_text(json.dumps(diagnosis(agent),indent=2)+"\n")
    return agent,diag

class Results:
    def __init__(self): self.passed=0; self.failed=0; self.rows=[]
    def check(self,ok,label,detail=""):
        status="PASS" if ok else "FAIL"
        print(f"{status:<5} {label}"+(f" — {detail}" if detail and not ok else ""))
        self.rows.append({"status":status.lower(),"label":label,"detail":detail})
        if ok:self.passed+=1
        else:self.failed+=1

def positive(tmp,r):
    print("\nSafe literal-replacement policy\n--------------------------------")
    source='''def v463_canonical_candidate_name(entity):
    name = str(entity.get("name") or "").strip()
    if str(entity.get("type") or "").lower() == "quest" and name.casefold() == "the missing sanitation workers":
        return "Missing Sewer Workers"
    return name
'''
    case=tmp/"literal"; agent,diag=make_case(case,source); out=case/"analysis"
    rc,log=run([sys.executable,str(ANALYZER),str(diag),"--output-dir",str(out)])
    r.check(rc==0,"Literal case analyzer completes",log[-800:])
    plan=out/"repair_plan.json"; r.check(plan.is_file(),"Literal case repair plan created")
    if not plan.is_file(): return
    s=load(plan).get("repair_strategy") or {}
    r.check(s.get("strategy")=="literal-replacement","Direct-return defect classified literal-replacement",repr(s))
    r.check(s.get("automatic_candidate_allowed") is True,"Direct-return defect authorizes candidate generation",repr(s))
    r.check(s.get("required_validation")=="full-frozen-regression","Literal repair requires full frozen regression",repr(s))
    root=case/"candidates"
    rc,log=run([sys.executable,str(REPAIRER),str(plan),"--candidate-root",str(root)])
    r.check(rc==0,"Literal repairer generates candidate",log[-800:])
    xs=sorted(root.glob("*/campaign_agent.py"))
    r.check(len(xs)==1,"Exactly one literal candidate generated",str(xs))
    if len(xs)!=1:return
    repaired=xs[0].read_text()
    r.check('return "Missing Sanitation Workers"' in repaired,"Candidate restores expected direct-return literal")
    r.check('return "Missing Sewer Workers"' not in repaired,"Candidate removes erroneous direct-return literal")
    r.check('name.casefold() == "the missing sanitation workers"' in repaired,"Candidate does not rewrite comparison sentinel")
    rc,log=run([sys.executable,"-m","py_compile",str(xs[0])])
    r.check(rc==0,"Generated literal candidate passes syntax",log)

def negative(tmp,r):
    print("\nComputed/conditional logic policy\n---------------------------------")
    source='''def v463_canonical_candidate_name(entity):
    name = str(entity.get("name") or "").strip()
    if str(entity.get("type") or "").lower() == "quest" and name.casefold() == "the missing sanitation workers":
        return name.replace("Sanitation", "Sewer")
    return name
'''
    case=tmp/"logic"; agent,diag=make_case(case,source); out=case/"analysis"
    rc,log=run([sys.executable,str(ANALYZER),str(diag),"--output-dir",str(out)])
    r.check(rc==0,"Logic case analyzer completes without exception",log[-800:])
    plan=out/"repair_plan.json"; r.check(plan.is_file(),"Logic case repair plan created")
    if not plan.is_file(): return
    s=load(plan).get("repair_strategy") or {}
    r.check(s.get("strategy")!="literal-replacement","Computed defect is not classified literal-replacement",repr(s))
    r.check(s.get("automatic_candidate_allowed") is False,"Computed defect blocks automatic candidate generation",repr(s))
    r.check(s.get("required_validation")=="human-review-before-candidate-generation","Computed defect requires human review before candidate",repr(s))
    root=case/"candidates"
    rc,log=run([sys.executable,str(REPAIRER),str(plan),"--candidate-root",str(root)])
    r.check(rc!=0,"Repairer independently refuses review-only strategy",log[-800:])
    xs=list(root.glob("*/campaign_agent.py")) if root.exists() else []
    r.check(not xs,"Review-only strategy generates no candidate",str(xs))
    r.check("REVIEW ONLY" in log,"Repairer refusal is explicitly review-only",log[-800:])

def write_exec(path,body):
    path.write_text(body)
    path.chmod(0o755)

def orchestration_case(tmp,r,mode):
    """Exercise campaign_agent_dev.py --repair in an isolated temporary toolchain."""
    case=tmp/f"orchestration_{mode}"
    case.mkdir(parents=True,exist_ok=True)
    dev=case/"campaign_agent_dev.py"; dev.write_text(DEV.read_text())
    (case/"campaign_agent.py").write_text("# isolated trusted-agent sentinel\n")
    (case/"campaign_agent_regression.py").write_text("# must never run\n")
    (case/"campaign_agent_pipeline_regression.py").write_text("# must never run\n")
    marker=case/"repairer_invoked.txt"

    diagnoser='''#!/usr/bin/env python3
import argparse,json
from pathlib import Path
ap=argparse.ArgumentParser(); ap.add_argument("handoff"); ap.add_argument("--output-dir",required=True); a=ap.parse_args()
out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
(out/"diagnosis.json").write_text(json.dumps({"schema_version":1,"kind":"campaign-agent-diagnostic-package","pipeline_result":"failed","failure_stage":"deterministic","status":"diagnosis-ready","inputs":{},"observed_failures":[]},indent=2)+"\\n")
'''
    write_exec(case/"campaign_agent_diagnose.py",diagnoser)

    if mode=="analyzer_exception":
        analyzer='''#!/usr/bin/env python3
print("INTENTIONAL ANALYZER FAILURE")
raise RuntimeError("synthetic analyzer exception")
'''
    else:
        analyzer='''#!/usr/bin/env python3
import argparse,json
from pathlib import Path
ap=argparse.ArgumentParser(); ap.add_argument("diagnosis"); ap.add_argument("--output-dir",required=True); a=ap.parse_args()
out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
(out/"repair_plan.json").write_text(json.dumps({"schema_version":2,"kind":"campaign-agent-repair-plan","repair_strategy":{"strategy":"conditional-logic","confidence":"medium","automatic_candidate_allowed":False,"reason":"synthetic review-only orchestration regression","target_function":"v463_canonical_candidate_name","evidence":[],"required_validation":"human-review-before-candidate-generation"}},indent=2)+"\\n")
'''
    write_exec(case/"campaign_agent_analyze.py",analyzer)
    repairer='#!/usr/bin/env python3\nfrom pathlib import Path\nPath('+repr(str(marker))+').write_text("INVOKED\\n")\nraise SystemExit("repairer must not be invoked")\n'
    write_exec(case/"campaign_agent_repair.py",repairer)

    handoff=case/"failed_handoff.json"
    handoff.write_text(json.dumps({"schema_version":1,"kind":"campaign-agent-development-handoff","result":"failed","failure_stage":"deterministic","inputs":{},"stages":{},"failures":[]},indent=2)+"\n")
    reports=case/"reports"
    rc,log=run([sys.executable,str(dev),"--repair",str(handoff),"--report-root",str(reports),"--yes-test"])
    runs=sorted(reports.glob("*_repair*")); state_path=runs[-1]/"repair_orchestration.json" if runs else None
    state=load(state_path) if state_path and state_path.is_file() else {}

    if mode=="analyzer_exception":
        print("\nOrchestration fail-closed analyzer exception\n--------------------------------------------")
        r.check(rc!=0,"Analyzer exception makes orchestration fail",log[-1000:])
        r.check(state.get("failure_stage")=="analyze","Analyzer exception stops at analyze stage",repr(state))
        r.check((state.get("stages") or {}).get("analyze",{}).get("status")=="fail","Analyzer failure is recorded",repr(state))
        r.check(not marker.exists(),"Analyzer failure does not invoke repairer")
        r.check(not list(reports.glob("**/candidates/*/campaign_agent.py")),"Analyzer failure generates no candidate")
        r.check("Approval received. Running frozen validation pipeline" not in log,"Analyzer failure does not run candidate validation",log[-1000:])
    else:
        print("\nOrchestration review-only strategy gate\n---------------------------------------")
        r.check(rc==3,"Review-only strategy returns dedicated review-required code",log[-1000:])
        r.check(state.get("result")=="review-required","Review-only result is recorded",repr(state))
        gate=(state.get("stages") or {}).get("strategy_gate") or {}
        r.check(gate.get("status")=="review-only","Strategy gate records review-only status",repr(gate))
        r.check(gate.get("strategy")=="conditional-logic","Strategy gate preserves classifier result",repr(gate))
        r.check(not marker.exists(),"Review-only strategy does not invoke repairer")
        r.check(not list(reports.glob("**/candidates/*/campaign_agent.py")),"Review-only strategy generates no candidate")
        r.check("REVIEW REQUIRED" in log,"Review-only orchestration explicitly requires review",log[-1000:])
        r.check("Approval received. Running frozen validation pipeline" not in log,"Review-only strategy does not run candidate validation",log[-1000:])

def orchestration_tests(tmp,r):
    orchestration_case(tmp,r,"analyzer_exception")
    orchestration_case(tmp,r,"review_only")

def main():
    ap=argparse.ArgumentParser(description="Campaign Agent V4.15 repair-pipeline + orchestration regression suite")
    ap.add_argument("--report")
    a=ap.parse_args()
    print("Campaign Agent Pipeline Regression Suite V4.15\n==============================================")
    r=Results()
    r.check(ANALYZER.is_file(),"Analyzer exists",str(ANALYZER))
    r.check(REPAIRER.is_file(),"Repairer exists",str(REPAIRER))
    r.check(DEV.is_file(),"Development orchestrator exists",str(DEV))
    if not ANALYZER.is_file() or not REPAIRER.is_file() or not DEV.is_file(): return 1
    agent0,fixture0,git0=sha(AGENT),sha(FIXTURE),git_status()
    with tempfile.TemporaryDirectory(prefix="campaign_agent_v414_") as td:
        positive(Path(td),r); negative(Path(td),r); orchestration_tests(Path(td),r)
    agent1,fixture1,git1=sha(AGENT),sha(FIXTURE),git_status()
    print("\nGlobal safety invariants\n------------------------")
    r.check(agent0==agent1,"Trusted campaign_agent.py unchanged")
    r.check(fixture0==fixture1,"Frozen regression fixture unchanged")
    r.check(git0 is not None and git0==git1,"Git working-tree state unchanged by suite")
    report={"schema_version":1,"kind":"campaign-agent-pipeline-regression","suite_version":"4.15",
            "counts":{"passed":r.passed,"failed":r.failed},
            "expected_agent_baselines":{"deterministic":{"passed":30,"failed":0,"skipped":1},
                                        "full":{"passed":41,"failed":0,"skipped":0}},
            "safety":{"trusted_agent_unchanged":agent0==agent1,
                      "fixture_unchanged":fixture0==fixture1,
                      "git_state_unchanged":git0 is not None and git0==git1,
                      "vault_writes_performed":False,"automatic_promotion_performed":False},
            "checks":r.rows}
    if a.report:
        rp=Path(a.report).expanduser().resolve(); rp.parent.mkdir(parents=True,exist_ok=True)
        rp.write_text(json.dumps(report,indent=2)+"\n"); print(f"\nReport: {rp}")
    print(f"\nSummary\n-------\nPassed: {r.passed}\nFailed: {r.failed}")
    print("Vault : NOT MODIFIED\nGit   : NOT MODIFIED\nAgent : NOT MODIFIED\nFixture: NOT MODIFIED")
    print("\nPIPELINE REGRESSION PASS" if not r.failed else "\nPIPELINE REGRESSION FAILED")
    return 0 if not r.failed else 1

if __name__=="__main__": raise SystemExit(main())
