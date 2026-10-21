"""Trusted finite build driver. Contribution code runs only in secretless Docker."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

PINS = {
    '171258': ('2e1578a9358b236f8762b41de624f65d8c4a9bcf', 'bc5cf24c730da8fc29a7bfb4a971273e258e4336'),
    '178125': ('511587cf101d6e3c14460ba7d90d11df633fc830', '9906b34fdcd93883fe10b1456aab4b15c2c9bb1c'),
    '171315': ('e3c9c0e22ce9b71822c04b2f5efc7e1142276704', '94b0900bb516132108b3d5e8297201b789c8f22f'),
    '178124': ('09517e7ab48747e2935ffbab41d3c90d2bde4202', 'fb409e5571e51267017a43b478f9052f5ce886c3'),
}
pr = sys.argv[1]
head, tree = PINS[pr]
assert os.environ['GITHUB_REPOSITORY'] == 'ChuLaiTheGreat/openclaw'
assert os.environ['GITHUB_RUN_ATTEMPT'] == '1'
root = Path(os.environ['RUNNER_TEMP']) / ('runtime-' + pr)
root.mkdir(mode=0o700)
source, tools, store, cache = [root / x for x in ['source', 'tools', 'store', 'cache']]
proof = Path(os.environ['GITHUB_WORKSPACE']) / 'runtime-export'
proof.mkdir()
active = set()
builder = 'isolated-runtime-builder'
def run(args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)
def capture(args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()
def save(name, value):
    (proof / name).write_text(json.dumps(value, indent=2) + '\n')
def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()
def state(cid):
    return json.loads(capture(['docker', 'inspect', cid]))[0]
def phase(label, command, network):
    name = 'exact-runtime-' + pr + '-' + label
    argv = ['docker', 'create', '--name', name, '--user', str(os.getuid()) + ':' + str(os.getgid()),
            '--memory=9g', '--memory-swap=9g', '--cpus=4', '--pids-limit=512',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--network=' + network,
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m', '-e', 'HOME=/tmp/build-home',
            '-e', 'XDG_CACHE_HOME=/cache', '-e', 'PATH=/tools:/usr/local/bin:/usr/bin:/bin',
            '-e', 'GIT_COMMIT=' + head,
            '-v', str(source) + ':/app', '-v', str(tools) + ':/tools:ro',
            '-v', str(store) + ':/store', '-v', str(cache) + ':/cache', '-w', '/app', image, *command]
    save(label + '-intent.json', {'command': command, 'head': head, 'network': network,
                                 'noCredentials': True, 'memory': '9g', 'cpus': 4})
    cid = capture(argv)
    active.add(cid)
    start = time.time()
    run(['docker', 'start', cid], stdout=subprocess.DEVNULL)
    wait = capture(['docker', 'wait', cid])
    observed = state(cid)
    logs = subprocess.run(['docker', 'logs', cid], capture_output=True, text=True)
    (proof / (label + '.log')).write_text(logs.stdout + logs.stderr)
    result = {'head': head, 'tree': tree, 'state': observed['State'], 'image': observed['Image'],
              'dockerWait': wait, 'seconds': time.time() - start,
              'logSha256': sha(proof / (label + '.log'))}
    save(label + '-result.json', result)
    print(json.dumps({'phase': label, **result}), flush=True)
    assert not observed['State']['Running'] and observed['State']['Pid'] == 0
    run(['docker', 'rm', cid], stdout=subprocess.DEVNULL)
    active.remove(cid)
    assert observed['State']['ExitCode'] == 0 and not observed['State']['OOMKilled']

verdict = 'failed'
try:
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    capacity = {'totalBytes': int(memory['MemTotal'].split()[0]) * 1024,
                'availableBytes': int(memory['MemAvailable'].split()[0]) * 1024,
                'cpus': len(os.sched_getaffinity(0)), 'diskFree': shutil.disk_usage(root).free}
    save('capacity.json', capacity)
    assert capacity['availableBytes'] >= 11 * 1024**3 and capacity['cpus'] >= 4
    assert capacity['diskFree'] >= 30 * 1024**3
    save('retention.json', {'owner': 'exact-runtime-' + pr, 'root': str(root),
                          'payloads': ['source', 'tools', 'store', 'cache'],
                          'lifecycle': 'Finite ephemeral hosted runner, cleanup in finally',
                          'export': 'Public source and runtime only; artifact retention one day'})
    request = urllib.request.Request('https://api.github.com/repos/openclaw/openclaw/pulls/' + pr,
                                     headers={'User-Agent': 'exact-runtime-proof'})
    with urllib.request.urlopen(request, timeout=30) as response:
        current = json.load(response)
    assert current['head']['sha'] == head and current['state'] == 'open' and not current['draft']
    assert current['user']['login'] == 'ChuLaiTheGreat'
    source.mkdir()
    run(['git', 'init', '--quiet', source])
    run(['git', '-C', source, 'remote', 'add', 'origin', 'https://github.com/ChuLaiTheGreat/openclaw.git'])
    run(['git', '-C', source, 'fetch', '--no-tags', '--depth=1', 'origin', head])
    run(['git', '-C', source, 'checkout', '--quiet', '--detach', head])
    assert capture(['git', '-C', source, 'rev-parse', 'HEAD^{tree}']) == tree
    assert sha(source / 'scripts/e2e/Dockerfile') == 'b2c59a849429c0424fd6c95c82dc373c0261893121fcc4faeebf954c7c498549'
    for path in [tools, store, cache]:
        path.mkdir()
    archive = root / 'pnpm.tar.gz'
    urllib.request.urlretrieve('https://github.com/pnpm/pnpm/releases/download/v12.12.1/pnpm-linux-x64.tar.gz', archive)
    assert sha(archive) == 'f61657f53f7891f76ae5cf11e924ee3e9ae1659affcaf7deb9622d4d7ea5637e'
    with tarfile.open(archive) as tar:
        items = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == 'pnpm']
        assert len(items) == 1
        (tools / 'pnpm').write_bytes(tar.extractfile(items[0]).read())
    assert sha(tools / 'pnpm') == 'f1cdef3cc3805f39d64ab2fbae7d9f263d8f38de6a1f7f0fd07b5bc09f74c415'
    (tools / 'pnpm').chmod(0o755)
    run(['docker', 'buildx', 'create', '--name', builder, '--driver', 'docker-container',
         '--driver-opt', 'memory=2g,memory-swap=2g,cpu-period=100000,cpu-quota=200000'])
    run(['docker', 'buildx', 'build', '--builder', builder, '--load', '--target', 'bare',
         '--file', source / 'scripts/e2e/Dockerfile', '--tag', 'exact-runtime-runner', source])
    image = capture(['docker', 'image', 'inspect', 'exact-runtime-runner', '--format', '{{.Id}}'])
    save('image.json', {'id': image, 'nativeDockerfileSha256': sha(source / 'scripts/e2e/Dockerfile')})
    phase('install', ['/tools/pnpm', '--config.manage-package-manager-versions=false', 'install',
                      '--frozen-lockfile', '--ignore-scripts', '--store-dir=/store',
                      '--package-import-method=copy', '--network-concurrency=4', '--child-concurrency=1'], 'bridge')
    phase('build', ['node', '--import', './scripts/tsx.mjs', 'scripts/build-all.mts', 'qaRuntime'], 'none')
    assert not capture(['git', '-C', source, 'status', '--porcelain', '--untracked-files=no'])
    stamps = {name: (source / 'dist' / name).read_text() for name in ['.buildstamp', '.runtime-postbuildstamp']}
    assert all(head in raw for raw in stamps.values())
    assert not any(json.loads(raw).get('sourceInputsDirty', False) for raw in stamps.values())
    save('source-and-stamps.json', {'head': head, 'tree': tree, 'trackedClean': True, 'stamps': stamps,
                                    'profile': 'qaRuntime', 'publishablePackage': False})
    # Public-only bytes. Keep tar symlinks intact; no dereference of outside paths.
    run(['tar', '-czf', proof / 'runtime.tar.gz', '--exclude=source/.git',
         '--exclude=source/.artifacts', '--exclude=source/.cache', '-C', root, 'source'])
    save('runtime-artifact.json', {'pr': int(pr), 'head': head, 'tree': tree,
                                  'sha256': sha(proof / 'runtime.tar.gz'),
                                  'bytes': (proof / 'runtime.tar.gz').stat().st_size,
                                  'scope': 'Exact runtime preparation; no live model or Telegram proof'})
    verdict = 'built'
finally:
    for cid in list(active):
        observed = state(cid)
        if observed['State']['Running']:
            run(['docker', 'stop', '--time', '40', cid], stdout=subprocess.DEVNULL)
        assert not state(cid)['State']['Running']
        run(['docker', 'rm', cid], stdout=subprocess.DEVNULL)
        active.remove(cid)
    subprocess.run(['docker', 'buildx', 'rm', builder], stdout=subprocess.DEVNULL)
    shutil.rmtree(root)
    save('CLOSED.json', {'terminal': True, 'payload_unused': True, 'owner_released': True,
                         'canonical_state_preserved': True, 'outcome': verdict,
                         'ephemeralPayloadRemoved': not root.exists(), 'ownContainersRemoved': not active})
