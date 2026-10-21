"""Finite, credential-free Docker qualification on the existing public fork runner."""
import hashlib,json,os,re,shutil,subprocess,tarfile,time,urllib.request
from pathlib import Path
HEAD=os.environ['REPAIR_HEAD'];BASE='e3c9c0e22ce9b71822c04b2f5efc7e1142276704'
assert re.fullmatch('[0-9a-f]{40}',HEAD)
assert os.environ['GITHUB_REPOSITORY']=='ChuLaiTheGreat/openclaw' and os.environ['GITHUB_RUN_ATTEMPT']=='1'
root=Path(os.environ['RUNNER_TEMP'])/'caption-repair-403cb83c'
proof=Path(os.environ['GITHUB_WORKSPACE'])/'caption-proof';proof.mkdir()
export=Path(os.environ['GITHUB_WORKSPACE'])/'caption-runtime';export.mkdir()
source,tools,store,cache=[root/n for n in ['source','tools','store','cache']]
files=['extensions/telegram/src/'+n for n in ['bot-handlers.inbound-authorization.ts','bot-handlers.inbound-pipeline.ts','telegram-ingress-preparation.integration.test.ts']]
active=set();builder='caption-repair-builder';image=None;outcome='failed'
def capture(args):return subprocess.check_output([str(a) for a in args],text=True).strip()
def run(args,**kw):return subprocess.run([str(a) for a in args],check=True,**kw)
def save(name,value):
 f=proof/name;t=f.with_suffix('.tmp');t.write_text(json.dumps(value,indent=2)+'\n');t.replace(f)
def sha(f):
 with Path(f).open('rb') as raw:return hashlib.file_digest(raw,'sha256').hexdigest()
def phase(label,args,network='bridge',expected=0):
 name='caption-403cb83c-'+label
 argv=['docker','create','--name',name,'--user',str(os.getuid())+':'+str(os.getgid()),'--memory=9g','--memory-swap=9g','--cpus=4','--pids-limit=512','--cap-drop=ALL','--security-opt=no-new-privileges','--network='+network,'--tmpfs','/tmp:rw,nosuid,nodev,size=512m','-e','HOME=/tmp/build-home','-e','XDG_CACHE_HOME=/cache','-e','PATH=/tools:/usr/local/bin:/usr/bin:/bin','-e','OPENCLAW_VITEST_RUNTIME=node','-e','OPENCLAW_VITEST_FS_MODULE_CACHE_PATH=/app/.artifacts/modules-'+label,'-e','GIT_COMMIT='+HEAD,'-e','NO_COLOR=1','-v',str(source)+':/app','-v',str(tools)+':/tools:ro','-v',str(store)+':/store','-v',str(cache)+':/cache','-w','/app',image,*args]
 save(label+'-intent.json',{'head':HEAD,'args':args,'network':network,'expectedExit':expected,'noCredentials':True,'memory':'9g','cpus':4})
 cid=capture(argv);active.add(cid);start=time.time();run(['docker','start',cid],stdout=subprocess.DEVNULL)
 current=json.loads(capture(['docker','inspect',cid]))[0];pid=current['State']['Pid'];parents=[]
 if pid:
  relative=Path('/proc/'+str(pid)+'/cgroup').read_text().split('::',1)[1].strip();cg=Path('/sys/fs/cgroup'+relative)
  while True:
   parents.append({'path':str(cg),**{n:(cg/n).read_text().strip() for n in ['memory.high','memory.max','memory.current'] if (cg/n).exists()}})
   if str(cg)=='/sys/fs/cgroup':break
   cg=cg.parent
 save(label+'-effective-cgroups.json',{'pid':pid,'parents':parents,'childMemory':current['HostConfig']['Memory']})
 assert all(int(p[n])>=10*1024**3 for p in parents[1:] for n in ['memory.max','memory.high'] if p.get(n,'max')!='max'),'Hosted parent is not qualified for a 9-GiB child'
 wait=capture(['docker','wait',cid]);observed=json.loads(capture(['docker','inspect',cid]))[0]
 logs=subprocess.run(['docker','logs',cid],text=True,capture_output=True);(proof/(label+'.log')).write_text(logs.stdout+logs.stderr)
 state=observed['State'];save(label+'-result.json',{'head':HEAD,'state':state,'image':observed['Image'],'seconds':time.time()-start,'dockerWait':wait,'logSha256':sha(proof/(label+'.log'))})
 assert not state['Running'] and state['Pid']==0
 run(['docker','rm',cid],stdout=subprocess.DEVNULL);active.remove(cid)
 print(json.dumps({'phase':label,'exit':state['ExitCode'],'oom':state['OOMKilled']}),flush=True)
 assert state['ExitCode']==expected and not state['OOMKilled'],label+' failed; preserve native result'
def tests(label,names,pattern=None,expected=0):
 report='/app/.artifacts/'+label+'.json'
 args=['node','scripts/run-vitest.mjs',*names,'--maxWorkers=1','--reporter=json','--outputFile='+report]
 if pattern:args+=['-t',pattern]
 phase(label,args,expected=expected)
 f=source/'.artifacts'/(label+'.json');assert f.exists(),'No terminal native report'
 shutil.copyfile(f,proof/(label+'.json'));r=json.loads(f.read_text())
 save(label+'-counts.json',{k:r.get(k) for k in ['numTotalTests','numPassedTests','numFailedTests','numPendingTests','success']})
 return r
try:
 mem=dict(l.split(':',1) for l in Path('/proc/meminfo').read_text().splitlines())
 capacity={'availableBytes':int(mem['MemAvailable'].split()[0])*1024,'totalBytes':int(mem['MemTotal'].split()[0])*1024,'cpus':len(os.sched_getaffinity(0)),'diskFree':shutil.disk_usage(Path(os.environ['RUNNER_TEMP'])).free}
 save('capacity.json',capacity);assert capacity['availableBytes']>=11*1024**3 and capacity['cpus']>=4 and capacity['diskFree']>=30*1024**3
 save('retention.json',{'owner':'caption-repair-403cb83c','root':str(root),'payloads':['source','tools','store','cache'],'lifecycle':'Finite ephemeral hosted runner; finally removes owned consumers and payloads','artifacts':'Small native receipts and exact public runtime; one-day hosted artifact retention'})
 root.mkdir(mode=0o700);source.mkdir()
 run(['git','init','--quiet',source]);run(['git','-C',source,'remote','add','origin','https://github.com/ChuLaiTheGreat/openclaw.git'])
 run(['git','-C',source,'fetch','--no-tags','--depth=2','origin',HEAD]);run(['git','-C',source,'checkout','--quiet','--detach',HEAD])
 assert capture(['git','-C',source,'rev-parse','HEAD^'])==BASE
 assert set(capture(['git','-C',source,'diff','--name-only',BASE,HEAD]).splitlines())==set(files)
 tree=capture(['git','-C',source,'rev-parse','HEAD^{tree}'])
 save('source-identity.json',{'head':HEAD,'base':BASE,'tree':tree,'changed':files,'candidateHashes':{n:sha(source/n) for n in files}})
 assert sha(source/'scripts/e2e/Dockerfile')=='b2c59a849429c0424fd6c95c82dc373c0261893121fcc4faeebf954c7c498549'
 for p in [tools,store,cache]:p.mkdir()
 archive=root/'pnpm.tar.gz';urllib.request.urlretrieve('https://github.com/pnpm/pnpm/releases/download/v12.12.1/pnpm-linux-x64.tar.gz',archive)
 assert sha(archive)=='f61657f53f7891f76ae5cf11e924ee3e9ae1659affcaf7deb9622d4d7ea5637e'
 with tarfile.open(archive) as tar:
  members=[m for m in tar.getmembers() if m.isfile() and Path(m.name).name=='pnpm'];assert len(members)==1;(tools/'pnpm').write_bytes(tar.extractfile(members[0]).read())
 assert sha(tools/'pnpm')=='f1cdef3cc3805f39d64ab2fbae7d9f263d8f38de6a1f7f0fd07b5bc09f74c415';(tools/'pnpm').chmod(0o755)
 run(['docker','buildx','create','--name',builder,'--driver','docker-container','--driver-opt','memory=2g,memory-swap=2g,cpu-period=100000,cpu-quota=200000'])
 run(['docker','buildx','build','--builder',builder,'--load','--target','bare','--file',source/'scripts/e2e/Dockerfile','--tag','caption-repair-runner',source])
 image=capture(['docker','image','inspect','caption-repair-runner','--format','{{.Id}}']);save('image.json',{'id':image,'dockerfileSha256':sha(source/'scripts/e2e/Dockerfile')})
 run(['docker','buildx','rm',builder]);builder=None
 phase('install',['/tools/pnpm','--config.manage-package-manager-versions=false','install','--frozen-lockfile','--ignore-scripts','--store-dir=/store','--package-import-method=copy','--network-concurrency=4','--child-concurrency=1'])
 phase('runtime-build',['node','--import','./scripts/tsx.mjs','scripts/build-all.mts','qaRuntime'])
 stamps={n:json.loads((source/'dist'/n).read_text()) for n in ['.buildstamp','.runtime-postbuildstamp']}
 assert all(s['head']==HEAD and s['inputsClean'] for s in stamps.values());save('runtime-stamps.json',stamps)
 originals={n:(source/n).read_bytes() for n in files}
 for n in files[:2]:(source/n).write_bytes(subprocess.check_output(['git','-C',source,'show',BASE+':'+n]))
 assert capture(['git','-C',source,'diff','--name-only',BASE])==files[2]
 red=tests('regression-red',[files[2]],'rejects command-denied DM media',expected=1)
 failures=[a for t in red['testResults'] for a in t['assertionResults'] if a['status']=='failed']
 assert red['numFailedTests']==2 and len(failures)==2
 assert all(all(s in '\n'.join(f['failureMessages']) for s in ['allowed','true','false']) for f in failures)
 for n,raw in originals.items():(source/n).write_bytes(raw)
 assert not capture(['git','-C',source,'status','--porcelain','--untracked-files=no'])
 green=tests('regression-green',[files[2]],'rejects command-denied DM media');assert green['numPassedTests']==2 and green['numFailedTests']==0
 owner=tests('owning-file',[files[2]]);assert owner['numFailedTests']==0 and owner['numPassedTests']>0
 counts={}
 for label,name in [('admission','bot-handlers.inbound-authorization.test.ts'),('pipeline','bot-handlers.inbound-pipeline.test.ts'),('media','bot-handlers.media.test.ts')]:
  r=tests('consumer-'+label,['extensions/telegram/src/'+name]);assert r['numFailedTests']==0 and r['numPassedTests']>0;counts[label]=r['numPassedTests']
 phase('format',['node_modules/.bin/oxfmt','--check',*files])
 phase('typed-lint',['node','scripts/run-oxlint.mjs',*files])
 assert not capture(['git','-C',source,'status','--porcelain','--untracked-files=no'])
 assert {n:sha(source/n) for n in files}==json.loads((proof/'source-identity.json').read_text())['candidateHashes']
 save('qualification.json',{'passed':True,'head':HEAD,'base':BASE,'tree':tree,'baselineFailedCases':2,'greenPassedCases':2,'ownerPassed':owner['numPassedTests'],'consumerPassed':counts,'formatPassed':True,'typedLintPassed':True,'qaRuntimeBuilt':True,'noLiveTelegram':True,'noRealModel':True,'noDeployment':True})
 run(['tar','-czf',export/'runtime.tar.gz','--exclude=source/.git','--exclude=source/.artifacts','--exclude=source/.cache','-C',root,'source'])
 metadata={'pr':171315,'head':HEAD,'tree':tree,'sha256':sha(export/'runtime.tar.gz'),'bytes':(export/'runtime.tar.gz').stat().st_size,'scope':'Exact runtime plus native regression/consumer/typed-lint proof; no live Telegram or model acceptance'}
 (export/'runtime-artifact.json').write_text(json.dumps(metadata,indent=2));save('runtime-artifact.json',metadata)
 shutil.copyfile(Path(os.environ['GITHUB_WORKSPACE'])/'.github/qualification/caption-repair-20261021/native-negative.mjs',source/'.artifacts/native-negative.mjs')
 try:
  phase('native-negative',['node','.artifacts/native-negative.mjs'],network='none')
 finally:
  native=source/'.artifacts/native-negative'
  if native.exists():
   shutil.copytree(native,proof/'native-negative',ignore=shutil.ignore_patterns('state','workspace','plugin'))
 native_result=json.loads((proof/'native-negative/result.json').read_text())
 assert native_result['head']==HEAD and native_result['admissionInvariantPassed'] and native_result['negativeCases']==4
 save('native-negative-qualification.json',native_result)
 outcome='validated'
finally:
 for cid in list(active):
  st=json.loads(capture(['docker','inspect',cid]))[0]['State']
  if st['Running']:run(['docker','stop','--time','40',cid],stdout=subprocess.DEVNULL)
  assert not json.loads(capture(['docker','inspect',cid]))[0]['State']['Running'];run(['docker','rm',cid],stdout=subprocess.DEVNULL);active.remove(cid)
 if builder:subprocess.run(['docker','buildx','rm',builder],stdout=subprocess.DEVNULL)
 if image:subprocess.run(['docker','image','rm','caption-repair-runner'],stdout=subprocess.DEVNULL)
 if root.exists():shutil.rmtree(root)
 save('CLOSED.json',{'terminal':True,'payload_unused':True,'owner_released':True,'canonical_state_preserved':True,'outcome':outcome,'ephemeralPayloadRemoved':not root.exists(),'ownContainersRemoved':not active})
