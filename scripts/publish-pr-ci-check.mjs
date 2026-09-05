#!/usr/bin/env node
import { createSign, verify as verifySignature } from "node:crypto";
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const EXPECTED_REPOSITORY = "Makepad-fr/nginx";
const EXPECTED_WORKFLOW_PATH = ".github/workflows/ci.yml";
const EXPECTED_WORKFLOW_NAME = "CI";
const EXPECTED_RUNNER_GROUP = "Nginx CI";
const BASE_RUNNER_LABELS = ["self-hosted", "linux", "x64", "makepad-nginx-ci-ephemeral"];
const EXPECTED_SCHEMA = "makepad.nginx.ci-attestation.v1";
const MAX_ATTESTATION_AGE_MS = 10 * 60 * 1000;
const MAX_FUTURE_SKEW_MS = 60 * 1000;
export const CHECK_NAMES = ["policy-and-render"];

const expectedRunnerLabels = (attestation) => [
  ...BASE_RUNNER_LABELS,
  `makepad-nginx-job-${attestation.run.id}-${attestation.run.attempt}`,
];

const required = (name, environment = process.env) => {
  const value = environment[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const exactKeys = (value, keys, label) => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object`);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) throw new Error(`${label} has unexpected fields`);
};

export const canonicalJSON = (value) => {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(",")}}`;
  }
  throw new Error("attestation contains a non-canonical JSON value");
};

const base64url = (value) => Buffer.from(value).toString("base64url");

export const createAppJWT = ({ appID, privateKey, now = new Date() }) => {
  if (!/^[1-9]\d*$/.test(appID)) throw new Error("GitHub App ID must be a positive integer");
  if (!/^-----BEGIN (?:RSA )?PRIVATE KEY-----/.test(privateKey.trim())) throw new Error("GitHub App private key must be a PEM private key");
  const issuedAt = Math.floor(now.getTime() / 1000) - 60;
  const header = base64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const payload = base64url(JSON.stringify({ iat: issuedAt, exp: issuedAt + 540, iss: appID }));
  const unsigned = `${header}.${payload}`;
  const signer = createSign("RSA-SHA256");
  signer.update(unsigned);
  signer.end();
  return `${unsigned}.${signer.sign(privateKey, "base64url")}`;
};

const githubResponse = async ({ token, method = "GET", path, body, fetchImpl = fetch }) => {
  const response = await fetchImpl(`https://api.github.com${path}`, {
    method,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28"
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: "error",
    signal: AbortSignal.timeout(30_000)
  });
  const text = await response.text();
  let payload = {};
  if (text) {
    try { payload = JSON.parse(text); }
    catch { throw new Error(`GitHub ${method} ${path} returned non-JSON (${response.status})`); }
  }
  return { ok: response.ok, status: response.status, payload };
};

const githubJSON = async (options) => {
  const response = await githubResponse(options);
  if (!response.ok) {
    const message = typeof response.payload.message === "string" ? response.payload.message : "request failed";
    throw new Error(`GitHub ${options.method || "GET"} ${options.path} failed (${response.status}): ${message}`);
  }
  return response.payload;
};

export const verifySignedAttestation = ({ event, publicKey, approvedDigest, launcherSenderID, now = new Date() }) => {
  if (!/^[1-9]\d*$/.test(String(launcherSenderID))) throw new Error("Launcher App sender ID must be a positive integer");
  if (event?.action !== "nginx-pr-ci-attestation") throw new Error("unexpected repository dispatch action");
  if (event?.repository?.full_name !== EXPECTED_REPOSITORY) throw new Error("attestation targets the wrong repository");
  if (event?.sender?.type !== "Bot" || String(event?.sender?.id) !== String(launcherSenderID)) throw new Error("attestation dispatch was not sent by the dedicated Launcher App");
  exactKeys(event.client_payload, ["attestation", "signature"], "dispatch payload");
  const attestation = event.client_payload.attestation;
  const signature = event.client_payload.signature;
  exactKeys(attestation, ["schema", "repository", "workflow", "ref", "run", "runner", "base_image_sha256", "nonce", "issued_at", "registration_absent", "teardown"], "attestation");
  exactKeys(attestation.workflow, ["name", "path"], "attestation workflow");
  exactKeys(attestation.run, ["id", "attempt", "job_id", "job_name", "event", "head_sha", "workflow_sha", "conclusion"], "attestation run");
  exactKeys(attestation.runner, ["id", "name", "group_id", "group_name", "labels"], "attestation runner");
  exactKeys(attestation.teardown, ["vm", "network", "firewall", "disk"], "attestation teardown");
  if (attestation.schema !== EXPECTED_SCHEMA || attestation.repository !== EXPECTED_REPOSITORY) throw new Error("attestation schema or repository mismatch");
  if (attestation.workflow.name !== EXPECTED_WORKFLOW_NAME || attestation.workflow.path !== EXPECTED_WORKFLOW_PATH || attestation.ref !== "refs/heads/main") throw new Error("attestation workflow or protected ref mismatch");
  for (const [label, value] of Object.entries({run_id: attestation.run.id, run_attempt: attestation.run.attempt, job_id: attestation.run.job_id, runner_id: attestation.runner.id, runner_group_id: attestation.runner.group_id})) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${label} must be a positive safe integer`);
  }
  if (attestation.run.job_name !== "candidate-policy-and-render" || !["pull_request_target", "push"].includes(attestation.run.event) || !/^[a-f0-9]{40}$/.test(attestation.run.head_sha) || !/^[a-f0-9]{40}$/.test(attestation.run.workflow_sha) || (attestation.run.event === "push" && attestation.run.head_sha !== attestation.run.workflow_sha) || !["success", "failure"].includes(attestation.run.conclusion)) throw new Error("attested job identity or conclusion is invalid");
  if (!/^nginx-ci-jit-[a-f0-9]{32}$/.test(attestation.runner.name) || attestation.runner.group_name !== EXPECTED_RUNNER_GROUP) throw new Error("attested runner identity is invalid");
  const labels = Array.isArray(attestation.runner.labels) ? attestation.runner.labels : [];
  const expectedLabels = expectedRunnerLabels(attestation);
  if (labels.length !== expectedLabels.length || labels.some((label, index) => label !== expectedLabels[index])) throw new Error("attested runner labels are not the exact run-bound JIT label set");
  if (attestation.base_image_sha256 !== approvedDigest || !/^[a-f0-9]{64}$/.test(approvedDigest)) throw new Error("attested base image digest is not approved");
  if (!/^[A-Za-z0-9_-]{43}$/.test(attestation.nonce)) throw new Error("attestation nonce is invalid");
  if (typeof attestation.issued_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(attestation.issued_at)) throw new Error("attestation issued_at must be canonical UTC RFC 3339");
  const issuedAt = Date.parse(attestation.issued_at);
  if (!Number.isFinite(issuedAt) || issuedAt < now.getTime() - MAX_ATTESTATION_AGE_MS || issuedAt > now.getTime() + MAX_FUTURE_SKEW_MS) throw new Error("attestation is stale or from the future");
  if (attestation.registration_absent !== true || Object.values(attestation.teardown).some((value) => value !== true)) throw new Error("runner registration or hypervisor teardown is incomplete");
  if (typeof signature !== "string" || !/^[A-Za-z0-9_-]{80,100}$/.test(signature)) throw new Error("attestation signature is invalid");
  let signatureValid = false;
  try { signatureValid = verifySignature(null, Buffer.from(canonicalJSON(attestation)), publicKey, Buffer.from(signature, "base64url")); }
  catch { signatureValid = false; }
  if (!signatureValid) throw new Error("attestation signature verification failed");
  return attestation;
};

export const validateAuthoritativeEvidence = ({ attestation, run, jobs, job, pullRequest = null, runnerListStatus = 200, runnerLookupStatus }) => {
  if (run.id !== attestation.run.id || run.run_attempt !== attestation.run.attempt || run.event !== attestation.run.event || run.head_sha !== attestation.run.workflow_sha || run.head_branch !== "main" || run.path !== EXPECTED_WORKFLOW_PATH || run.name !== EXPECTED_WORKFLOW_NAME || run.status !== "completed" || run.repository?.full_name !== EXPECTED_REPOSITORY || !Number.isSafeInteger(run.repository?.id)) throw new Error("authoritative workflow run does not match the attestation");
  if (jobs.total_count !== (jobs.jobs || []).length) throw new Error("authoritative job response is truncated");
  const matches = (jobs.jobs || []).filter((candidate) => candidate.id === attestation.run.job_id);
  if (matches.length !== 1 || matches[0].id !== job.id) throw new Error("attested job is not unique in the authoritative run attempt");
  const labels = Array.isArray(job.labels) ? job.labels.map((label) => String(label).toLowerCase()).sort() : [];
  const expectedLabels = expectedRunnerLabels(attestation).sort();
  if (job.run_id !== run.id || job.head_sha !== attestation.run.workflow_sha || job.workflow_name !== EXPECTED_WORKFLOW_NAME || job.name !== attestation.run.job_name || job.status !== "completed" || job.runner_id !== attestation.runner.id || job.runner_name !== attestation.runner.name || job.runner_group_id !== attestation.runner.group_id || job.runner_group_name !== attestation.runner.group_name || labels.length !== expectedLabels.length || labels.some((label, index) => label !== expectedLabels[index])) throw new Error("authoritative job runner identity differs from the signed attestation");
  const associations = Array.isArray(run.pull_requests) ? run.pull_requests : [];
  let pullRequestNumber = null;
  if (attestation.run.event === "pull_request_target") {
    if (associations.length !== 1) throw new Error("source run must identify exactly one pull request");
    const association = associations[0];
    if (association.head?.sha !== attestation.run.head_sha || association.head?.repo?.id !== run.repository?.id || association.base?.repo?.id !== run.repository?.id || association.base?.ref !== "main" || association.base?.sha !== attestation.run.workflow_sha || pullRequest?.number !== association.number || pullRequest.state !== "open" || pullRequest.draft !== false || pullRequest.head?.sha !== attestation.run.head_sha || pullRequest.head?.repo?.full_name !== EXPECTED_REPOSITORY || pullRequest.base?.sha !== attestation.run.workflow_sha || pullRequest.base?.repo?.full_name !== EXPECTED_REPOSITORY || pullRequest.base?.ref !== "main") throw new Error("authoritative pull request differs from the signed head identity");
    pullRequestNumber = pullRequest.number;
  } else if (attestation.run.head_sha !== attestation.run.workflow_sha) {
    throw new Error("protected-main push source differs from its workflow SHA");
  }
  const expectedConclusion = run.conclusion === "success" && job.conclusion === "success" ? "success" : "failure";
  if (attestation.run.conclusion !== expectedConclusion) throw new Error("signed conclusion differs from authoritative test result");
  if (runnerListStatus !== 200 || runnerLookupStatus !== 404) throw new Error("attested JIT runner is still registered or registration absence is uncertain");
  return { event: attestation.run.event, headSHA: attestation.run.head_sha, workflowSHA: attestation.run.workflow_sha, pullRequestNumber, conclusion: expectedConclusion, sourceRunID: run.id, sourceRunAttempt: run.run_attempt, detailsURL: run.html_url, nonce: attestation.nonce };
};

export const assertNoAttestationReplay = ({existing, appID, prefix}) => {
  if (!Number.isSafeInteger(existing.total_count) || existing.total_count !== (existing.check_runs || []).length) throw new Error("cannot prove Nginx CI replay protection");
  if ((existing.check_runs || []).some((check) => String(check.app?.id) === String(appID) && String(check.external_id || "").startsWith(prefix))) throw new Error("attestation replay detected for this run attempt");
};

export const publishPRCheck = async ({ environment = process.env, fetchImpl = fetch, now = new Date() } = {}) => {
  if (required("GITHUB_REPOSITORY", environment) !== EXPECTED_REPOSITORY || required("GITHUB_REF", environment) !== "refs/heads/main") throw new Error("CI attestation must run for Makepad-fr/nginx protected main");
  const event = JSON.parse(await readFile(required("GITHUB_EVENT_PATH", environment), "utf8"));
  const attestation = verifySignedAttestation({ event, publicKey: required("NGINX_CI_ATTESTATION_PUBLIC_KEY", environment), approvedDigest: required("NGINX_CI_APPROVED_BASE_IMAGE_SHA256", environment), launcherSenderID: required("NGINX_CI_LAUNCHER_APP_SENDER_ID", environment), now });
  const repositoryToken = required("GITHUB_TOKEN", environment);
  const run = await githubJSON({ token: repositoryToken, path: `/repos/${EXPECTED_REPOSITORY}/actions/runs/${attestation.run.id}`, fetchImpl });
  const jobs = await githubJSON({ token: repositoryToken, path: `/repos/${EXPECTED_REPOSITORY}/actions/runs/${attestation.run.id}/attempts/${attestation.run.attempt}/jobs?per_page=100`, fetchImpl });
  const job = await githubJSON({ token: repositoryToken, path: `/repos/${EXPECTED_REPOSITORY}/actions/jobs/${attestation.run.job_id}`, fetchImpl });
  const associations = Array.isArray(run.pull_requests) ? run.pull_requests : [];
  let pullRequest = null;
  if (attestation.run.event === "pull_request_target") {
    if (associations.length !== 1 || !Number.isSafeInteger(associations[0]?.number)) throw new Error("source run has no unique pull request association");
    pullRequest = await githubJSON({ token: repositoryToken, path: `/repos/${EXPECTED_REPOSITORY}/pulls/${associations[0].number}`, fetchImpl });
  }

  const appID = required("NGINX_PR_CHECK_APP_ID", environment);
  const appJWT = createAppJWT({ appID, privateKey: required("NGINX_PR_CHECK_APP_PRIVATE_KEY", environment), now });
  const installation = await githubJSON({ token: appJWT, path: `/repos/${EXPECTED_REPOSITORY}/installation`, fetchImpl });
  if (String(installation.app_id) !== appID || !Number.isSafeInteger(installation.id)) throw new Error("configured Checks App is not the Nginx installation");
  const installationToken = await githubJSON({ token: appJWT, method: "POST", path: `/app/installations/${installation.id}/access_tokens`, body: { repositories: ["nginx"], permissions: { checks: "write", organization_self_hosted_runners: "read" } }, fetchImpl });
  if (typeof installationToken.token !== "string" || !installationToken.token) throw new Error("Checks App installation did not issue a token");
  const runnerList = await githubResponse({ token: installationToken.token, path: "/orgs/Makepad-fr/actions/runners?per_page=1", fetchImpl });
  const runnerLookup = await githubResponse({ token: installationToken.token, path: `/orgs/Makepad-fr/actions/runners/${attestation.runner.id}`, fetchImpl });
  const verified = validateAuthoritativeEvidence({ attestation, run, jobs, job, pullRequest, runnerListStatus: runnerList.status, runnerLookupStatus: runnerLookup.status });

  const externalID = `nginx-ci:${verified.event}:${verified.sourceRunID}:${verified.sourceRunAttempt}:${verified.nonce}`;
  const checkRunIDs = {};
  for (const checkName of CHECK_NAMES) {
    const existing = await githubJSON({ token: installationToken.token, path: `/repos/${EXPECTED_REPOSITORY}/commits/${verified.headSHA}/check-runs?check_name=${encodeURIComponent(checkName)}&filter=all&per_page=100`, fetchImpl });
    const prefix = `nginx-ci:${verified.event}:${verified.sourceRunID}:${verified.sourceRunAttempt}:`;
    assertNoAttestationReplay({existing, appID, prefix});
    const scope = verified.event === "pull_request_target" ? `PR #${verified.pullRequestNumber}` : "protected-main push";
    const checkBody = { name: checkName, head_sha: verified.headSHA, status: "completed", conclusion: verified.conclusion, external_id: externalID, details_url: verified.detailsURL, completed_at: now.toISOString(), output: { title: verified.conclusion === "success" ? "Disposable CI and teardown verified" : "Disposable CI failed; teardown verified", summary: `Signed hypervisor evidence for ${scope}, run ${verified.sourceRunID}, attempt ${verified.sourceRunAttempt}.` } };
    const published = await githubJSON({ token: installationToken.token, method: "POST", path: `/repos/${EXPECTED_REPOSITORY}/check-runs`, body: checkBody, fetchImpl });
    if (published.name !== checkName || published.head_sha !== verified.headSHA || published.external_id !== externalID || published.conclusion !== verified.conclusion || String(published.app?.id) !== appID || !Number.isSafeInteger(published.id)) throw new Error(`published ${checkName} check does not match the verified signed result`);
    checkRunIDs[checkName] = published.id;
  }
  return { ...verified, checkRunIDs, appID };
};

const invokedAsCLI = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedAsCLI) {
  publishPRCheck().then((result) => process.stdout.write(`Published ${CHECK_NAMES.join(",")}=${result.conclusion} for ${result.headSHA}.\n`)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
