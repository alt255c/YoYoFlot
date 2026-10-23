"""Build, verify and publish the complete product on the original dataset."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))
from scripts.download_dataset import prepare_dataset, load_manifest, verify_file, download_archive
from scripts.verify_source_product import check
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint
from yoyoflot.pipeline import run as build_core
from yoyoflot.product.build import run as build_baseline
from yoyoflot.product.assessment import build as build_assessment
from yoyoflot.product.compile import run as compile_product
from yoyoflot.product.report import run as export_report
from yoyoflot.product.reuse import verify_raw_core


def publish_links(entries):
    """Preflight both outputs and roll back all switches if one fails."""
    plans, applied = [], []
    try:
        for path, destination in entries:
            path, destination = Path(path).absolute(), Path(destination).resolve(strict=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            backup = None
            previous = os.readlink(path) if path.is_symlink() else None
            if path.exists() and previous is None:
                markers = [path / name for name in ('run_manifest.json', 'report_manifest.json')]
                manifest = next((p for p in markers if p.is_file()), None)
                if manifest is None:
                    raise ValueError(f'Refusing to replace an unrelated directory: {path}')
                backup = path.with_name(path.name + '.previous-' + fingerprint(manifest)[:16])
                if backup.exists() or backup.is_symlink():
                    raise ValueError(f'Backup already exists; preserve or move it first: {backup}')
            temporary = path.with_name('.' + path.name + '.next-' + os.urandom(8).hex())
            restore = path.with_name('.' + path.name + '.restore-' + os.urandom(8).hex())
            plan = (path, temporary, restore, backup, previous)
            plans.append(plan)
            temporary.symlink_to(os.path.relpath(destination, path.parent), target_is_directory=True)
            if previous is not None:
                restore.symlink_to(previous, target_is_directory=True)
        for plan in plans:
            path, temporary, restore, backup, previous = plan
            if backup is not None:
                os.rename(path, backup)
            applied.append(plan)
            os.replace(temporary, path)
    except BaseException:
        for path, temporary, restore, backup, previous in reversed(applied):
            if backup is not None:
                path.unlink(missing_ok=True)
                os.rename(backup, path)
            elif previous is not None:
                os.replace(restore, path)
            else:
                path.unlink(missing_ok=True)
        raise
    finally:
        for path, temporary, restore, backup, previous in plans:
            temporary.unlink(missing_ok=True)
            restore.unlink(missing_ok=True)


def run(args):
    progress = lambda message: print(message, flush=True)
    artifacts = ROOT / 'artifacts'
    archive = args.archive or ROOT / 'data/downloads/Airlines-All.zip'
    prepare_dataset(args.data_dir, archive=args.archive)
    # prepare_dataset reuses raw files without needing the ZIP. The source
    # assessment also verifies the original ZIP, so ensure that cache exists.
    if not archive.is_file():
        download_archive(archive, load_manifest()['archive'])
    verify_file(archive, load_manifest()['archive'])
    if args.core_snapshot:
        tables, core = verify_raw_core(args.core_snapshot, data_dir=args.data_dir,
            config=ROOT/'configs/sources.yaml', normalization=ROOT/'configs/normalization.yaml',
            resolution=ROOT/'configs/resolution.yaml', reference=ROOT/'data/reference/airports.csv',
            overrides=ROOT/'configs/airports_overrides.yaml', reference_manifest=None)
    else:
        core, _ = build_core(data_dir=args.data_dir, work_dir=args.work_dir,
                            artifacts_dir=artifacts/'core', require_all=True,
                            memory_limit=args.memory_limit, progress=progress)
        tables = artifacts/'core'/core['run_id']/'tables'
    inputs = {'core':fingerprint(tables.parent/'run_manifest.json'),
              'product_config':fingerprint(ROOT/'configs/product.yaml'),
              'scoring':fingerprint(args.scoring), 'python':platform.python_version(),
              'runtime':{name:importlib.metadata.version(name) for name in ('duckdb','pyarrow','pyyaml')},
              'code':{p.relative_to(ROOT).as_posix():fingerprint(p)
                      for p in sorted((ROOT/'src/yoyoflot').rglob('*.py'))},
              'runner':fingerprint(Path(__file__)),
              'marker_config':fingerprint(ROOT/'configs/markers_v2.yaml')}
    key = hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()[:24]
    base_inputs = {name:value for name,value in inputs.items() if name!='scoring'}
    base_key = hashlib.sha256(json.dumps(base_inputs,sort_keys=True).encode()).hexdigest()[:24]
    staging = args.work_dir/'products'/base_key
    staging.mkdir(parents=True, exist_ok=True)
    baseline = staging/'source-product'
    progress('Building verified source factors')
    build_baseline(tables, baseline, ROOT/'configs/product.yaml', ROOT/'data/reference/airports.csv',
                   args.memory_limit, progress)
    build_assessment(baseline, tables, archive, args.data_dir,
                     staging/'source-assessment-v4', args.assessment_memory_limit or args.memory_limit, progress)
    release = artifacts/'releases'/key
    release.mkdir(parents=True, exist_ok=True)
    product = release/'product'
    compile_product(baseline, product, args.scoring)
    progress('Verifying the final ranking and source evidence')
    verification = check(product)
    (release/'verification.json').write_text(json.dumps(verification,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    progress('Exporting the same final ranking used by the website')
    export_report(product, release/'report')
    # Publish only after all preparation, verification and exports succeeded.
    publish_links([(args.report_output, release/'report'), (args.output, product)])
    print(f'Product: {args.output}\nExports: {args.report_output}\nVerification: {release / "verification.json"}',flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=ROOT/'data/raw')
    p.add_argument('--work-dir',type=Path,default=ROOT/'data/work/source-product')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts/source-product')
    p.add_argument('--report-output',type=Path)
    p.add_argument('--archive',type=Path)
    p.add_argument('--core-snapshot',type=Path)
    p.add_argument('--scoring',type=Path,default=ROOT/'configs/scoring.yaml')
    p.add_argument('--memory-limit',default='1GB')
    p.add_argument('--assessment-memory-limit',help='Override memory only for source-factor assessment; keep core caches unchanged')
    args=p.parse_args()
    for name in ('data_dir','work_dir','archive','core_snapshot','scoring'):
        value=getattr(args,name)
        if value is not None: setattr(args,name,value.resolve())
    args.output=args.output.absolute()
    args.report_output=(args.report_output or args.output.with_name(args.output.name+'-report')).absolute()
    # Do not let user-selected output paths swallow source or work directories.
    paths=[args.output,args.report_output,args.data_dir,args.work_dir]
    for i,a in enumerate(paths):
        for b in paths[i+1:]:
            if a==b or a in b.parents or b in a.parents:
                p.error(f'Input, work and output directories must be separate: {a}, {b}')
    args.work_dir.mkdir(parents=True,exist_ok=True)
    os.chdir(ROOT)
    try:
        with OutputLock(args.work_dir/'.project-build.lock','Project build'):
            run(args)
    except (ValueError,OSError,RuntimeError) as error:
        p.exit(1,f'Build failed: {error}\n')


if __name__=='__main__':
    main()
