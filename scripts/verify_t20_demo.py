"""Create a fresh T20 run and independently verify its persisted tables."""
import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from yoyoflot.evaluation.artifact import run
from yoyoflot.evaluation.verify import audit_snapshot
from yoyoflot.models.demo import write_json
from yoyoflot.provenance import fingerprint


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for flag,name in [('--config','evaluation.yaml'),('--if-config','isolation_forest.yaml'),('--rules-config','model.yaml'),('--graph-config','graph.yaml')]:
        parser.add_argument(flag,type=Path,default=ROOT/'configs'/name)
    parser.add_argument('--report',type=Path,default=ROOT/'data/verification/t20_demo_check.json')
    args=parser.parse_args()
    base=ROOT/'data/verification';base.mkdir(parents=True,exist_ok=True)
    fresh=Path(tempfile.mkdtemp(prefix='t20-',dir=base))
    started=time.monotonic()
    report,_=run(args.config,args.if_config,args.rules_config,args.graph_config,work_dir=fresh/'work',artifacts_dir=fresh/'artifacts',progress=lambda line:print(line,flush=True))
    wall=time.monotonic()-started
    path=fresh/'artifacts'/report['run_id']
    started=time.monotonic();audit=audit_snapshot(path);audit_time=time.monotonic()-started
    peak=None
    if sys.platform=='linux':
        import resource
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result=dict(report=report,audit=audit,wall_seconds=round(wall,3),audit_seconds=round(audit_time,3),
        snapshot=path.relative_to(ROOT).as_posix(),run_manifest_sha256=fingerprint(path/'run_manifest.json'),
        peak_rss_kib=peak,peak_rss_scope='whole_fresh_Linux_process_including_independent_audit',platform=platform.platform(),
        timing_scope='run after top-level imports, including final snapshot verification; audit separate')
    args.report.parent.mkdir(parents=True,exist_ok=True);write_json(args.report,result)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
