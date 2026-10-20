#!/usr/bin/env python3
"""Trusted, ephemeral public-runner driver. Never execute contribution code on host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = Path(__file__).resolve().parent
SHA = re.compile(r'^[a-f0-9]{40}$')
BASE_TOOLING = '5a0b5006d5fcb72f238c3c587395bf38591971ad'
E2E_PATHS = {'scripts/e2e/lib/plugin-index-sqlite.mjs', 'scripts/e2e/lib/upgrade-survivor/assertions.mjs', 'scripts/e2e/lib/upgrade-survivor/sqlite-volume.mjs', 'test/scripts/plugin-index-sqlite.test.ts'}
PHASES = {'package178124', 'sdk171315'}
PRIVATE_FIELDS = re.compile(r'(token|secret|password|auth|api.?key|cookie|credential)', re.I)

def public_text(raw):
    # Disposable fixtures can generate auth tokens. Keep them out of public logs.
    # Structured receipts retain counts/digests; sensitive keys/lines are omitted.
    def clean(value):
        if isinstance(value,dict):
            return {key:('[redacted]' if PRIVATE_FIELDS.search(key) and item not in (None,False,'',[],{}) else clean(item)) for key,item in value.items()}
        if isinstance(value,list):return [clean(item) for item in value]
        if isinstance(value,str):
            return '\n'.join('[redacted sensitive fixture line]' if PRIVATE_FIELDS.search(line) else line for line in value.split('\n'))
        return value
    try:return json.dumps(clean(json.loads(raw)),indent=2)
    except (json.JSONDecodeError,TypeError):return clean(raw)


def run(args, **kwargs):
    subprocess.run([str(a) for a in args], check=True, **kwargs)

def capture(args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, **kwargs).strip()

def digest(path):
    return hashlib.file_digest(Path(path).open('rb'), 'sha256').hexdigest()

def selection():
    m = json.loads((HERE / 'proof-manifest.json').read_text())
    phase = m['phase']
    if phase not in PHASES or m['repository'] != 'ChuLaiTheGreat/openclaw' or m['branch'] != 'qualification/openclaw-pr-capacity-20261020':
        raise RuntimeError('Unexpected committed qualification selection')
    c = m['candidates'][phase]
    if not SHA.fullmatch(c['head']) or not SHA.fullmatch(c['tree']) or not SHA.fullmatch(m['toolingSha']):
        raise RuntimeError('Final reviewed public candidate is not pinned; no build authorized')
    if c['pr'] != {'package178124':178124, 'sdk171315':171315}[phase]:
        raise RuntimeError('PR identity mismatch')
    if phase=='package178124':
        tooling=m['packageTooling']
        if tooling['parent']!=BASE_TOOLING or not SHA.fullmatch(tooling['tree']) or type(tooling['pr']) is not int or tooling['pr']<=0:
            raise RuntimeError('Final public E2E tooling provenance is not pinned')
    elif m['toolingSha']!=BASE_TOOLING:
        raise RuntimeError('SDK comparison baseline must remain the original pinned main')
    return m, phase, c

def capacity():
    values = dict(line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
    total = int(values['MemTotal'].split()[0]) * 1024
    available = int(values['MemAvailable'].split()[0]) * 1024
    cpus = len(os.sched_getaffinity(0))
    # The standard hosted runner is a VM; account for any imposed cgroup ancestors.
    cgroup = next((x.split(':',2)[2] for x in Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::')), None)
    hierarchy = Path('/sys/fs/cgroup').resolve()
    if cgroup is not None:
        leaf = (hierarchy / cgroup.lstrip('/')).resolve()
        if leaf.is_relative_to(hierarchy) and leaf.is_dir():
            for parent in [leaf, *leaf.parents]:
                if not parent.is_relative_to(hierarchy): break
                memory = parent / 'memory.max'
                if memory.is_file() and memory.read_text().strip() != 'max': total = min(total, int(memory.read_text().strip()))
                quota = parent / 'cpu.max'
                if quota.is_file():
                    limit, period = quota.read_text().split()
                    if limit != 'max': cpus = min(cpus, int(limit) / int(period))
    disk = shutil.disk_usage(os.environ['RUNNER_TEMP']).free
    receipt = {'effectiveMemoryMiB':total//1048576, 'availableMemoryMiB':available//1048576, 'effectiveCpus':cpus, 'freeDiskMiB':disk//1048576}
    print('CAPACITY',json.dumps(receipt),flush=True)
    if total < 12*1024**3 or available < 12*1024**3 or cpus < 4 or disk < 40*1024**3:
        raise RuntimeError('Runner capacity is insufficient; do not weaken native checks')
    return receipt

def public_candidate(m,c,read_token=None):
    url = f'https://api.github.com/repos/openclaw/openclaw/pulls/{c["pr"]}'
    headers={'Accept':'application/vnd.github+json','User-Agent':'OpenClaw-independent-native-qualification'}
    if read_token: headers['Authorization']='Bearer '+read_token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response: row=json.load(response)
    if row['head']['sha'] != c['head'] or row['head']['repo']['full_name'] != m['repository'] or row['state'] != 'open' or row['draft']:
        raise RuntimeError('Public PR no longer matches the reviewed ready candidate')
    return {'url':row['html_url'],'head':row['head']['sha'],'repository':row['head']['repo']['full_name'],'state':row['state'],'draft':row['draft']}

def checkout(root,repo,head,tree=None):
    root.mkdir()
    run(['git','init','--quiet',root])
    run(['git','-C',root,'remote','add','origin',f'https://github.com/{repo}.git'])
    run(['git','-C',root,'fetch','--no-tags','--depth=2','origin',head])
    run(['git','-C',root,'checkout','--quiet','--detach',head])
    if capture(['git','-C',root,'rev-parse','HEAD']) != head or (tree and capture(['git','-C',root,'rev-parse','HEAD^{tree}']) != tree):
        raise RuntimeError('Checkout identity mismatch')

def verify_package_tooling(manifest,tooling,proof):
    pin=manifest['packageTooling'];head=manifest['toolingSha'];parent=pin['parent']
    if capture(['git','-C',tooling,'show','-s','--format=%P','HEAD'])!=parent:
        raise RuntimeError('E2E tooling must have exactly the original main parent')
    if capture(['git','-C',tooling,'rev-parse','HEAD^{tree}'])!=pin['tree']:
        raise RuntimeError('Reviewed E2E tooling tree changed')
    changed=capture(['git','-C',tooling,'diff','--name-only','--no-renames',parent,head]).splitlines()
    if len(changed)!=len(E2E_PATHS) or set(changed)!=E2E_PATHS:
        raise RuntimeError('E2E tooling changes outside the four reviewed fixture files')
    for name in sorted(E2E_PATHS):
        for revision in [parent,head]:
            row=capture(['git','-C',tooling,'ls-tree',revision,'--',name]).split()
            if len(row)!=4 or row[0]!='100644' or row[1]!='blob' or row[3]!=name:
                raise RuntimeError('E2E tooling path is not an existing regular source file')
    # The exact changed-file set also excludes all transitive plan/dependency changes.
    unchanged=['scripts/e2e/Dockerfile','scripts/test-docker-all.mjs','scripts/lib/docker-e2e-plan.mts','package.json','pnpm-lock.yaml','pnpm-workspace.yaml']
    blobs={}
    for name in unchanged:
        old=capture(['git','-C',tooling,'rev-parse',parent+':'+name]);new=capture(['git','-C',tooling,'rev-parse',head+':'+name])
        if old!=new:raise RuntimeError('Native plan/toolchain changed: '+name)
        blobs[name]=new
    receipt={'head':head,'tree':pin['tree'],'parent':parent,'changedFiles':changed,'unchangedNativeBlobs':blobs,'scope':'Only reviewed E2E bounded projection; native plan, Dockerfile and dependency manifests unchanged'}
    (proof/'tooling-provenance.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('TOOLING_PROVENANCE',json.dumps(receipt),flush=True)
    return receipt

def source_snapshot(source):
    return {'head':capture(['git','-C',source,'rev-parse','HEAD']), 'tree':capture(['git','-C',source,'rev-parse','HEAD^{tree}']), 'trackedStatus':capture(['git','-C',source,'status','--porcelain','--untracked-files=no'])}

def container_phase(label,command,source,proof,image,tools,store,helpers=None,extra_env=None):
    """No credentials/socket. Host receipts are outside every contribution RW mount."""
    source, proof = Path(source), Path(proof)
    phase=proof/label
    phase.mkdir(mode=0o700)
    output=phase/'container-output';output.mkdir(mode=0o700)
    home=phase/'home';home.mkdir(mode=0o700)
    (home/'config/pnpm').mkdir(parents=True)
    (home/'config/pnpm/config.yaml').write_text('storeDir: /store\n')
    cache=phase/'cache';cache.mkdir(mode=0o700)
    initial=source_snapshot(source)
    if initial['trackedStatus']: raise RuntimeError('Tracked source is dirty before '+label)
    name='openclaw-independent-'+label+'-'+str(os.getpid())
    if subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
        raise RuntimeError('Refusing existing container '+name)
    env={'HOME':'/task-home','USERPROFILE':'/task-home','XDG_CONFIG_HOME':'/task-home/config','XDG_CACHE_HOME':'/cache','PNPM_HOME':'/task-home/pnpm-home','npm_config_cache':'/cache/npm','RUNNER_TEMP':'/proof','PATH':'/tools:/usr/local/bin:/usr/bin:/bin','GOMEMLIMIT':'4GiB','GOMAXPROCS':'4','GOGC':'30','NODE_OPTIONS':'--max-old-space-size=6144','OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB':'6144','OPENCLAW_FORMAT_TOOL':'oxfmt'}
    env.update(extra_env or {})
    argv=['docker','create','--name',name,'--network=bridge','--user',f'{os.getuid()}:{os.getgid()}','--memory=12g','--memory-swap=12g','--cpus=4','--pids-limit=512','--cap-drop=ALL','--security-opt=no-new-privileges']
    for key,value in env.items(): argv += ['-e',f'{key}={value}']
    for host,dest,ro in [(source,'/work',False),(source/'.git','/work/.git',True),(tools,'/tools',True),(output,'/proof',False),(home,'/task-home',False),(cache,'/cache',False),(store,'/store',False)]:
        argv += ['--mount',f'type=bind,src={host},dst={dest}'+(',readonly' if ro else '')]
    if helpers: argv += ['--mount',f'type=bind,src={helpers},dst=/helper,readonly']
    argv += ['-w','/work',image,*command]
    (phase/'intent.json').write_text(json.dumps({'command':command,'source':initial,'dockerCreate':argv},indent=2)+'\n')
    created=subprocess.run(argv,text=True,capture_output=True)
    (phase/'create.log').write_text(created.stdout+created.stderr)
    if created.returncode or not re.fullmatch('[a-f0-9]{64}',created.stdout.strip()): raise RuntimeError('Container creation uncertain; preserve '+name)
    cid=created.stdout.strip()
    (phase/'container-id').write_text(cid+'\n')
    start=time.monotonic()
    print('START',label,flush=True)
    with (phase/'console.log').open('w') as log: client=subprocess.run(['docker','start','--attach',cid],stdout=log,stderr=subprocess.STDOUT)
    observed=json.loads(capture(['docker','inspect',cid]))[0]
    state=observed['State']
    settled=state.get('Status')=='exited' and state.get('Pid')==0 and state.get('Running') is False and state.get('Restarting') is False and state.get('Paused') is False and type(state.get('ExitCode')) is int
    if not settled: raise RuntimeError('Container is not durably settled; preserve '+cid)
    final=source_snapshot(source)
    code=137 if state.get('OOMKilled') else state['ExitCode']
    receipt={'phase':label,'command':command,'source':initial,'finalSource':final,'sourceUnchanged':initial==final,'exit':code,'clientExit':client.returncode,'transportUncertain':client.returncode!=0,'state':state,'containerId':cid,'seconds':time.monotonic()-start}
    (phase/'outcome.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('OUTCOME',public_text(json.dumps(receipt)),flush=True)
    # Logs are available in this job, never uploaded as a package/artifact or trusted as commands.
    print(public_text((phase/'console.log').read_text(errors='replace')[-48000:]),flush=True)
    run(['docker','rm',cid],stdout=subprocess.DEVNULL)
    if initial!=final: raise RuntimeError('Tracked source changed during '+label)
    return code

RG_QUALIFICATION_CODE = r"""
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
const [version,expectedSha,expectedSize]=process.argv.slice(1);
const binary='/tools/rg';
if(process.platform!=='linux'||process.arch!=='x64') throw Error('Pinned rg requires linux x64');
if(fs.statSync(binary).size!==Number(expectedSize)||crypto.createHash('sha256').update(fs.readFileSync(binary)).digest('hex')!==expectedSha) throw Error('Pinned rg bytes differ');
const v=spawnSync('rg',['--version'],{encoding:'utf8'});
if(v.error||v.status!==0||v.stdout.split('\n')[0].split(' ')[0]!=='ripgrep'||v.stdout.split('\n')[0].split(' ')[1]!==version) throw Error('Pinned rg version/loader incompatible');
const root=fs.mkdtempSync('/proof/rg-native-flags-');
const roots=['src','extensions','packages','scripts'];
const extensions=['ts','tsx','js','mjs','mts','cts','cjs'];
const expected=[];
for(const dir of roots){
 fs.mkdirSync(path.join(root,dir));
 for(const ext of extensions){const file=`${dir}/fixture.${ext}`;fs.writeFileSync(path.join(root,file),'// synthetic rg contract\n');expected.push(file);}
 fs.writeFileSync(path.join(root,dir,'excluded.md'),'synthetic excluded file\n');
}
const args=['--files','--null','-g','*.{ts,tsx,js,mjs,mts,cts,cjs}',...roots];
const scan=spawnSync('rg',args,{cwd:root,encoding:'utf8'});
if(scan.error||scan.status!==0||!scan.stdout.endsWith('\0')) throw Error('Native rg scan flags failed');
const actual=scan.stdout.split('\0').filter(Boolean).sort();
if(JSON.stringify(actual)!==JSON.stringify(expected.sort())) throw Error('Native rg glob/NUL inventory differs');
console.log(JSON.stringify({qualification:'passed',version:v.stdout.split('\n')[0],binarySha256:expectedSha,binarySize:Number(expectedSize),platform:process.platform,arch:process.arch,nativeFlags:args,matchedFiles:actual.length,pcre2Required:false}));
"""

def prepare_rg(manifest,root,tools,proof):
    """Extract one pinned upstream MUSL executable; never run archive/host code."""
    pin=manifest['ripgrep']
    if pin['version']!='15.2.0':raise RuntimeError('Unreviewed ripgrep version')
    asset='ripgrep-'+pin['version']+'-x86_64-unknown-linux-musl.tar.gz'
    url='https://github.com/BurntSushi/ripgrep/releases/download/'+pin['version']+'/'+asset
    if pin['archiveUrl']!=url:raise RuntimeError('Unexpected official ripgrep URL')
    archive=root/asset;sidecar=root/(asset+'.sha256')
    urllib.request.urlretrieve(url,archive);urllib.request.urlretrieve(url+'.sha256',sidecar)
    if archive.stat().st_size!=pin['archiveBytes'] or digest(archive)!=pin['archiveSha256'] or digest(sidecar)!=pin['sidecarSha256']:
        raise RuntimeError('Pinned ripgrep archive/official checksum differs')
    if sidecar.read_text().split()!=[pin['archiveSha256'],asset]:raise RuntimeError('Official ripgrep checksum tuple differs')
    with tarfile.open(archive) as tar:
        wanted=asset.removesuffix('.tar.gz')+'/rg'
        members=[v for v in tar.getmembers() if v.name==wanted]
        if len(members)!=1 or not members[0].isfile() or members[0].issym() or members[0].size!=pin['binaryBytes']:
            raise RuntimeError('Unexpected ripgrep member/type/size')
        target=tools/'rg';target.write_bytes(tar.extractfile(members[0]).read())
    if target.stat().st_size!=pin['binaryBytes'] or digest(target)!=pin['binarySha256']:raise RuntimeError('Pinned rg executable differs')
    target.chmod(0o755)
    receipt={**pin,'member':wanted,'extracted':'rg only; no host execution','containerQualification':'pending'}
    (proof/'rg-provenance.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print('RG_PROVENANCE',json.dumps(receipt),flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--select',action='store_true');parser.add_argument('--run',choices=sorted(PHASES));args=parser.parse_args()
    m,phase,c=selection()
    if args.select:
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as f: f.write(f'phase={phase}\nhead={c["head"]}\n')
        print(json.dumps({'phase':phase,'head':c['head'],'tree':c['tree']}));return 0
    if args.run!=phase: raise RuntimeError('Requested phase differs from committed enum')
    if os.environ.get('GITHUB_REPOSITORY')!=m['repository'] or os.environ.get('GITHUB_EVENT_NAME')!='push' or os.environ.get('GITHUB_REF')!='refs/heads/'+m['branch'] or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':
        raise RuntimeError('Unexpected actual workflow identity; never spoof GitHub context')
    # Consume the job's read-only token before any subprocess or candidate checkout.
    read_token=os.environ.pop('QUALIFICATION_GITHUB_READ_TOKEN',None)
    cap=capacity();public=public_candidate(m,c,read_token)
    public_tooling=public_candidate(m,{'pr':m['packageTooling']['pr'],'head':m['toolingSha']},read_token) if phase=='package178124' else None
    read_token=None
    root=Path(os.environ['RUNNER_TEMP'])/('openclaw-native-'+phase+'-'+c['head'][:12]);root.mkdir(mode=0o700)
    proof=root/'proof';proof.mkdir(mode=0o700)
    (proof/'capacity.json').write_text(json.dumps(cap)+'\n');(proof/'public-candidate.json').write_text(json.dumps(public)+'\n')
    tooling=root/'tooling';candidate=root/'candidate'
    checkout(tooling,m['repository'] if phase=='package178124' else 'openclaw/openclaw',m['toolingSha']);checkout(candidate,m['repository'],c['head'],c['tree'])
    if phase=='package178124':
        (proof/'public-tooling.json').write_text(json.dumps(public_tooling)+'\n')
        verify_package_tooling(m,tooling,proof)
    if digest(tooling/'scripts/e2e/Dockerfile')!=m['dockerfileSha256']: raise RuntimeError('Native Dockerfile changed')
    tools=root/'tools';tools.mkdir();archive=root/'pnpm.tar.gz'
    urllib.request.urlretrieve('https://github.com/pnpm/pnpm/releases/download/v12.12.1/pnpm-linux-x64.tar.gz',archive)
    if digest(archive)!=m['pnpmArchiveSha256']:raise RuntimeError('pnpm archive digest mismatch')
    with tarfile.open(archive) as tar:
        members=[v for v in tar.getmembers() if v.isfile() and Path(v.name).name=='pnpm']
        if len(members)!=1:raise RuntimeError('Unexpected standalone archive inventory')
        (tools/'pnpm').write_bytes(tar.extractfile(members[0]).read())
    if digest(tools/'pnpm')!=m['pnpmExecutableSha256']:raise RuntimeError('pnpm executable digest mismatch')
    (tools/'pnpm').chmod(0o755)
    iid=root/'native-image-id'
    run(['docker','build','--target','bare','--file',tooling/'scripts/e2e/Dockerfile','--iidfile',iid,tooling])
    image=iid.read_text().strip()
    if not re.fullmatch('sha256:[a-f0-9]{64}',image) or capture(['docker','image','inspect',image,'--format','{{.Id}}'])!=image: raise RuntimeError('Actual native image identity unavailable')
    provenance={'toolingSha':m['toolingSha'],'dockerfileSha256':m['dockerfileSha256'],'target':'bare','imageId':image,'baseImage':m['baseImage']}
    provenance_path=proof/'image-provenance.json';provenance_path.write_text(json.dumps(provenance,indent=2)+'\n');print('IMAGE_PROVENANCE',json.dumps(provenance),flush=True)
    store=root/'candidate-store';store.mkdir()
    tooling_store=root/'tooling-store';tooling_store.mkdir()
    install=['/tools/pnpm','install','--frozen-lockfile','--prefer-offline','--ignore-scripts','--package-import-method=copy','--store-dir=/store','--state-dir=/cache/state','--network-concurrency=4','--child-concurrency=1']
    for name,source in [('candidate-deps',candidate),('tooling-deps',tooling)]:
        code=container_phase(name,install,source,proof,image,tools,store if source==candidate else tooling_store)
        if code:return code
    if phase=='sdk171315':
        prepare_rg(m,root,tools,proof)
        pin=m['ripgrep']
        rg_code=container_phase('sdk-rg-qualification',['node','--input-type=module','-e',RG_QUALIFICATION_CODE,pin['version'],pin['binarySha256'],str(pin['binaryBytes'])],candidate,proof,image,tools,store)
        if rg_code:
            # Refuse an unusable native inventory tool before the costly eight-phase SDK run.
            return rg_code
    env=dict(os.environ,TOOLING_ROOT=str(tooling),CANDIDATE_ROOT=str(candidate),PROOF_ROOT=str(proof/'native-package'),PNPM_TOOLS=str(tools),IMAGE_ID=image,IMAGE_PROVENANCE_FILE=str(provenance_path))
    if phase=='package178124':
        result=subprocess.run(['bash',str(HERE/'portable-package-proof.sh'),'all'],env=env).returncode
        package_proof=proof/'native-package'
        # Keep exact settled receipts and native assertions inspectable in job logs.
        # This output remains text under the outer randomized stop-commands guard.
        receipts=sorted((package_proof/'containers').glob('*.result.json'))[:24]
        receipts += [package_proof/name for name in ['image-verification.json','isolation-verification.json','plan.json','package.sha256','registry-result.json','package-artifact.json','registry-validation.json','package.log','registry.log','sqlite-volume.log']]
        for name in ['summary.json','volume-gateway.json','volume-gateway-restarted.json','volume-doctor-budget.json','phases.jsonl']:
            receipts.append(package_proof/'sqlite-volume'/name)
        # Only the trusted publisher's public projection, never sqlite-volume/diagnostics/raw.json.
        diagnostic_root=package_proof/'native-published-diagnostics'
        if diagnostic_root.is_dir() and not diagnostic_root.is_symlink() and diagnostic_root.resolve().is_relative_to(package_proof.resolve()):
            for name in ['failure.json','summary.json']:
                receipts += sorted(diagnostic_root.glob('upgrade-survivor-*/'+name))[:4]
        for item in receipts:
            if item.is_file() and not item.is_symlink() and item.resolve().is_relative_to(package_proof.resolve()):
                print('PACKAGE_EVIDENCE',item.relative_to(package_proof),flush=True)
                print(public_text(item.read_text(errors='replace')[-24000:]),flush=True)
        print('PACKAGE_SUMMARY',json.dumps({'head':c['head'],'tree':c['tree'],'exit':result}),flush=True)
        return result
    from importlib.util import spec_from_file_location,module_from_spec
    spec=spec_from_file_location('portable_sdk',HERE/'portable-sdk-proof.py');module=module_from_spec(spec);spec.loader.exec_module(module)
    return module.execute(m,c,candidate,proof,image,tools,store,HERE)

if __name__=='__main__':
    # Build/package output is untrusted text, never GitHub workflow instructions.
    token=secrets.token_hex(32)
    if '--select' not in sys.argv: print('::stop-commands::'+token,flush=True)
    try: result=main()
    finally:
        if '--select' not in sys.argv: print('::'+token+'::',flush=True)
    sys.exit(result)
