#!/usr/bin/env python3
"""Trusted serial SDK phases; all contribution and native build code stays in Docker."""
from pathlib import Path
import json
import subprocess
from importlib.util import spec_from_file_location,module_from_spec

def execute(manifest,candidate,source,proof,image,tools,store,helpers):
    spec=spec_from_file_location('portable_driver',helpers/'portable-proof-bootstrap.py')
    driver=module_from_spec(spec);spec.loader.exec_module(driver)
    # All paths come from actual changes, not a fabricated GitHub event or an allowlist bypass.
    base=manifest['toolingSha']
    driver.run(['git','-C',source,'fetch','--no-tags','origin',base])
    names=driver.capture(['git','-C',source,'diff','--name-only',base,candidate['head'],'--','extensions/']).splitlines()
    lint=[name for name in names if name.endswith(('.ts','.mts','.tsx')) and (source/name).is_file()]
    if not lint:raise RuntimeError('Expected Telegram extension changes are absent')
    commands=[
      ('sdk-exports',['node','--import','./scripts/tsx.mjs','scripts/sync-plugin-sdk-exports.mts','--check']),
      ('sdk-full-build',['pnpm','build']),
      ('sdk-extension-memory',['node','--import','./scripts/tsx.mjs','scripts/profile-extension-memory.mts','--extension','telegram','--skip-combined','--concurrency','1','--json','/proof/extension-memory.json']),
      ('sdk-api-diff',['node','/helper/portable-sdk-api-driver.mjs',candidate['head'],base]),
      ('sdk-extension-lint',['node','scripts/run-oxlint.mjs','--tsconfig','extensions/tsconfig.json',*lint]),
      ('sdk-core-types',['node','scripts/run-tsgo.mjs','-p','tsconfig.core.json','--incremental','--tsBuildInfoFile','.artifacts/tsgo-cache/core.tsbuildinfo']),
      ('sdk-native-boundary',['node','--import','./scripts/tsx.mjs','scripts/check-extension-package-tsc-boundary.mts','--mode=compile']),
    ]
    rows=[]
    build_ok=False
    for label,command in commands:
        if label=='sdk-extension-memory' and not build_ok:
            rows.append({'phase':label,'skipped':'Exact-candidate full build failed; no implicit profiler build'})
            continue
        code=driver.container_phase(label,command,source,proof,image,tools,store,helpers)
        if label=='sdk-full-build':build_ok=code==0
        rows.append({'phase':label,'exit':code})
        (proof/'sdk-summary.json').write_text(json.dumps({'head':candidate['head'],'tree':candidate['tree'],'base':base,'boundarySelection':'Native full selection outside pull_request; actual event is push; no GitHub identity overrides','phases':rows},indent=2)+'\n')
        # Metadata failure invalidates the build premise, but preserve a concrete failure.
        if label=='sdk-exports' and code:break
    print('SDK_SUMMARY',json.dumps(rows),flush=True)
    return next((row['exit'] for row in rows if row.get('exit')),0)
