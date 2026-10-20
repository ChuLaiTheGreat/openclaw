#!/usr/bin/env bash
# Independent contributor test infrastructure; never canonical upstream approval.
# Bootstrap owns public source checkout, frozen dependencies and native image build.
set -Eeuo pipefail
umask 077
PHASE=${1:-help}
case "$PHASE" in
  help) echo 'Usage: portable-package-proof.sh verify|mounts|plan|package|registry|survivor|all'; exit 0 ;;
  verify|mounts|plan|package|registry|survivor|all) ;;
  *) echo 'Unknown phase' >&2; exit 2 ;;
esac
die() { echo "$*" >&2; exit 1; }
: "${TOOLING_ROOT:?}" "${CANDIDATE_ROOT:?}" "${PROOF_ROOT:?}" "${PNPM_TOOLS:?}"
: "${IMAGE_ID:?}" "${IMAGE_PROVENANCE_FILE:?}" "${RUNNER_TEMP:?}"
TOOLING=5a0b5006d5fcb72f238c3c587395bf38591971ad
SELECTED=09517e7ab48747e2935ffbab41d3c90d2bde4202
EXPECTED_TREE=fb409e5571e51267017a43b478f9052f5ce886c3
EXPECTED_PARENTS="eb3874c1d23b9a1bc6f424148f793d991b6c56f8 94dacc48dfb325197481e95cc0fe3fd863eec645"
DOCKERFILE_SHA=b2c59a849429c0424fd6c95c82dc373c0261893121fcc4faeebf954c7c498549
BASE_IMAGE=node:24-trixie-slim@sha256:7f486cabe892bff8fef04c0f7da8dba84a560be3e4e2740b7a86afc57ebec315
PNPM_SHA=f1cdef3cc3805f39d64ab2fbae7d9f263d8f38de6a1f7f0fd07b5bc09f74c415
[[ "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Native image ID must be explicit'
[[ -z "${FINAL_PACKAGE_SHA:-}" || "$FINAL_PACKAGE_SHA" == "$SELECTED" ]] || die 'Manifest package SHA differs from reviewed public head'
[[ -d "$TOOLING_ROOT" && -d "$CANDIDATE_ROOT" && -d "$PNPM_TOOLS" ]] || die 'Bootstrap paths missing'
mkdir -p "$PROOF_ROOT"
SOURCE=$(realpath -e "$CANDIDATE_ROOT")
HARNESS=$(realpath -e "$TOOLING_ROOT")
PROOF=$(realpath -e "$PROOF_ROOT")
PNPM_TOOLS=$(realpath -e "$PNPM_TOOLS")
RUNNER_TEMP=$(realpath -e "$RUNNER_TEMP")
IMAGE_PROVENANCE_FILE=$(realpath -e "$IMAGE_PROVENANCE_FILE")
NODE=$(command -v node)
NODE=$(realpath -e "$NODE")
HOST_NODE_DIR=$(dirname "$NODE")
[[ -x "$NODE" && -x "$PNPM_TOOLS/pnpm" ]] || die 'Bootstrap native Node/pnpm missing'
[[ "$PROOF" == "$RUNNER_TEMP/"* ]] || die 'Proof state must be job-private RUNNER_TEMP'
OUTPUT="$PROOF/artifacts"
RUN_KEY="${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-0}"
[[ "$RUN_KEY" =~ ^[0-9]+-[0-9]+$ ]] || die 'Invalid task run identity'
[[ -d "$SOURCE/.git" && -d "$HARNESS/.git" ]] || die 'Use independent public checkouts, not shared worktree metadata'
[[ -d "$SOURCE/node_modules" && -d "$HARNESS/node_modules" ]] || die 'Bootstrap frozen dependency installation missing'
"$NODE" - "$SOURCE" "$HARNESS" "$PROOF" "$PNPM_TOOLS" "$IMAGE_PROVENANCE_FILE" "$RUNNER_TEMP" <<'NODE'
const fs=require("node:fs"),path=require("node:path");
const [source,harness,proof,tools,provenance,temp]=process.argv.slice(2);
const roots=[source,harness,proof,tools];
for(let i=0;i<roots.length;i++) for(let j=0;j<roots.length;j++) {
 if(i!==j&&(roots[i]===roots[j]||roots[i].startsWith(roots[j]+path.sep)))
  throw Error("Source, tooling, tools and private proof roots must be disjoint");
}
if(!provenance.startsWith(temp+path.sep)) throw Error("Image provenance must be job-private");
for(const exposed of [source,proof+"/artifacts",proof+"/homes"]) {
 if(provenance===exposed||provenance.startsWith(exposed+path.sep))
  throw Error("Image provenance must not enter a contribution-writable mount");
}
if(!fs.statSync(provenance).isFile()) throw Error("Image provenance missing");
NODE
exec 9>"$PROOF/pipeline.lock"
flock -n 9 || die 'This task proof is already active'
mkdir -p "$PROOF/driver-home" "$PROOF/homes" "$PROOF/containers" "$OUTPUT/package" "$OUTPUT/registry"
[[ ! -e "$PROOF/uncertain-container" ]] || die 'Reconcile prior uncertain task container before proceeding'
ACTIVE_CONTAINER=''
trap 'if [[ -n "$ACTIVE_CONTAINER" ]]; then printf "%s\n" "$ACTIVE_CONTAINER" > "$PROOF/uncertain-container"; fi' EXIT

owned_container() {
  local name=$1 memory=$2 cpus=$3 stem container_id attach_exit native_exit status
  shift 3
  # A successful listing is required; an unavailable Docker API never means absent.
  status=$(docker ps -a --filter "name=^/${name}$" --format '{{.Names}}') || return 1
  [[ -z "$status" ]] || die "Existing task container $name needs reconciliation; not starting again"
  stem="$PROOF/containers/$name.$(date +%s%N).$$"
  ACTIVE_CONTAINER=$name
  printf '%s\n' "$name" > "$stem.intent"
  if ! container_id=$(docker create --init --name "$name" --memory "$memory" \
    --memory-swap "$memory" --cpus "$cpus" --pids-limit 2048 \
    --cap-drop ALL --security-opt no-new-privileges "$@"); then
    echo 'Container creation failed or unconfirmed; preserve intent and inspect by name' >&2
    return 1
  fi
  [[ "$container_id" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$container_id" > "$stem.id"
  if docker start --attach "$container_id"; then attach_exit=0; else attach_exit=$?; fi
  printf '%s\n' "$attach_exit" > "$stem.attach-exit"
  docker inspect --type=container "$container_id" > "$stem.inspect.json" || return 1
  if ! native_exit=$("$NODE" - "$stem.inspect.json" "$stem.result.json" "$attach_exit" "$container_id" "$name" <<'NODE'
const fs=require("node:fs");
const [input,output,attached,expectedId,expectedName]=process.argv.slice(2), value=JSON.parse(fs.readFileSync(input,"utf8"))[0];
if(value.Id!==expectedId||value.Name!=="/"+expectedName) throw Error("Task container identity differs");
const state=value.State, joined=state.Status==="exited"&&state.Running===false&&state.Restarting===false&&state.Paused===false&&state.Pid===0&&Number.isInteger(state.ExitCode);
fs.writeFileSync(output,JSON.stringify({containerId:value.Id,name:value.Name,attachExit:Number(attached),
  nativeExit:state.ExitCode,oomKilled:state.OOMKilled,transportUncertain:Number(attached)!==0,joined,state,
  memory:value.HostConfig.Memory,memorySwap:value.HostConfig.MemorySwap,
  nanoCpus:value.HostConfig.NanoCpus,cgroupParent:value.HostConfig.CgroupParent},null,2)+"\n");
if(!joined) process.exit(1);
process.stdout.write(String(state.OOMKilled ? 137 : state.ExitCode));
NODE
  ); then
    echo 'Container still active or result unconfirmed; retained for reconciliation' >&2
    return 1
  fi
  # Preserve a complete native log even if the attached client lost transport.
  # Client status is recorded separately; the settled container owns its exit.
  docker logs "$container_id" > "$stem.native.log" 2>&1 || return 1
  docker rm "$container_id" > "$stem.cleanup" || return 1
  ACTIVE_CONTAINER=''
  return "$native_exit"
}

assert_trusted_driver() {
  [[ $(git -C "$HARNESS" rev-parse HEAD) == "$TOOLING" ]] || die 'Trusted tooling identity changed'
  [[ $(git -C "$SOURCE" rev-parse HEAD) == "$SELECTED" ]] || die 'Candidate public head changed'
  [[ $(git -C "$SOURCE" rev-parse 'HEAD^{tree}') == "$EXPECTED_TREE" ]] || die 'Reviewed candidate tree changed'
  [[ $(git -C "$SOURCE" show -s --format=%P HEAD) == "$EXPECTED_PARENTS" ]] || die 'Reviewed merge parents changed'
  [[ -z $(git -C "$HARNESS" status --porcelain --untracked-files=no) ]] || die 'Trusted tooling tracked changes'
  [[ -z $(git -C "$SOURCE" status --porcelain --untracked-files=no) ]] || die 'Candidate tracked changes'
  for checkout in "$SOURCE" "$HARNESS"; do
    local configured status
    if configured=$(git -C "$checkout" config --local --name-only --get-regexp '(^http\..*\.extraheader$|^credential\.)'); then
      die 'Checkout retains credential configuration; bootstrap must remove it'
    else
      status=$?
      [[ "$status" == 1 ]] || die 'Could not inspect checkout credential settings'
    fi
  done
  [[ $(sha256sum "$PNPM_TOOLS/pnpm" | cut -d' ' -f1) == "$PNPM_SHA" ]] || die 'Standalone pnpm bytes changed'
  "$NODE" - "$HARNESS" "$SOURCE" > "$PROOF/toolchain-verification.json" <<'NODE'
const fs=require("node:fs"),{createRequire}=require("node:module");
const [harness,source]=process.argv.slice(2);
const semver=createRequire(harness+"/package.json")("semver");
const manifests=[harness,source].map(root=>JSON.parse(fs.readFileSync(root+"/package.json","utf8")));
if(manifests.some(m=>!m.packageManager.startsWith("pnpm@12.12.1+sha512.")||!semver.satisfies(process.versions.node,m.engines.node)))
 throw Error("Host Node/pnpm does not satisfy exact source/tooling manifests");
console.log(JSON.stringify({hostNode:process.version,nodeRanges:manifests.map(m=>m.engines.node),pnpmPins:manifests.map(m=>m.packageManager)}));
NODE
  [[ $(cd "$HARNESS"; env -i PATH="$PNPM_TOOLS:$HOST_NODE_DIR:/usr/bin:/bin" HOME="$PROOF/driver-home" "$PNPM_TOOLS/pnpm" --version) == 12.12.1 ]] || die 'Native pnpm version changed'
}

driver() {
  assert_trusted_driver
  env -i PATH="$PNPM_TOOLS:$HOST_NODE_DIR:/usr/local/bin:/usr/bin:/bin" HOME="$PROOF/driver-home" \
    CI=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 OPENCLAW_DOCKER_E2E_REPO_ROOT="$SOURCE" \
    OPENCLAW_DOCKER_E2E_TRUSTED_HARNESS_DIR="$HARNESS" \
    OPENCLAW_DOCKER_E2E_SELECTED_SHA="$SELECTED" \
    OPENCLAW_DOCKER_ALL_LANES=published-upgrade-survivor \
    OPENCLAW_DOCKER_ALL_LIVE_MODE=skip OPENCLAW_DOCKER_ALL_TIMINGS=0 \
    OPENCLAW_UPGRADE_SURVIVOR_BASELINE_SPECS=openclaw@2026.10.5 \
    OPENCLAW_UPGRADE_SURVIVOR_SCENARIOS=sqlite-volume "$@"
}

isolated() {
  local phase=$1 workdir=$SOURCE
  shift
  mkdir -p "$PROOF/homes/$phase"
  local -a mounts=(
    --mount "type=bind,src=$PNPM_TOOLS,dst=$PNPM_TOOLS,readonly"
    --mount "type=bind,src=$HARNESS,dst=$HARNESS,readonly"
    --mount "type=bind,src=$SOURCE,dst=$SOURCE"
    --mount "type=bind,src=$SOURCE/.git,dst=$SOURCE/.git,readonly"
    --mount "type=bind,src=$PROOF/homes/$phase,dst=/home/appuser"
  )
  case "$phase" in
    build|mountcheck) mounts+=(--mount "type=bind,src=$OUTPUT/package,dst=$OUTPUT/package") ;;
    registry) mounts+=(--mount "type=bind,src=$OUTPUT/registry,dst=$OUTPUT/registry") ;;
    *) die 'Unexpected contribution phase' ;;
  esac
  # Contribution receives neither host HOME/auth nor Docker; receipt paths stay outside mounts.
  owned_container "fork-package124-$RUN_KEY-$phase" 12g 4 \
    --network bridge --user "$(id -u):$(id -g)" \
    "${mounts[@]}" \
    --workdir "$workdir" -e "PATH=$PNPM_TOOLS:/usr/local/bin:/usr/bin:/bin" \
    -e HOME=/home/appuser -e XDG_CACHE_HOME=/home/appuser/.cache \
    -e GIT_OPTIONAL_LOCKS=0 -e GIT_CONFIG_GLOBAL=/dev/null -e GIT_CONFIG_NOSYSTEM=1 \
    -e GIT_TERMINAL_PROMPT=0 -e CI=1 -e NODE_OPTIONS=--max-old-space-size=6144 \
    -e OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB=6144 \
    -e GOMEMLIMIT=4GiB -e GOMAXPROCS=4 -e GOGC=30 \
    "$IMAGE_ID" "$@"
}

verify_image() {
  assert_trusted_driver
  [[ $(docker image inspect "$IMAGE_ID" --format '{{.Id}}') == "$IMAGE_ID" ]] || die 'Native bare image identity differs'
  [[ $(sha256sum "$HARNESS/scripts/e2e/Dockerfile" | cut -d' ' -f1) == "$DOCKERFILE_SHA" ]] || die 'Trusted native Dockerfile bytes changed'
  cmp "$SOURCE/scripts/e2e/Dockerfile" "$HARNESS/scripts/e2e/Dockerfile"
  "$NODE" - "$IMAGE_PROVENANCE_FILE" "$TOOLING" "$DOCKERFILE_SHA" "$IMAGE_ID" "$BASE_IMAGE" > "$PROOF/image-provenance-verification.json" <<'NODE'
const fs=require("node:fs");
const [input,toolingSha,dockerfileSha256,imageId,baseImage]=process.argv.slice(2);
const p=JSON.parse(fs.readFileSync(input,"utf8"));
for(const [key,value] of Object.entries({toolingSha,dockerfileSha256,imageId,baseImage,target:"bare"}))
 if(p[key]!==value) throw Error("Native image provenance differs: "+key);
console.log(JSON.stringify(p));
NODE
  owned_container "fork-package124-$RUN_KEY-image-verify" 512m 1 \
    --network none --mount "type=bind,src=$HARNESS,dst=/trusted-harness,readonly" \
    --entrypoint node "$IMAGE_ID" -e '
const fs=require("node:fs");
if(process.version!=="v24.22.0") throw Error("Pinned native bare Node changed");
const expected=JSON.parse(fs.readFileSync("/trusted-harness/package.json","utf8")).devDependencies.typescript;
const actual=JSON.parse(fs.readFileSync("/opt/openclaw-e2e/node_modules/typescript/package.json","utf8")).version;
if(actual!==expected) throw Error("Native bare TypeScript differs");
for(const rel of ["scripts/prepublish-plugin-registry-artifact.mjs","scripts/e2e/lib/prepublish-plugin-registry.sh","scripts/e2e/lib/plugins/npm-registry-server.mjs","scripts/lib/bounded-response.mjs"])
 if(!fs.readFileSync("/trusted-harness/"+rel).equals(fs.readFileSync("/opt/openclaw-e2e/"+rel))) throw Error("Embedded trusted helper differs: "+rel);
console.log(JSON.stringify({node:process.version,typescript:actual,embeddedHarness:"exact"}));
' > "$PROOF/image-verification.json"
}

mounts() {
  isolated mountcheck node -e '
const fs=require("node:fs"),os=require("node:os"),path=require("node:path");
const [source,harness,tools,proof,output]=process.argv.slice(1);
for(const p of [source,output]) fs.accessSync(p,fs.constants.W_OK);
for(const p of [harness,tools,source+"/.git"]) {
 let denied=false;try{fs.accessSync(p,fs.constants.W_OK);}catch{denied=true;}
 if(!denied) throw Error("Trusted mount writable: "+p);
}
for(const p of ["/var/run/docker.sock",proof+"/containers",proof+"/pipeline.lock",proof+"/image-provenance-verification.json"])
 if(fs.existsSync(p)) throw Error("Private host authority or receipt exposed: "+p);
const scratch=fs.mkdtempSync(path.join(os.tmpdir(),"package-proof-"));
fs.rmSync(scratch,{recursive:true});
console.log(JSON.stringify({source:"writable",phaseOutput:"writable",tooling:"readonly",git:"readonly",tools:"readonly",hostDockerAndReceipts:"absent"}));
' "$SOURCE" "$HARNESS" "$PNPM_TOOLS" "$PROOF" "$OUTPUT/package" > "$PROOF/isolation-verification.json"
}

plan() {
  (cd "$HARNESS"; driver "$NODE" "$HARNESS/scripts/test-docker-all.mjs" --plan-json) > "$PROOF/plan.json"
  "$NODE" -e '
const fs=require("node:fs"); const p=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
if(p.lanes.length!==1 || p.lanes[0].stateScenario!=="upgrade-survivor" || !p.lanes[0].name.includes("sqlite-volume") ||
   !p.needs.package || !p.needs.bareImage || !p.needs.prepublishPluginRegistry || p.credentials.length ||
   !p.requiredPrepublishPluginPackages.includes("@openclaw/codex")) throw Error("Unexpected proof plan");
' "$PROOF/plan.json"
}

assert_package_artifact() {
  "$NODE" - "$OUTPUT/package/openclaw-current.tgz" <<'NODE'
const fs=require("node:fs");
const p=process.argv[2], st=fs.lstatSync(p);
if(!st.isFile()||st.isSymbolicLink()||st.size===0||fs.realpathSync(p)!==p)
 throw Error("Package artifact must be a nonempty regular file inside its phase output");
NODE
}

package() {
  [[ ! -e "$OUTPUT/package/openclaw-current.tgz" ]] || die 'Package already exists; reconcile its receipt before another build'
  isolated build node "$HARNESS/scripts/package-openclaw-for-docker.mjs" \
    --source-dir "$SOURCE" --allow-unreleased-changelog \
    --output-dir "$OUTPUT/package" --output-name openclaw-current.tgz \
    > "$PROOF/package-path.txt" 2> "$PROOF/package.log"
  assert_package_artifact
  sha256sum "$OUTPUT/package/openclaw-current.tgz" > "$PROOF/package.sha256"
  "$NODE" - "$OUTPUT/package/openclaw-current.tgz" "$SELECTED" > "$PROOF/package-artifact.json" <<'NODE'
const fs=require("node:fs"),crypto=require("node:crypto");
const [file,sourceSha]=process.argv.slice(2);
console.log(JSON.stringify({sourceSha,path:file,sizeBytes:fs.statSync(file).size,sha256:crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex"),nativeFullPackageExit:0}));
NODE
}

registry() {
  [[ -s "$PROOF/package.sha256" ]] || die 'Successful full package prerequisite missing'
  assert_package_artifact
  sha256sum --check "$PROOF/package.sha256"
  [[ ! -e "$OUTPUT/registry/prepublish-plugin-registry" ]] || die 'Registry directory exists; reconcile before replacing artifacts'
  isolated registry env OPENCLAW_DOCKER_E2E_REPO_ROOT="$SOURCE" \
    OPENCLAW_DOCKER_E2E_TRUSTED_HARNESS_DIR="$HARNESS" OPENCLAW_DOCKER_E2E_SELECTED_SHA="$SELECTED" \
    OPENCLAW_DOCKER_ALL_LANES=published-upgrade-survivor OPENCLAW_DOCKER_ALL_LIVE_MODE=skip \
    OPENCLAW_DOCKER_ALL_LOG_DIR="$OUTPUT/registry" OPENCLAW_DOCKER_ALL_TIMINGS=0 \
    OPENCLAW_UPGRADE_SURVIVOR_BASELINE_SPECS=openclaw@2026.10.5 \
    OPENCLAW_UPGRADE_SURVIVOR_SCENARIOS=sqlite-volume \
    node "$HARNESS/scripts/test-docker-all.mjs" --prepare-plugin-registry \
    > "$PROOF/registry-result.json" 2> "$PROOF/registry.log"
  driver "$NODE" --input-type=module - "$HARNESS" "$PROOF" "$SELECTED" <<'NODE'
import fs from "node:fs"; import {pathToFileURL} from "node:url";
const [harness,proof,sha]=process.argv.slice(2);
const {validatePrepublishPluginRegistryArtifact}=await import(pathToFileURL(harness+"/scripts/prepublish-plugin-registry-artifact.mjs"));
const result=JSON.parse(fs.readFileSync(proof+"/registry-result.json","utf8"));
const plan=JSON.parse(fs.readFileSync(proof+"/plan.json","utf8"));
const expected=proof+"/artifacts/registry/prepublish-plugin-registry";
if(result.dir!==expected||!fs.lstatSync(expected).isDirectory()||fs.realpathSync(expected)!==expected)
 throw Error("Registry artifact must remain in its exact phase output directory");
for(const entry of fs.readdirSync(expected,{withFileTypes:true}))
 if(!entry.isFile()) throw Error("Registry artifact contains a non-regular entry");
const validated=validatePrepublishPluginRegistryArtifact({artifactDir:result.dir,expectedSourceSha:sha,expectedCandidateVersion:"2026.10.5",expectedManifestSha256:result.manifestSha256,requiredPackages:plan.requiredPrepublishPluginPackages});
fs.writeFileSync(proof+"/registry-validation.json",JSON.stringify({sourceSha:validated.manifest.sourceSha,candidateVersion:validated.manifest.candidateVersion,manifestPath:validated.manifestPath,manifestSha256:validated.manifestSha256,packageCount:validated.manifest.packages.length,requiredPackages:plan.requiredPrepublishPluginPackages,requiredPackageDigests:validated.manifest.packages.filter(entry=>plan.requiredPrepublishPluginPackages.includes(entry.name)).map(({name,version,sha256})=>({name,version,sha256})),nativeValidation:"passed"},null,2)+"\n");
NODE
}

survivor() {
  assert_package_artifact
  sha256sum --check "$PROOF/package.sha256"
  [[ -s "$PROOF/registry-result.json" ]] || die 'Verified registry prerequisite missing'
  (cd "$HARNESS"; driver env \
    OPENCLAW_CURRENT_PACKAGE_TGZ="$OUTPUT/package/openclaw-current.tgz" \
    OPENCLAW_PREPUBLISH_PLUGIN_REGISTRY_DIR="$OUTPUT/registry/prepublish-plugin-registry" \
    OPENCLAW_UPGRADE_SURVIVOR_PUBLISHED_BASELINE=1 \
    OPENCLAW_UPGRADE_SURVIVOR_BASELINE_SPEC=openclaw@2026.10.5 \
    OPENCLAW_UPGRADE_SURVIVOR_CANDIDATE=current OPENCLAW_UPGRADE_SURVIVOR_SCENARIO=sqlite-volume \
    OPENCLAW_UPGRADE_SURVIVOR_ARTIFACT_DIR="$PROOF/sqlite-volume" \
    OPENCLAW_UPGRADE_SURVIVOR_UPDATE_RESTART_MODE=manual OPENCLAW_UPGRADE_SURVIVOR_ROOT_MANAGED_VPS=0 \
    OPENCLAW_UPGRADE_SURVIVOR_LIVE_MODELS= OPENCLAW_UPGRADE_SURVIVOR_LIVE_OPENAI=0 \
    OPENCLAW_UPGRADE_SURVIVOR_E2E_IMAGE="$IMAGE_ID" OPENCLAW_UPGRADE_SURVIVOR_E2E_SKIP_BUILD=1 \
    OPENCLAW_DOCKER_E2E_REQUIRE_LOCAL_IMAGE=1 OPENCLAW_DOCKER_E2E_MEMORY=12g \
    OPENCLAW_DOCKER_E2E_CPUS=4 OPENCLAW_DOCKER_E2E_AVAILABLE_CPUS=4 \
    GOMEMLIMIT=4GiB GOMAXPROCS=4 GOGC=30 \
    bash "$HARNESS/scripts/e2e/upgrade-survivor-docker.sh") > "$PROOF/sqlite-volume.log" 2>&1
}

# Image and source qualification precede every phase; all invokes phases serially.
verify_image
case "$PHASE" in
  verify) ;;
  all) plan; mounts; package; registry; survivor ;;
  *) "$PHASE" ;;
esac

