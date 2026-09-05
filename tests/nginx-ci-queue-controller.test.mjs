import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdir, mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import test from "node:test";
import {fileURLToPath, pathToFileURL} from "node:url";

const candidateRoot = path.resolve(process.env.NGINX_CANDIDATE_ROOT || fileURLToPath(new URL("..", import.meta.url)));
const controllerURL = pathToFileURL(path.join(candidateRoot, "scripts/nginx-ci-queue-controller.mjs"));
const reconcilerPath = path.join(candidateRoot, "scripts/reconcile-nginx-ci-jit.sh");
const launcherPath = path.join(candidateRoot, "scripts/run-nginx-ci-jit-vm.sh");
const {reconcileInterruptedJobs, resourceIdentity, selectAuthorizedJobs} = await import(controllerURL.href);

const repositoryID = 77;
const prBase = () => {
  const association = {number: 9, head: {sha: "a".repeat(40), repo: {id: repositoryID}}, base: {ref: "main", sha: "b".repeat(40), repo: {id: repositoryID}}};
  const run = {id: 101, run_attempt: 2, name: "CI", path: ".github/workflows/ci.yml", event: "pull_request_target", status: "queued", head_sha: "b".repeat(40), repository: {id: repositoryID}, pull_requests: [association]};
  const job = {id: 202, run_id: 101, head_sha: "b".repeat(40), workflow_name: "CI", name: "candidate-policy-and-render", status: "queued", labels: ["self-hosted", "linux", "x64", "makepad-nginx-ci-ephemeral", "makepad-nginx-job-101-2"]};
  return {
    runs: {total_count: 1, workflow_runs: [run]},
    jobsByRun: new Map([["101:2", {total_count: 1, jobs: [job]}]]),
    pullRequests: new Map([[9, {number: 9, state: "open", draft: false, head: {sha: "a".repeat(40), repo: {id: repositoryID}}, base: {ref: "main", sha: "b".repeat(40), repo: {id: repositoryID}}}]]),
  };
};

test("selects the exact queued protected-base same-repository PR job without relying on a nonexistent job attempt field", () => {
  assert.deepEqual(selectAuthorizedJobs({...prBase(), repositoryID}), [{runID: 101, attempt: 2, jobID: 202, event: "pull_request_target", sourceSHA: "a".repeat(40), workflowSHA: "b".repeat(40), pullRequestNumber: 9}]);
});

test("selects an exact protected-main push job so release CI cannot remain queued", () => {
  const value = prBase();
  const run = value.runs.workflow_runs[0];
  run.event = "push";
  run.head_branch = "main";
  run.pull_requests = [];
  value.pullRequests.clear();
  assert.deepEqual(selectAuthorizedJobs({...value, repositoryID}), [{runID: 101, attempt: 2, jobID: 202, event: "push", sourceSHA: "b".repeat(40), workflowSHA: "b".repeat(40), pullRequestNumber: null}]);
});

test("rejects fork, wrong workflow, extra-label, moved-head, and non-queued jobs", () => {
  for (const mutate of [
    (value) => { value.runs.workflow_runs[0].pull_requests[0].head.repo.id = 999; },
    (value) => { value.runs.workflow_runs[0].pull_requests[0].base.sha = "c".repeat(40); },
    (value) => { value.runs.workflow_runs[0].path = ".github/workflows/evil.yml"; },
    (value) => { value.jobsByRun.get("101:2").jobs[0].labels.push("persistent"); },
    (value) => { value.jobsByRun.get("101:2").jobs[0].labels[4] = "makepad-nginx-job-101-3"; },
    (value) => { value.pullRequests.get(9).head.sha = "b".repeat(40); },
    (value) => { value.pullRequests.get(9).state = "closed"; },
    (value) => { value.pullRequests.get(9).draft = true; },
    (value) => { value.jobsByRun.get("101:2").jobs[0].status = "in_progress"; },
  ]) {
    const value = prBase();
    mutate(value);
    assert.deepEqual(selectAuthorizedJobs({...value, repositoryID}), []);
  }
});

test("rejects a non-main push, mismatched job head, and wrong job workflow", () => {
  for (const mutate of [
    (value) => { value.runs.workflow_runs[0].head_branch = "feature"; },
    (value) => { value.jobsByRun.get("101:2").jobs[0].head_sha = "c".repeat(40); },
    (value) => { value.jobsByRun.get("101:2").jobs[0].workflow_name = "Other"; },
  ]) {
    const value = prBase();
    value.runs.workflow_runs[0].event = "push";
    value.runs.workflow_runs[0].head_branch = "main";
    value.runs.workflow_runs[0].pull_requests = [];
    mutate(value);
    assert.deepEqual(selectAuthorizedJobs({...value, repositoryID}), []);
  }
});

test("derives a canonical root-ledger identity for every disposable resource", () => {
  assert.deepEqual(resourceIdentity("d".repeat(32)), {
    id: "d".repeat(32),
    bridge: `ngx${"d".repeat(10)}`,
    directory: `/var/lib/makepad/nginx-ci/jobs/nginx-ci-jit-${"d".repeat(32)}`,
    disk: `/var/lib/makepad/nginx-ci/jobs/nginx-ci-jit-${"d".repeat(32)}/runner.qcow2`,
    firewall: `ngxci_${"d".repeat(32)}`,
    jobRoot: "/var/lib/makepad/nginx-ci/jobs",
    network: `ngxci-${"d".repeat(32)}`,
    runner: `nginx-ci-jit-${"d".repeat(32)}`,
    seed: `/var/lib/makepad/nginx-ci/jobs/nginx-ci-jit-${"d".repeat(32)}/seed.iso`,
    version: 1,
    vm: `nginx-ci-${"d".repeat(32)}`,
  });
  assert.throws(() => resourceIdentity("short"), /128-bit lowercase hex/);
  assert.throws(() => resourceIdentity("d".repeat(32), "/tmp/jobs"), /outside the root-owned/);
});

test("startup reconciliation proves local absence before requesting a registration token", async () => {
  const resources = resourceIdentity("e".repeat(32));
  const state = {version: 1, jobs: {202: {jobID: 202, status: "launching", resources}}};
  const calls = [];
  const persisted = [];
  const count = await reconcileInterruptedJobs({
    state,
    reconcileLocal: async (observed) => { calls.push(["local", observed.id]); },
    issueToken: async () => { calls.push(["token"]); return "fresh-token"; },
    reconcileRegistration: async (observed, token) => { calls.push(["registration", observed.id, token]); },
    persist: async (value) => { persisted.push(structuredClone(value)); },
    now: () => "2026-09-05T12:00:00.000Z",
  });
  assert.equal(count, 1);
  assert.deepEqual(calls, [["local", resources.id], ["token"], ["registration", resources.id, "fresh-token"]]);
  assert.equal(state.jobs[202].status, "failed");
  assert.match(state.jobs[202].failure, /startup reconciliation proved all disposable resources absent/);
  assert.equal(state.jobs[202].reconciledAt, "2026-09-05T12:00:00.000Z");
  assert.equal(persisted.length, 1);
});

test("failed startup cleanup stays launching and is safely retried after reboot", async () => {
  const resources = resourceIdentity("f".repeat(32));
  const state = {version: 1, jobs: {303: {jobID: 303, status: "launching", resources}}};
  let localAttempts = 0;
  let registrationAttempts = 0;
  const persist = async () => {};
  await assert.rejects(reconcileInterruptedJobs({
    state,
    reconcileLocal: async () => { localAttempts += 1; },
    issueToken: async () => "fresh-token",
    reconcileRegistration: async () => { registrationAttempts += 1; throw new Error("GitHub unavailable"); },
    persist,
  }), /still has uncertain disposable resources/);
  assert.equal(state.jobs[303].status, "launching");
  assert.match(state.jobs[303].reconciliationFailure, /GitHub unavailable/);

  await reconcileInterruptedJobs({
    state,
    reconcileLocal: async () => { localAttempts += 1; },
    issueToken: async () => "new-token",
    reconcileRegistration: async () => { registrationAttempts += 1; },
    persist,
  });
  assert.equal(localAttempts, 2, "local absence must be re-proven after a reboot or killed reconciliation");
  assert.equal(registrationAttempts, 2);
  assert.equal(state.jobs[303].status, "failed");
  assert.equal("reconciliationFailure" in state.jobs[303], false);
});

test("missing durable resource identity blocks startup without attempting cleanup", async () => {
  let invoked = false;
  await assert.rejects(reconcileInterruptedJobs({
    state: {version: 1, jobs: {404: {jobID: 404, status: "launching"}}},
    reconcileLocal: async () => { invoked = true; },
    issueToken: async () => "token",
    reconcileRegistration: async () => { invoked = true; },
    persist: async () => {},
  }), /resource identity is missing/);
  assert.equal(invoked, false);
});

test("the host reconciler preserves containment until exact local absence is proven", async () => {
  const source = await readFile(reconcilerPath, "utf8");
  const domain = source.indexOf("virsh list --all");
  const network = source.indexOf("virsh net-list --all");
  const bridge = source.indexOf("ip -j link show");
  const containment = source.indexOf("Preserve containment whenever the VM, network, or bridge remains");
  const firewall = source.indexOf('nft delete table inet "${nft_table}"');
  const disk = source.indexOf('find "${job_directory}" -depth -mindepth 1 -delete');
  assert.ok(domain >= 0 && domain < network && network < bridge && bridge < containment && containment < firewall && firewall < disk);
  assert.match(source, /if \[\[ "\$\{domain_absent\}" == true && "\$\{network_absent\}" == true && "\$\{bridge_absent\}" == true \]\]/);
  assert.match(source, /Retaining the interrupted runner bridge/);
  assert.match(source, /Retaining the interrupted runner firewall/);
  assert.match(source, /Retaining interrupted runner disk and seed/);
  assert.match(source, /interrupted local JIT teardown remains uncertain/);

  const launcher = await readFile(launcherPath, "utf8");
  assert.match(launcher, /NGINX_CI_RESOURCE_ID/);
  assert.match(launcher, /temporary_directory="\$\{job_root\}\/nginx-ci-jit-\$\{resource_id\}"/);
  assert.match(launcher, /virsh net-autostart --disable "\$\{network_name\}"/);
  assert.match(launcher, /virsh autostart --disable "\$\{vm_name\}"/);
  assert.doesNotMatch(launcher, /nginx-ci-jit-XXXXXXXX/);
});

test("local startup reconciliation removes reboot-surviving definitions and keeps containment on query failure", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "nginx-ci-local-reconcile-"));
  const tools = path.join(directory, "tools");
  const state = path.join(directory, "state");
  const log = path.join(directory, "commands.log");
  await mkdir(tools);
  await mkdir(state);
  const resourceID = "b".repeat(32);
  await writeFile(path.join(state, "domain"), `nginx-ci-${resourceID}\n`);
  await writeFile(path.join(state, "network"), `ngxci-${resourceID}\n`);
  await writeFile(path.join(state, "bridge"), `ngx${resourceID.slice(0, 10)}\n`);
  await writeFile(path.join(state, "firewall"), `ngxci_${resourceID}\n`);
  const idTool = path.join(tools, "id");
  const pythonTool = path.join(tools, "python3");
  const virshTool = path.join(tools, "virsh");
  const ipTool = path.join(tools, "ip");
  const nftTool = path.join(tools, "nft");
  await writeFile(idTool, "#!/usr/bin/env bash\n[[ \"${1:-}\" == -u ]] && { echo 0; exit 0; }\nexec /usr/bin/id \"$@\"\n", {mode: 0o755});
  await writeFile(pythonTool, "#!/usr/bin/env bash\nif [[ \"${1:-}\" == - && \"${2:-}\" == /var/lib/makepad/nginx-ci/* ]]; then cat >/dev/null; exit 0; fi\nexec /usr/bin/python3 \"$@\"\n", {mode: 0o755});
  await writeFile(virshTool, `#!/usr/bin/env bash
set -euo pipefail
printf 'virsh %s\\n' "$*" >>"\${COMMAND_LOG}"
[[ "\${FAIL_DOMAIN_QUERY:-0}" == 0 ]] || { [[ "$*" != "list --all --name" ]] || exit 1; }
case "$*" in
  "list --all --name") cat "\${RESOURCE_STATE}/domain" ;;
  "net-list --all --name") cat "\${RESOURCE_STATE}/network" ;;
  destroy*|undefine*) : >"\${RESOURCE_STATE}/domain" ;;
  net-destroy*|net-undefine*) : >"\${RESOURCE_STATE}/network" ;;
esac
`, {mode: 0o755});
  await writeFile(ipTool, `#!/usr/bin/env bash
set -euo pipefail
printf 'ip %s\\n' "$*" >>"\${COMMAND_LOG}"
if [[ "$*" == "-j link show" ]]; then
  name=$(cat "\${RESOURCE_STATE}/bridge")
  if [[ -n "\${name}" ]]; then printf '[{"ifname":"%s"}]\\n' "\${name}"; else printf '[]\\n'; fi
elif [[ "$*" == link\\ delete\\ dev* ]]; then
  : >"\${RESOURCE_STATE}/bridge"
else
  exit 1
fi
`, {mode: 0o755});
  await writeFile(nftTool, `#!/usr/bin/env bash
set -euo pipefail
printf 'nft %s\\n' "$*" >>"\${COMMAND_LOG}"
if [[ "$*" == "list tables" ]]; then
  name=$(cat "\${RESOURCE_STATE}/firewall")
  if [[ -n "\${name}" ]]; then printf 'table inet %s\\n' "\${name}"; fi
elif [[ "$*" == delete\\ table\\ inet* ]]; then
  : >"\${RESOURCE_STATE}/firewall"
else
  exit 1
fi
`, {mode: 0o755});

  const run = (extraEnvironment = {}) => new Promise((resolve) => {
    const child = spawn("/bin/bash", [reconcilerPath, "local"], {
      env: {...process.env, ...extraEnvironment, PATH: `${tools}:/usr/bin:/bin`, COMMAND_LOG: log, RESOURCE_STATE: state, NGINX_CI_RESOURCE_ID: resourceID},
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("exit", (code) => resolve({code, stdout, stderr}));
  });

  try {
    const failure = await run({FAIL_DOMAIN_QUERY: "1"});
    assert.notEqual(failure.code, 0);
    assert.match(failure.stderr, /Cannot query libvirt domains/);
    assert.equal(await readFile(path.join(state, "firewall"), "utf8"), `ngxci_${resourceID}\n`, "firewall must survive uncertain VM state");

    await writeFile(path.join(state, "domain"), `nginx-ci-${resourceID}\n`);
    await writeFile(path.join(state, "network"), `ngxci-${resourceID}\n`);
    await writeFile(path.join(state, "bridge"), `ngx${resourceID.slice(0, 10)}\n`);
    await writeFile(log, "");
    const success = await run();
    assert.equal(success.code, 0, success.stderr);
    assert.match(success.stdout, /VM, network, bridge, firewall, disk, and seed/);
    for (const name of ["domain", "network", "bridge", "firewall"]) assert.equal(await readFile(path.join(state, name), "utf8"), "");
    const commands = await readFile(log, "utf8");
    assert.ok(commands.indexOf("virsh destroy") < commands.indexOf("virsh net-destroy"));
    assert.ok(commands.indexOf("virsh net-undefine") < commands.indexOf("ip link delete"));
    assert.ok(commands.indexOf("ip link delete") < commands.indexOf("nft delete table"));
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
});

test("registration reconciliation proves deletion and fails closed on API deletion error", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "nginx-ci-reconcile-"));
  const tools = path.join(directory, "tools");
  const runnerState = path.join(directory, "runner-state");
  await mkdir(tools);
  await writeFile(runnerState, "55\n");
  const idTool = path.join(tools, "id");
  const ghTool = path.join(tools, "gh");
  await writeFile(idTool, "#!/usr/bin/env bash\n[[ \"${1:-}\" == -u ]] && { echo 0; exit 0; }\nexec /usr/bin/id \"$@\"\n", {mode: 0o755});
  await writeFile(ghTool, "#!/usr/bin/env bash\nset -euo pipefail\nif [[ \" $* \" == *' --method DELETE '* ]]; then\n  [[ \"${FAIL_DELETE:-0}\" == 0 ]] || exit 1\n  : >\"${RUNNER_STATE}\"\n  exit 0\nfi\ncat \"${RUNNER_STATE}\"\n", {mode: 0o755});

  const run = (extraEnvironment = {}) => new Promise((resolve) => {
    const child = spawn("/bin/bash", [reconcilerPath, "registration"], {
      env: {...process.env, ...extraEnvironment, PATH: `${tools}:/usr/bin:/bin`, RUNNER_STATE: runnerState, NGINX_CI_RESOURCE_ID: "a".repeat(32)},
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.once("exit", (code) => resolve({code, stdout, stderr}));
    child.stdin.end(`ghs_${"T".repeat(32)}\n`);
  });

  try {
    await writeFile(runnerState, "55\n56\n");
    const ambiguous = await run();
    assert.notEqual(ambiguous.code, 0);
    assert.match(ambiguous.stderr, /registration identity is ambiguous/);
    assert.equal(await readFile(runnerState, "utf8"), "55\n56\n");

    await writeFile(runnerState, "55\n");
    const failure = await run({FAIL_DELETE: "1"});
    assert.notEqual(failure.code, 0);
    assert.match(failure.stderr, /failed to delete interrupted JIT runner registration/);
    assert.equal(await readFile(runnerState, "utf8"), "55\n");

    const success = await run();
    assert.equal(success.code, 0, success.stderr);
    assert.match(success.stdout, /Proved interrupted JIT runner registration/);
    assert.equal(await readFile(runnerState, "utf8"), "");
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
});

test("the durable controller records resource identity before launch and never retries recorded IDs", async () => {
  const source = await readFile(controllerURL, "utf8");
  assert.match(source, /state\.jobs\[String\(job\.jobID\)\] = \{\.\.\.job, nonce, resources, status: "launching"/);
  assert.match(source, /recordedResourceIDs\.has\(resources\.id\)/);
  assert.match(source, /filter\(\(job\) => !state\.jobs\[String\(job\.jobID\)\]\)/);
  assert.match(source, /\.slice\(0, 1\)/);
  assert.match(source, /await runLauncher\(\{launcher, token, metadata: \{\.\.\.job, nonce, resources\}, environment\}\)/);
  assert.match(source, /issues/);
  assert.match(source, /NGINX_CI_RECONCILER/);
  assert.match(source, /phase: "local"/);
  assert.match(source, /phase: "registration"/);
  assert.match(source, /status === "launching"/);
  assert.match(source, /all remain permanently no-retry/);
  assert.match(source, /host OnFailure channel remains authoritative/);
  assert.match(source, /pull_requests: "read"/);
  assert.match(source, /await handle\.sync\(\)/);
  assert.match(source, /await directory\.sync\(\)/);
  assert.match(source, /controller state file is insecure/);
  assert.match(source, /legacy interrupted job has no exact resource identity/);
});
