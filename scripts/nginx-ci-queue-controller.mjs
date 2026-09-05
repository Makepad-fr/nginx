#!/usr/bin/env node
import crypto, { createSign } from "node:crypto";
import { spawn } from "node:child_process";
import { lstat, mkdir, open, readFile, realpath, rename } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const REPOSITORY = "Makepad-fr/nginx";
const WORKFLOW_PATH = ".github/workflows/ci.yml";
const BASE_LABELS = ["self-hosted", "linux", "x64", "makepad-nginx-ci-ephemeral"];
const jobLabel = (run) => `makepad-nginx-job-${run.id}-${run.run_attempt}`;

export const resourceIdentity = (id, jobRoot = "/var/lib/makepad/nginx-ci/jobs") => {
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("JIT resource ID must be 128-bit lowercase hex");
  if (!/^\/var\/lib\/makepad\/nginx-ci\/[A-Za-z0-9._/-]+$/.test(jobRoot) || jobRoot.includes("..") || path.normalize(jobRoot) !== jobRoot) throw new Error("JIT job root is outside the root-owned Nginx CI tree");
  const directory = path.join(jobRoot, `nginx-ci-jit-${id}`);
  return {
    id,
    bridge: `ngx${id.slice(0, 10)}`,
    directory,
    disk: path.join(directory, "runner.qcow2"),
    firewall: `ngxci_${id}`,
    jobRoot,
    network: `ngxci-${id}`,
    runner: `nginx-ci-jit-${id}`,
    seed: path.join(directory, "seed.iso"),
    version: 1,
    vm: `nginx-ci-${id}`,
  };
};

const assertResourceIdentity = (resources) => {
  if (!resources || typeof resources !== "object" || Array.isArray(resources)) throw new Error("durable JIT resource identity is missing");
  const expected = resourceIdentity(resources.id, resources.jobRoot);
  const expectedKeys = Object.keys(expected).sort();
  const actualKeys = Object.keys(resources).sort();
  if (actualKeys.length !== expectedKeys.length || actualKeys.some((key, index) => key !== expectedKeys[index]) || expectedKeys.some((key) => resources[key] !== expected[key])) throw new Error("durable JIT resource identity is not canonical");
  return expected;
};

const required = (name, env = process.env) => {
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const github = async ({ token, method = "GET", endpoint, body, fetchImpl = fetch }) => {
  const response = await fetchImpl(`https://api.github.com${endpoint}`, {
    method,
    headers: {Accept: "application/vnd.github+json", Authorization: `Bearer ${token}`, "Content-Type": "application/json", "User-Agent": "makepad-nginx-ci-controller", "X-GitHub-Api-Version": "2022-11-28"},
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: "error",
    signal: AbortSignal.timeout(30_000),
  });
  const text = await response.text();
  let payload = {};
  if (text) payload = JSON.parse(text);
  if (!response.ok) throw new Error(`GitHub ${method} ${endpoint} failed with ${response.status}`);
  return payload;
};

const appJWT = ({appID, key, now = Date.now()}) => {
  if (!/^[1-9]\d*$/.test(appID)) throw new Error("Launcher App ID must be numeric");
  const issued = Math.floor(now / 1000) - 60;
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${encode({alg: "RS256", typ: "JWT"})}.${encode({iat: issued, exp: issued + 540, iss: appID})}`;
  const signer = createSign("RSA-SHA256");
  signer.update(unsigned);
  signer.end();
  return `${unsigned}.${signer.sign(key, "base64url")}`;
};

export const selectAuthorizedJobs = ({ runs, jobsByRun, pullRequests, repositoryID }) => {
  if (!Array.isArray(runs.workflow_runs) || runs.total_count !== runs.workflow_runs.length) throw new Error("workflow-run response is truncated");
  const selected = [];
  for (const run of runs.workflow_runs) {
    if (!Number.isSafeInteger(run.id) || run.id <= 0 || !Number.isSafeInteger(run.run_attempt) || run.run_attempt <= 0) continue;
    const associations = Array.isArray(run.pull_requests) ? run.pull_requests : [];
    if (run.name !== "CI" || run.path !== WORKFLOW_PATH || run.status !== "queued" || run.repository?.id !== repositoryID || !/^[a-f0-9]{40}$/.test(run.head_sha || "")) continue;
    let sourceSHA;
    let pullRequestNumber = null;
    if (run.event === "pull_request_target") {
      if (associations.length !== 1 || !Number.isSafeInteger(associations[0]?.number)) continue;
      const association = associations[0];
      const pull = pullRequests.get(association.number);
      if (association.head?.repo?.id !== repositoryID || association.base?.repo?.id !== repositoryID || association.base?.ref !== "main" || association.base?.sha !== run.head_sha || !/^[a-f0-9]{40}$/.test(association.head?.sha || "") || pull?.number !== association.number || pull?.state !== "open" || pull?.draft !== false || pull?.head?.sha !== association.head?.sha || pull?.head?.repo?.id !== repositoryID || pull?.base?.repo?.id !== repositoryID || pull?.base?.ref !== "main" || pull?.base?.sha !== run.head_sha) continue;
      sourceSHA = association.head.sha;
      pullRequestNumber = association.number;
    } else if (run.event === "push") {
      if (run.head_branch !== "main") continue;
      sourceSHA = run.head_sha;
    } else {
      continue;
    }
    const response = jobsByRun.get(`${run.id}:${run.run_attempt}`);
    if (!response || !Array.isArray(response.jobs) || response.total_count !== response.jobs.length) throw new Error("workflow-job response is missing or truncated");
    for (const job of response.jobs) {
      const labels = Array.isArray(job.labels) ? job.labels.map((value) => String(value).toLowerCase()).sort() : [];
      const expectedLabels = [...BASE_LABELS, jobLabel(run)].sort();
      if (Number.isSafeInteger(job.id) && job.id > 0 && job.name === "candidate-policy-and-render" && job.status === "queued" && job.run_id === run.id && job.head_sha === run.head_sha && job.workflow_name === "CI" && labels.length === expectedLabels.length && labels.every((value, index) => value === expectedLabels[index])) {
        selected.push({runID: run.id, attempt: run.run_attempt, jobID: job.id, event: run.event, sourceSHA, workflowSHA: run.head_sha, pullRequestNumber});
      }
    }
  }
  return selected.sort((left, right) => left.jobID - right.jobID);
};

const atomicState = async (file, state) => {
  const incoming = `${file}.incoming-${process.pid}-${crypto.randomBytes(8).toString("hex")}`;
  const handle = await open(incoming, "wx", 0o600);
  try {
    await handle.writeFile(`${JSON.stringify(state, null, 2)}\n`);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await rename(incoming, file);
  const directory = await open(path.dirname(file), "r");
  try { await directory.sync(); } finally { await directory.close(); }
};

const secureRootPath = async (file, expectedMode, finalKind = "file") => {
  if (!path.isAbsolute(file) || await realpath(file) !== file) throw new Error(`controller file is not an absolute real path: ${file}`);
  const components = file.split(path.sep).filter(Boolean);
  let current = path.sep;
  for (let index = 0; index < components.length; index += 1) {
    current = path.join(current, components[index]);
    const value = await lstat(current);
    const final = index === components.length - 1;
    const wrongFinalKind = final && (finalKind === "file" ? !value.isFile() : !value.isDirectory());
    if (value.isSymbolicLink() || value.uid !== 0 || (value.mode & 0o022) !== 0 || wrongFinalKind || (!final && !value.isDirectory()) || (final && (value.mode & 0o777) !== expectedMode)) throw new Error(`insecure controller path component: ${current}`);
  }
};

const runLauncher = ({launcher, token, metadata, environment}) => new Promise((resolve, reject) => {
  const child = spawn(launcher, [], {
    env: {...environment, NGINX_CI_RUN_ID: String(metadata.runID), NGINX_CI_RUN_ATTEMPT: String(metadata.attempt), NGINX_CI_JOB_ID: String(metadata.jobID), NGINX_CI_RUN_EVENT: metadata.event, NGINX_CI_HEAD_SHA: metadata.sourceSHA, NGINX_CI_WORKFLOW_SHA: metadata.workflowSHA, NGINX_CI_ATTESTATION_NONCE: metadata.nonce, NGINX_CI_RESOURCE_ID: metadata.resources.id, NGINX_CI_JOB_ROOT: metadata.resources.jobRoot},
    stdio: ["pipe", "inherit", "inherit"],
  });
  child.stdin.end(`${token}\n`);
  child.once("error", reject);
  child.once("exit", (code, signal) => code === 0 && signal === null ? resolve() : reject(new Error(`launcher exited ${code ?? signal}`)));
});

const runReconciler = ({reconciler, phase, resources, token = "", environment}) => new Promise((resolve, reject) => {
  const child = spawn(reconciler, [phase], {
    env: {...environment, NGINX_CI_RESOURCE_ID: resources.id, NGINX_CI_JOB_ROOT: resources.jobRoot},
    stdio: ["pipe", "inherit", "inherit"],
  });
  child.stdin.end(token ? `${token}\n` : "");
  child.once("error", reject);
  child.once("exit", (code, signal) => code === 0 && signal === null ? resolve() : reject(new Error(`reconciler ${phase} exited ${code ?? signal}`)));
});

export const reconcileInterruptedJobs = async ({state, reconcileLocal, reconcileRegistration, issueToken, persist, failureMessage = "controller stopped during launcher execution; startup reconciliation proved all disposable resources absent", now = () => new Date().toISOString()}) => {
  const interrupted = Object.values(state.jobs).filter((value) => value.status === "launching");
  for (const value of interrupted) {
    const resources = assertResourceIdentity(value.resources);
    try {
      // Local teardown must not wait for GitHub or DNS. This immediately stops
      // a surviving guest after a controller SIGKILL; registration cleanup is
      // a separate, retryable phase once a fresh App token is available.
      await reconcileLocal(resources);
      const token = await issueToken();
      await reconcileRegistration(resources, token);
    } catch (error) {
      value.reconciliationAttemptedAt = now();
      value.reconciliationFailure = error instanceof Error ? error.message.slice(0, 200) : "unknown startup reconciliation failure";
      // Keep the entry in launching state. Every controller restart retries
      // the idempotent teardown and no new job may launch while it is uncertain.
      await persist(state);
      throw new Error(`interrupted job ${value.jobID} still has uncertain disposable resources`);
    }
    value.status = "failed";
    value.failure = failureMessage;
    value.finishedAt = now();
    value.reconciledAt = value.finishedAt;
    delete value.reconciliationAttemptedAt;
    delete value.reconciliationFailure;
    await persist(state);
  }
  return interrupted.length;
};

export const controller = async ({environment = process.env, fetchImpl = fetch, once = false} = {}) => {
  if (process.getuid?.() !== 0) throw new Error("queue controller must run as root on the dedicated hypervisor");
  const repositoryID = Number(required("NGINX_CI_REPOSITORY_ID", environment));
  const appID = required("NGINX_CI_LAUNCHER_APP_ID", environment);
  const installationID = required("NGINX_CI_LAUNCHER_APP_INSTALLATION_ID", environment);
  const privateKeyFile = required("NGINX_CI_LAUNCHER_APP_PRIVATE_KEY_FILE", environment);
  const stateDirectory = required("NGINX_CI_CONTROLLER_STATE_DIRECTORY", environment);
  const launcher = required("NGINX_CI_LAUNCHER", environment);
  const reconciler = required("NGINX_CI_RECONCILER", environment);
  const jobRoot = required("NGINX_CI_JOB_ROOT", environment);
  if (!Number.isSafeInteger(repositoryID) || repositoryID <= 0 || !/^[1-9]\d*$/.test(installationID)) throw new Error("repository and installation IDs must be positive integers");
  resourceIdentity("0".repeat(32), jobRoot);
  if (!/^\/var\/lib\/makepad\/nginx-ci\/[A-Za-z0-9._/-]+$/.test(stateDirectory) || stateDirectory.includes("..") || path.normalize(stateDirectory) !== stateDirectory) throw new Error("controller state directory is outside the root-owned Nginx CI tree");
  for (const [file, expectedMode] of [[privateKeyFile, 0o400], [launcher, 0o755], [reconciler, 0o755]]) {
    await secureRootPath(file, expectedMode);
  }
  const key = await readFile(privateKeyFile, "utf8");
  await mkdir(stateDirectory, {recursive: true, mode: 0o700});
  await secureRootPath(stateDirectory, 0o700, "directory");
  const stateFile = path.join(stateDirectory, "jobs.json");
  let state = {version: 1, jobs: {}};
  try {
    const stateInfo = await lstat(stateFile);
    if (!stateInfo.isFile() || stateInfo.isSymbolicLink() || stateInfo.uid !== 0 || (stateInfo.mode & 0o777) !== 0o600 || await realpath(stateFile) !== stateFile) throw new Error("controller state file is insecure");
    state = JSON.parse(await readFile(stateFile, "utf8"));
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  if (state.version !== 1 || !state.jobs || typeof state.jobs !== "object") throw new Error("controller state is invalid");
  const recordedResourceIDs = new Set();
  for (const [key, value] of Object.entries(state.jobs)) {
    if (!/^[1-9]\d*$/.test(key) || !value || String(value.jobID) !== key || !["launching", "completed", "failed"].includes(value.status)) throw new Error("controller job ledger is invalid");
    if (value.resources !== undefined) {
      const resources = assertResourceIdentity(value.resources);
      if (recordedResourceIDs.has(resources.id)) throw new Error("controller ledger reuses a JIT resource identity");
      recordedResourceIDs.add(resources.id);
    }
    if (value.status === "launching" && value.resources === undefined) throw new Error("legacy interrupted job has no exact resource identity; manual hypervisor audit is required");
  }

  const issueLauncherToken = async () => {
    const jwt = appJWT({appID, key});
    const installation = await github({token: jwt, method: "POST", endpoint: `/app/installations/${installationID}/access_tokens`, body: {repositories: ["nginx"], permissions: {actions: "read", contents: "write", issues: "write", organization_self_hosted_runners: "write", pull_requests: "read"}}, fetchImpl});
    if (typeof installation.token !== "string" || !installation.token.startsWith("ghs_")) throw new Error("Launcher App did not issue an installation token");
    return installation.token;
  };

  const reconciledCount = await reconcileInterruptedJobs({
    state,
    reconcileLocal: (resources) => runReconciler({reconciler, phase: "local", resources, environment}),
    reconcileRegistration: (resources, token) => runReconciler({reconciler, phase: "registration", resources, token, environment}),
    issueToken: issueLauncherToken,
    persist: (nextState) => atomicState(stateFile, nextState),
  });
  if (reconciledCount > 0) {
    // Fail exactly once after successful recovery so the independent host
    // alert records the crash. The next supervised start may process new jobs.
    throw new Error(`reconciled ${reconciledCount} interrupted launcher job(s); all remain permanently no-retry`);
  }

  do {
    const token = await issueLauncherToken();
    const runs = await github({token, endpoint: `/repos/${REPOSITORY}/actions/workflows/ci.yml/runs?status=queued&per_page=100`, fetchImpl});
    const jobsByRun = new Map();
    const pullRequests = new Map();
    for (const run of runs.workflow_runs || []) {
      jobsByRun.set(`${run.id}:${run.run_attempt}`, await github({token, endpoint: `/repos/${REPOSITORY}/actions/runs/${run.id}/attempts/${run.run_attempt}/jobs?per_page=100`, fetchImpl}));
      const number = run.pull_requests?.[0]?.number;
      if (Number.isSafeInteger(number) && !pullRequests.has(number)) pullRequests.set(number, await github({token, endpoint: `/repos/${REPOSITORY}/pulls/${number}`, fetchImpl}));
    }
    // A Launcher App token is short lived while a VM may run for 45 minutes.
    // Launch at most one job per token and refresh all authoritative state on
    // the next loop instead of handing an old token to a later queued job.
    const pending = selectAuthorizedJobs({runs, jobsByRun, pullRequests, repositoryID})
      .filter((job) => !state.jobs[String(job.jobID)])
      .slice(0, 1);
    for (const job of pending) {
      const nonce = crypto.randomBytes(32).toString("base64url");
      let resources;
      do {
        resources = resourceIdentity(crypto.randomBytes(16).toString("hex"), jobRoot);
      } while (recordedResourceIDs.has(resources.id));
      recordedResourceIDs.add(resources.id);
      state.jobs[String(job.jobID)] = {...job, nonce, resources, status: "launching", createdAt: new Date().toISOString()};
      await atomicState(stateFile, state);
      try {
        await runLauncher({launcher, token, metadata: {...job, nonce, resources}, environment});
        state.jobs[String(job.jobID)].status = "completed";
      } catch (error) {
        const launcherFailure = error instanceof Error ? error.message.slice(0, 200) : "unknown launcher failure";
        state.jobs[String(job.jobID)].launcherFailure = launcherFailure;
        // A normal launcher error can occur before its EXIT trap is installed,
        // and a cleanup error explicitly means resources may remain. Keep the
        // durable state launching until the same idempotent crash reconciler
        // proves every local object and GitHub registration absent.
        await reconcileInterruptedJobs({
          state,
          reconcileLocal: (resources) => runReconciler({reconciler, phase: "local", resources, environment}),
          reconcileRegistration: (resources, cleanupToken) => runReconciler({reconciler, phase: "registration", resources, token: cleanupToken, environment}),
          issueToken: issueLauncherToken,
          persist: (nextState) => atomicState(stateFile, nextState),
          failureMessage: `launcher failed and reconciliation proved all disposable resources absent: ${launcherFailure}`,
        });
        // The no-retry decision is durable and teardown is proven before any
        // network alert. The host OnFailure channel remains authoritative; the
        // GitHub issue is useful secondary evidence only.
        const title = `Nginx JIT launcher failed for job ${job.jobID}`;
        try {
          await github({token, method: "POST", endpoint: `/repos/${REPOSITORY}/issues`, body: {title, body: `The supervised hypervisor controller could not complete run ${job.runID}, attempt ${job.attempt}, job ${job.jobID}. No success attestation was issued. Inspect the root-only hypervisor journal.`}, fetchImpl});
        } catch {
          // The systemd OnFailure webhook remains independent of GitHub.
        }
        throw error;
      }
      state.jobs[String(job.jobID)].finishedAt = new Date().toISOString();
      await atomicState(stateFile, state);
    }
    if (once) break;
    const pollSeconds = Math.min(120, Math.max(15, Number(environment.NGINX_CI_POLL_SECONDS || 30)));
    await new Promise((resolve) => setTimeout(resolve, pollSeconds * 1000));
  } while (true);
};

const invokedAsCLI = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedAsCLI) controller({once: process.argv.includes("--once")}).catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
