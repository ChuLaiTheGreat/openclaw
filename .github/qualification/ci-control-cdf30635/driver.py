"""Finite secretless exact-CI base/merge controls on the existing fork runner."""
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

SHAS = {'base': '9bd9b07fe1d59a2fa148da45609c581a033017e5',
        'merge': 'b52838fa656964d11d7e860801ff926edd83d917'}
CASES = {
    'plan': ('test/scripts/ci-changed-node-test-plan.test.ts', 'keeps more than 96 changed tests'),
    'feishu': ('extensions/feishu/src/streaming-card.test.ts', 'rejects oversized streaming card-create JSON'),
    'imap': ('test/scripts/tsdown-config.test.ts', 'routes HTML-only mail and advances the cursor'),
    'levanto': ('extensions/levanto/registration.test.ts', None),
    'codex': ('extensions/codex/src/session-catalog-backoff.test.ts', 'shrinks oversized wire pages'),
    'sdk': ('test/scripts/plugin-sdk-surface-report.test.ts', None),
}
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
    env = {'HOME': '/tmp/ci-home', 'PATH': '/tools/node/bin:/tools/bun/bin:/tools:/usr/local/bin:/usr/bin:/bin',
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
    run(['docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
         '--driver-opt', 'memory=2g,memory-swap=2g,cpu-period=100000,cpu-quota=200000'])
    run(['docker', 'buildx', 'build', '--builder', builder, '--load', '--target', 'bare',
         '--file', root / 'base/scripts/e2e/Dockerfile', '--tag', 'ci-control-cdf30635', root / 'base'])
    image = capture(['docker', 'image', 'inspect', 'ci-control-cdf30635', '--format', '{{.Id}}'])
    save('image.json', {'image': image, 'dockerfileSha256': sha(root / 'base/scripts/e2e/Dockerfile')})
    run(['docker', 'buildx', 'rm', builder])
    builder = None
    (tools / 'bun').mkdir()
    phase('bun-stage', 'base', ['/bin/bash', 'scripts/stage-openclaw-bun.sh', '/bun', 'linux', 'x64'],
          network='bridge', extra_mounts=[(tools / 'bun', '/bun', 'rw')], setup=True)
    pin = json.loads((root / 'base/scripts/lib/openclaw-bun.json').read_text())
    assert sha(tools / 'bun/bin/bun') == pin['artifacts']['linux-x64']['executableSha256']
    save('toolchain.json', {'nodeExecutableSha256': sha(tools / 'node/bin/node'),
                           'pnpmSha256': sha(tools / 'pnpm'), 'bunExecutableSha256': sha(tools / 'bun/bin/bun'), 'bunPin': pin})
    for lane in SHAS:
        source = root / lane
        store = root / ('store-' + lane)
        cache = root / ('cache-' + lane)
        store.mkdir()
        cache.mkdir()
        env = {'NODE_BIN': '/tools/node/bin', 'FROZEN_LOCKFILE': 'true', 'DEPENDENCY_CACHE': 'false',
               'DEPENDENCY_CACHE_HIT': 'false', 'GITHUB_WORKSPACE': '/app', 'GITHUB_ENV': '/tmp/ci-env',
               'PNPM_CONFIG_STORE_DIR': '/store', 'PNPM_CONFIG_CACHE_DIR': '/cache',
               'PNPM_CONFIG_PACKAGE_IMPORT_METHOD': 'copy', 'PNPM_CONFIG_CHILD_CONCURRENCY': '2',
               'PNPM_CONFIG_NETWORK_CONCURRENCY': '8'}
        phase(lane + '-install', lane, ['/bin/bash', '.github/actions/setup-node-env/install-dependencies.sh'],
              network='bridge', extra_env=env, extra_mounts=[(store, '/store', 'rw'), (cache, '/cache', 'rw')], setup=True)
        for name, (file, pattern) in CASES.items():
            label = lane + '-' + name
            report = '/app/.artifacts/' + label + '.json'
            args = ['node', 'scripts/run-vitest.mjs', file, '--maxWorkers=1', '--reporter=json', '--outputFile=' + report]
            if pattern:
                args += ['-t', pattern]
            row = phase(label, lane, args, network='bridge')
            path = source / '.artifacts' / (label + '.json')
            if path.exists():
                shutil.copyfile(path, proof / (label + '.json'))
                native = json.loads(path.read_text())
                row['counts'] = {k: native.get(k) for k in ['numTotalTests', 'numPassedTests', 'numFailedTests', 'numPendingTests', 'success']}
                row['failures'] = [{'name': a.get('fullName'), 'messages': a.get('failureMessages')}
                                   for t in native.get('testResults', []) for a in t.get('assertionResults', []) if a.get('status') == 'failed']
            else:
                row['nativeReportAbsent'] = True
            assert sha(source / file) == json.loads((proof / (lane + '-identity.json')).read_text())['inputs'][file]
            row['trackedSourceUnchanged'] = not capture(['git', '-C', source, 'status', '--porcelain', '--untracked-files=no'])
            assert row['trackedSourceUnchanged']
            results.append(row)
            save('partial-comparison.json', {'commits': SHAS, 'results': results})
    save('comparison.json', {'commits': SHAS, 'results': results, 'scope': 'Focused exact base/merge diagnosis, not whole-shard CI or live product acceptance'})
    outcome = 'compared' if all(not r['state']['OOMKilled'] and not r.get('nativeReportAbsent')
                                and (r.get('counts', {}).get('numPassedTests') or 0)
                                + (r.get('counts', {}).get('numFailedTests') or 0) > 0
                                for r in results) else 'incomplete'
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
assert outcome == 'compared', 'Incomplete comparison has no causal product verdict'
