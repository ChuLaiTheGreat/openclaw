#!/usr/bin/env node
// Private native API-diff driver. No fake GitHub repository/event identity.
import fs from 'node:fs';
import { spawnSync } from 'node:child_process';
const [head,base]=process.argv.slice(2);
if (![head,base].every(value=>/^[a-f0-9]{40}$/.test(value??''))) throw new Error('Exact candidate/base SHAs required');
const repo='/proof/api-driver-repo';
if(fs.existsSync(repo))throw new Error('Refuse existing API driver state');
function run(command,args,cwd='/proof'){
 const result=spawnSync(command,args,{cwd,stdio:'inherit',env:process.env});
 if(result.error)throw result.error;
 if(result.status!==0)process.exit(result.status??125);
}
// Independent public clone owns every writable object and native worktree registration.
// Fetching public missing objects cannot mutate the reviewed source .git mounted read-only.
fs.mkdirSync(repo);
run('git',['init','--quiet',repo]);
run('git',['remote','add','origin','https://github.com/ChuLaiTheGreat/openclaw.git'],repo);
run('git',['fetch','--no-tags','origin',head,base],repo);
run('git',['checkout','--quiet','--detach',head],repo);
// Native renderRevision resolves its child --import tsx from this private repo.
// Reuse only this candidate container's already-frozen dependencies.
fs.symlinkSync('/work/node_modules',repo+'/node_modules','dir');
for(const sha of [head,base])run('git',['cat-file','-e',`${sha}^{commit}`],repo);
run(process.execPath,['--import','/work/scripts/tsx.mjs','/work/scripts/plugin-sdk-api-diff.mts','--base',base,'--head',head,'--json','/proof/api-diff.json','--summary','/proof/api-diff.md'],repo);
