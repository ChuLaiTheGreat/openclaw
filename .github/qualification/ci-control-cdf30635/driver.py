"""Finite secretless causal qualification of the upstream planner-test repair on the existing fork runner."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request

SHAS = {'base': 'b52838fa656964d11d7e860801ff926edd83d917'}
FILE = 'test/scripts/ci-changed-node-test-plan.test.ts'
CASES = {'plan': (FILE, 'keeps more than 96 changed tests')}
UPSTREAM = '352b23dd3407ac04b60fe00d11d0781cd37e2fa0'
FIXED_SHA = '8f88d1615b329a18bf54b6c0a70fec7fe4178760a12c95104d09c4c60508604a'
assert os.environ['GITHUB_REPOSITORY'] == 'ChuLaiTheGreat/openclaw'
assert os.environ['GITHUB_RUN_ATTEMPT'] == '1'
root = Path(os.environ['RUNNER_TEMP']) / 'ci-control-cdf30635'
proof = Path(os.environ['GITHUB_WORKSPACE']) / 'ci-control-proof-cdf30635'
proof.mkdir()
tools = root / 'tools'
active = set()
builder = 'ci-control-cdf30635-builder'
image = None
outcome = 'failed'
results = []


def save(name, value):
    f = proof / name
    t = f.with_suffix('.tmp')
    t.write_text(json.dumps(value, indent=2) + '\n')
    t.replace(f)


def sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run(args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def capture(args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def phase(label, lane, args, network='none', extra_env=None, extra_mounts=None, setup=False):
    source = root / lane
    # Native staging executes its hash-verified runtime from TMPDIR. Docker's
    # /tmp mount remains noexec; use this phase's owned executable work mount.
    (source / '.artifacts' / ('tmp-' + label)).mkdir(parents=True)
    env = {'HOME': '/tmp/ci-home', 'PATH': '/tools/node/bin:/tools/bun/bin:/tools:/usr/local/bin:/usr/bin:/bin',
           'TMPDIR': '/app/.artifacts/tmp-' + label,
           'OPENCLAW_VITEST_RUNTIME': 'bun', 'OPENCLAW_BUN_PATH': '/tools/bun/bin/bun',
           'OPENCLAW_CI_TEST_RUNTIME_POLICY': 'bun-all', 'OPENCLAW_VITEST_MAX_WORKERS': '1',
           'OPENCLAW_VITEST_WORKER_CACHE': '1',
           'OPENCLAW_VITEST_FS_MODULE_CACHE_PATH': '/app/.artifacts/cache-' + label,
           'GOMEMLIMIT': '2GiB', 'GOMAXPROCS': '2', 'GOGC': '20', 'NO_COLOR': '1'}
    env.update(extra_env or {})
    argv = ['docker', 'create', '--name', 'ci-cdf30635-' + label,
            '--user', f'{os.getuid()}:{os.getgid()}', '--memory=9g', '--memory-swap=9g',
            '--cpus=4', '--pids-limit=512', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--network=' + network, '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m',
            '-v', f'{source}:/app', '-v', f'{tools}:/tools:ro', '-w', '/app']
    for host, target, mode in extra_mounts or []:
        argv += ['-v', f'{host}:{target}:{mode}']
    for key, value in env.items():
        argv += ['-e', key + '=' + value]
    argv += [image, *args]
    save(label + '-intent.json', {'commit': SHAS[lane], 'command': args,
                                'secretless': True, 'memory': '9g', 'cpus': 4, 'network': network})
    cid = capture(argv)
    active.add(cid)
    start = time.time()
    run(['docker', 'start', cid], stdout=subprocess.DEVNULL)
    x = json.loads(capture(['docker', 'inspect', cid]))[0]
    pid = x['State']['Pid']
    parents = []
    if pid:
        relative = Path(f'/proc/{pid}/cgroup').read_text().split('::', 1)[1].strip()
        cg = Path('/sys/fs/cgroup' + relative)
        while True:
            parents.append({'path': str(cg), **{n: (cg / n).read_text().strip()
                            for n in ['memory.high', 'memory.max', 'memory.current'] if (cg / n).exists()}})
            if str(cg) == '/sys/fs/cgroup':
                break
            cg = cg.parent
    save(label + '-effective-cgroups.json', {'parents': parents, 'image': x['Image'], 'pid': pid})
    assert all(int(p[n]) >= 10 * 1024**3 for p in parents[1:]
               for n in ['memory.high', 'memory.max'] if p.get(n, 'max') != 'max')
    waited = capture(['docker', 'wait', cid])
    state = json.loads(capture(['docker', 'inspect', cid]))[0]['State']
    logs = subprocess.run(['docker', 'logs', cid], text=True, capture_output=True)
    (proof / (label + '.log')).write_text(logs.stdout + logs.stderr)
    assert not state['Running'] and state['Pid'] == 0 and not state['Paused']
    run(['docker', 'rm', cid], stdout=subprocess.DEVNULL)
    active.remove(cid)
    row = {'label': label, 'commit': SHAS[lane], 'state': state, 'dockerWait': waited,
           'seconds': time.time() - start, 'containerRemoved': True,
           'logSha256': sha(proof / (label + '.log'))}
    save(label + '-result.json', row)
    print(json.dumps(row), flush=True)
    if setup:
        assert state['ExitCode'] == 0 and not state['OOMKilled'], 'Setup has no product verdict'
    return row


try:
    capacity = {'availableBytes': int(dict(l.split(':', 1) for l in Path('/proc/meminfo').read_text().splitlines())['MemAvailable'].split()[0]) * 1024,
                'cpus': len(os.sched_getaffinity(0)), 'diskFree': shutil.disk_usage(Path(os.environ['RUNNER_TEMP'])).free}
    save('capacity.json', capacity)
    assert capacity['availableBytes'] >= 11 * 1024**3 and capacity['cpus'] >= 4 and capacity['diskFree'] >= 30 * 1024**3
    root.mkdir(mode=0o700)
    tools.mkdir()
    save('retention.json', {'root': str(root), 'owner': 'exact-ci-control-cdf30635',
                          'lifecycle': 'Existing finite ephemeral hosted-runner recipe: finally removes owned containers, builder, fresh public clones, tools, installs and caches; one-day small proof artifact.',
                          'canonicalState': 'Only public pinned source, no client state or credentials'})
    for lane, commit in SHAS.items():
        source = root / lane
        source.mkdir()
        run(['git', 'init', '--quiet', source])
        run(['git', '-C', source, 'remote', 'add', 'origin', 'https://github.com/openclaw/openclaw.git'])
        run(['git', '-C', source, 'fetch', '--no-tags', '--depth=1', 'origin', commit])
        run(['git', '-C', source, 'checkout', '--quiet', '--detach', commit])
        assert capture(['git', '-C', source, 'rev-parse', 'HEAD']) == commit
        save(lane + '-identity.json', {'commit': commit, 'tree': capture(['git', '-C', source, 'rev-parse', 'HEAD^{tree}']),
                                     'inputs': {f: sha(source / f) for f, _ in CASES.values()}})
    archive = root / 'pnpm.tar.gz'
    urllib.request.urlretrieve('https://github.com/pnpm/pnpm/releases/download/v12.12.1/pnpm-linux-x64.tar.gz', archive)
    assert sha(archive) == 'f61657f53f7891f76ae5cf11e924ee3e9ae1659affcaf7deb9622d4d7ea5637e'
    with tarfile.open(archive) as source:
        member = [x for x in source.getmembers() if x.isfile() and Path(x.name).name == 'pnpm']
        assert len(member) == 1
        (tools / 'pnpm').write_bytes(source.extractfile(member[0]).read())
    assert sha(tools / 'pnpm') == 'f1cdef3cc3805f39d64ab2fbae7d9f263d8f38de6a1f7f0fd07b5bc09f74c415'
    (tools / 'pnpm').chmod(0o755)
    archive = root / 'node.tar.xz'
    urllib.request.urlretrieve('https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz', archive)
    with tarfile.open(archive) as source:
        source.extractall(tools, filter='data')
    (tools / 'node-v24.19.0-linux-x64').rename(tools / 'node')
    assert sha(tools / 'node/bin/node') == 'bc17c508ffeed0ec622934f9b7fa72f8e78da65350e63c3eceb56fa688aa5e12'
    # The upstream bare image does not include the native Bun stager's tools.
    # Add them inside this finite test image; never change host tooling.
    diagnostic_dockerfile = root / 'Dockerfile.ci-control'
    diagnostic_dockerfile.write_bytes((root / 'base/scripts/e2e/Dockerfile').read_bytes() + b'''
FROM bare AS ci-control
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip perl && rm -rf /var/lib/apt/lists/*
USER appuser
''')
    run(['docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
         '--driver-opt', 'memory=2g,memory-swap=2g,cpu-period=100000,cpu-quota=200000'])
    run(['docker', 'buildx', 'build', '--builder', builder, '--load', '--target', 'ci-control',
         '--file', diagnostic_dockerfile, '--tag', 'ci-control-cdf30635', root / 'base'])
    image = capture(['docker', 'image', 'inspect', 'ci-control-cdf30635', '--format', '{{.Id}}'])
    save('image.json', {'image': image, 'dockerfileSha256': sha(root / 'base/scripts/e2e/Dockerfile'),
                        'diagnosticDockerfileSha256': sha(diagnostic_dockerfile),
                        'isolatedToolAdditions': ['curl', 'unzip', 'perl']})
    run(['docker', 'buildx', 'rm', builder])
    builder = None
    (tools / 'bun').mkdir()
    phase('bun-stage', 'base', ['/bin/bash', 'scripts/stage-openclaw-bun.sh', '/bun', 'linux', 'x64'],
          network='bridge', extra_mounts=[(tools / 'bun', '/bun', 'rw')], setup=True)
    pin = json.loads((root / 'base/scripts/lib/openclaw-bun.json').read_text())
    assert sha(tools / 'bun/bin/bun') == pin['artifacts']['linux-x64']['executableSha256']
    save('toolchain.json', {'nodeExecutableSha256': sha(tools / 'node/bin/node'),
                           'pnpmSha256': sha(tools / 'pnpm'), 'bunExecutableSha256': sha(tools / 'bun/bin/bun'), 'bunPin': pin})
    lane = 'base'
    source = root / lane
    store, cache = root / 'store-base', root / 'cache-base'
    store.mkdir(); cache.mkdir()
    env = {'NODE_BIN': '/tools/node/bin', 'FROZEN_LOCKFILE': 'true', 'DEPENDENCY_CACHE': 'false',
           'DEPENDENCY_CACHE_HIT': 'false', 'GITHUB_WORKSPACE': '/app', 'GITHUB_ENV': '/tmp/ci-env',
           'PNPM_CONFIG_STORE_DIR': '/store', 'PNPM_CONFIG_CACHE_DIR': '/cache',
           'PNPM_CONFIG_PACKAGE_IMPORT_METHOD': 'copy', 'PNPM_CONFIG_CHILD_CONCURRENCY': '2',
           'PNPM_CONFIG_NETWORK_CONCURRENCY': '8'}
    phase('install', lane, ['/bin/bash', '.github/actions/setup-node-env/install-dependencies.sh'],
          network='bridge', extra_env=env, extra_mounts=[(store, '/store', 'rw'), (cache, '/cache', 'rw')], setup=True)
    def owner_test(label, pattern=None):
        report = source / '.artifacts' / (label + '.json')
        args = ['node', 'scripts/run-vitest.mjs', FILE, '--maxWorkers=1', '--reporter=json',
                '--outputFile=/app/.artifacts/' + label + '.json']
        if pattern: args += ['-t', pattern]
        row = phase(label, lane, args, network='bridge')
        assert not row['state']['OOMKilled'] and report.exists()
        shutil.copyfile(report, proof / (label + '.json'))
        native = json.loads(report.read_text())
        row['counts'] = {k: native.get(k) for k in ['numTotalTests', 'numPassedTests', 'numFailedTests', 'numPendingTests', 'success']}
        row['failures'] = [a for t in native.get('testResults', []) for a in t.get('assertionResults', []) if a.get('status') == 'failed']
        save(label + '-verdict.json', row)
        return row
    red = owner_test('planner-red', CASES['plan'][1])
    assert red['state']['ExitCode'] == 1 and red['counts']['numFailedTests'] == 1
    assert len(red['failures']) == 1 and 'expected 1 to be 2' in '\n'.join(red['failures'][0]['failureMessages'])
    original = (source / FILE).read_text()
    old = '        expect(job.planConcurrency).toBe(ownerJob.planConcurrency);'
    new = '        const serialProjection = isSerialNodeTestProjection(job.groups, ownerJob);\n        expect(job.planConcurrency).toBe(serialProjection ? 1 : ownerJob.planConcurrency);'
    assert original.count(old) == 1
    (source / FILE).write_text(original.replace(old, new))
    assert sha(source / FILE) == FIXED_SHA
    assert capture(['git', '-C', source, 'diff', '--name-only']) == FILE
    assert capture(['git', '-C', source, 'diff', '--numstat']) == '2\t1\t' + FILE
    green = owner_test('planner-owning-file')
    assert green['state']['ExitCode'] == 0 and green['counts']['numPassedTests'] == 77
    assert green['counts']['numFailedTests'] == 0 and green['counts']['numPendingTests'] == 0
    phase('format', lane, ['node_modules/.bin/oxfmt', '--check', FILE], setup=True)
    assert sha(source / FILE) == FIXED_SHA and capture(['git', '-C', source, 'diff', '--name-only']) == FILE
    save('qualification.json', {'passed': True, 'testedMerge': SHAS['base'],
                              'contributionHead': 'd9b6e3ff2055d042be440410524e60beefd884fa',
                              'upstreamRepair': UPSTREAM, 'fixedFixtureSha256': FIXED_SHA,
                              'onlySourceChange': FILE, 'numstat': '2 additions, 1 deletion',
                              'productionUnchanged': True, 'redFailed': 1, 'owningPassed': 77,
                              'owningSkipped': 0, 'formatPassed': True,
                              'workerMemoryIsolationAssertionsRetained': True,
                              'scope': 'Causal verification of the existing upstream fixture repair on the exact failing CI merge. No contribution ref change, API budget approval, whole PR CI or product acceptance.'})
    outcome = 'qualified'

finally:
    for cid in list(active):
        state = json.loads(capture(['docker', 'inspect', cid]))[0]['State']
        if state['Running']:
            run(['docker', 'stop', '--time', '40', cid], stdout=subprocess.DEVNULL)
        assert not json.loads(capture(['docker', 'inspect', cid]))[0]['State']['Running']
        run(['docker', 'rm', cid], stdout=subprocess.DEVNULL)
        active.remove(cid)
    if builder:
        subprocess.run(['docker', 'buildx', 'rm', builder], stdout=subprocess.DEVNULL)
    if image:
        subprocess.run(['docker', 'image', 'rm', 'ci-control-cdf30635'], stdout=subprocess.DEVNULL)
    if root.exists():
        shutil.rmtree(root)
    save('CLOSED.json', {'terminal': True, 'payload_unused': True, 'owner_released': True,
                         'canonical_state_preserved': True, 'outcome': outcome,
                         'ephemeralPayloadRemoved': not root.exists(), 'ownContainersRemoved': not active})
assert outcome == 'qualified', 'Incomplete upstream-fix qualification has no passing verdict'
