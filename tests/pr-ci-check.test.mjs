import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import {mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import test from "node:test";
import {fileURLToPath, pathToFileURL} from "node:url";

const candidateRoot = path.resolve(process.env.NGINX_CANDIDATE_ROOT || fileURLToPath(new URL("..", import.meta.url)));
const publisherURL = pathToFileURL(path.join(candidateRoot, "scripts/publish-pr-ci-check.mjs"));
const {
  CHECK_NAMES,
  assertNoAttestationReplay,
  canonicalJSON,
  publishPRCheck,
  validateAuthoritativeEvidence,
  verifySignedAttestation,
} = await import(publisherURL.href);

const now = new Date("2026-09-05T10:00:00Z");
const digest = "a".repeat(64);
const {privateKey, publicKey} = generateKeyPairSync("ed25519");

const baseAttestation = () => ({
  base_image_sha256: digest,
  issued_at: now.toISOString().replace(".000Z", "Z"),
  nonce: "A".repeat(43),
  ref: "refs/heads/main",
  registration_absent: true,
  repository: "Makepad-fr/nginx",
  run: {attempt: 2, conclusion: "success", event: "pull_request_target", head_sha: "b".repeat(40), workflow_sha: "c".repeat(40), id: 1234, job_id: 5678, job_name: "candidate-policy-and-render"},
  runner: {group_id: 12, group_name: "Nginx CI", id: 44, labels: ["self-hosted", "linux", "x64", "makepad-nginx-ci-ephemeral", "makepad-nginx-job-1234-2"], name: `nginx-ci-jit-${"d".repeat(32)}`},
  schema: "makepad.nginx.ci-attestation.v1",
  teardown: {disk: true, firewall: true, network: true, vm: true},
  workflow: {name: "CI", path: ".github/workflows/ci.yml"},
});

const signedEvent = (attestation = baseAttestation(), senderID = 9001) => ({
  action: "nginx-pr-ci-attestation",
  repository: {full_name: "Makepad-fr/nginx"},
  sender: {id: senderID, type: "Bot"},
  client_payload: {
    attestation,
    signature: sign(null, Buffer.from(canonicalJSON(attestation)), privateKey).toString("base64url"),
  },
});

const verify = (event, overrides = {}) => verifySignedAttestation({event, publicKey, approvedDigest: digest, launcherSenderID: "9001", now, ...overrides});

const authoritative = (attestation = baseAttestation()) => {
  const job = {
    id: 5678,
    run_id: 1234,
    head_sha: "c".repeat(40),
    workflow_name: "CI",
    name: "candidate-policy-and-render",
    status: "completed",
    conclusion: attestation.run.conclusion,
    runner_id: 44,
    runner_name: `nginx-ci-jit-${"d".repeat(32)}`,
    runner_group_id: 12,
    runner_group_name: "Nginx CI",
    labels: ["self-hosted", "linux", "x64", "makepad-nginx-ci-ephemeral", "makepad-nginx-job-1234-2"],
  };
  const association = {number: 7, head: {sha: "b".repeat(40), repo: {id: 88}}, base: {ref: "main", sha: "c".repeat(40), repo: {id: 88}}};
  return {
    attestation,
    run: {id: 1234, run_attempt: 2, event: attestation.run.event, head_sha: attestation.run.workflow_sha, head_branch: "main", path: ".github/workflows/ci.yml", name: "CI", status: "completed", conclusion: attestation.run.conclusion, repository: {id: 88, full_name: "Makepad-fr/nginx"}, pull_requests: [association], html_url: "https://github.example/run/1234"},
    jobs: {total_count: 1, jobs: [job]},
    job,
    pullRequest: {number: 7, state: "open", draft: false, head: {sha: "b".repeat(40), repo: {full_name: "Makepad-fr/nginx"}}, base: {ref: "main", sha: "c".repeat(40), repo: {full_name: "Makepad-fr/nginx"}}},
    runnerLookupStatus: 404,
  };
};

test("accepts fresh hypervisor-signed teardown evidence from immutable Launcher App sender", () => {
  assert.deepEqual(CHECK_NAMES, ["policy-and-render"]);
  assert.equal(verify(signedEvent()).run.job_id, 5678);
});

test("rejects forged evidence", () => {
  const event = signedEvent();
  event.client_payload.attestation.run.head_sha = "c".repeat(40);
  assert.throws(() => verify(event), /signature verification failed/);
});

test("rejects stale evidence", () => {
  const attestation = baseAttestation();
  attestation.issued_at = "2026-09-05T09:40:00Z";
  assert.throws(() => verify(signedEvent(attestation)), /stale or from the future/);
});

test("rejects an unapproved base image digest", () => {
  assert.throws(() => verify(signedEvent(), {approvedDigest: "c".repeat(64)}), /not approved/);
});

test("rejects incomplete hypervisor teardown", () => {
  const attestation = baseAttestation();
  attestation.teardown.network = false;
  assert.throws(() => verify(signedEvent(attestation)), /teardown is incomplete/);
});

test("rejects a signed runner label not bound to the exact run attempt", () => {
  const attestation = baseAttestation();
  attestation.runner.labels[4] = "makepad-nginx-job-1234-3";
  assert.throws(() => verify(signedEvent(attestation)), /run-bound JIT label set/);
});

test("rejects mutable sender-name forgery with the wrong numeric App sender ID", () => {
  assert.throws(() => verify(signedEvent(baseAttestation(), 9002)), /dedicated Launcher App/);
});

test("rejects authoritative runner mismatch and a still-registered runner", () => {
  const mismatch = authoritative();
  mismatch.job.runner_id = 45;
  assert.throws(() => validateAuthoritativeEvidence(mismatch), /runner identity differs/);
  const wrongJobLabel = authoritative();
  wrongJobLabel.job.labels[4] = "makepad-nginx-job-1234-3";
  assert.throws(() => validateAuthoritativeEvidence(wrongJobLabel), /runner identity differs/);
  const registered = authoritative();
  registered.runnerLookupStatus = 200;
  assert.throws(() => validateAuthoritativeEvidence(registered), /still registered/);
  const noListAuthority = authoritative();
  noListAuthority.runnerListStatus = 403;
  assert.throws(() => validateAuthoritativeEvidence(noListAuthority), /absence is uncertain/);
});

test("rejects a moved, closed, draft, fork, or differently based pull request", () => {
  for (const mutate of [
    (value) => { value.pullRequest.head.sha = "d".repeat(40); },
    (value) => { value.pullRequest.state = "closed"; },
    (value) => { value.pullRequest.draft = true; },
    (value) => { value.run.pull_requests[0].head.repo.id = 999; },
    (value) => { value.run.pull_requests[0].base.sha = "d".repeat(40); },
    (value) => { value.pullRequest.base.sha = "d".repeat(40); },
  ]) {
    const evidence = authoritative();
    mutate(evidence);
    assert.throws(() => validateAuthoritativeEvidence(evidence), /pull request differs/);
  }
});

test("accepts a failing test result only as a failing check after verified teardown", () => {
  const attestation = baseAttestation();
  attestation.run.conclusion = "failure";
  const verified = validateAuthoritativeEvidence(authoritative(attestation));
  assert.equal(verified.conclusion, "failure");
});

test("accepts protected-main push evidence only when source and workflow SHAs match", () => {
  const attestation = baseAttestation();
  attestation.run.event = "push";
  attestation.run.head_sha = attestation.run.workflow_sha;
  const evidence = authoritative(attestation);
  evidence.run.pull_requests = [];
  evidence.pullRequest = null;
  const verified = validateAuthoritativeEvidence(evidence);
  assert.equal(verified.event, "push");
  const mismatch = baseAttestation();
  mismatch.run.event = "push";
  assert.throws(() => verify(signedEvent(mismatch)), /identity or conclusion is invalid/);
});

test("rejects an authoritative workflow execution SHA mismatch", () => {
  const evidence = authoritative();
  evidence.job.head_sha = "d".repeat(40);
  assert.throws(() => validateAuthoritativeEvidence(evidence), /runner identity differs/);
});

test("rejects replay for the same run attempt and Checks App", () => {
  assert.throws(() => assertNoAttestationReplay({
    appID: "500",
    prefix: "nginx-ci:pull_request_target:1234:2:",
    existing: {total_count: 1, check_runs: [{app: {id: 500}, external_id: `nginx-ci:pull_request_target:1234:2:${"A".repeat(43)}`}]},
  }), /replay detected/);
});

test("the Checks App token requests no unused Actions permission", async () => {
  const source = await readFile(publisherURL, "utf8");
  const tokenRequest = source.match(/permissions: \{ checks: "write", organization_self_hosted_runners: "read" \}/);
  assert.ok(tokenRequest);
});

test("publishes one Checks-App-bound result only after authoritative teardown verification", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "nginx-ci-attestor-"));
  const eventPath = path.join(directory, "event.json");
  await writeFile(eventPath, JSON.stringify(signedEvent()));
  const {privateKey: appPrivateKey} = generateKeyPairSync("rsa", {modulusLength: 2048});
  const environment = {
    GITHUB_REPOSITORY: "Makepad-fr/nginx",
    GITHUB_REF: "refs/heads/main",
    GITHUB_EVENT_PATH: eventPath,
    GITHUB_TOKEN: "read-only-workflow-token",
    NGINX_CI_ATTESTATION_PUBLIC_KEY: publicKey.export({type: "spki", format: "pem"}),
    NGINX_CI_APPROVED_BASE_IMAGE_SHA256: digest,
    NGINX_CI_LAUNCHER_APP_SENDER_ID: "9001",
    NGINX_PR_CHECK_APP_ID: "4242",
    NGINX_PR_CHECK_APP_PRIVATE_KEY: appPrivateKey.export({type: "pkcs8", format: "pem"}),
  };
  const evidence = authoritative();
  const published = [];
  const json = (value, status = 200) => new Response(JSON.stringify(value), {
    status,
    headers: {"content-type": "application/json"},
  });
  const fetchImpl = async (input, init) => {
    const url = new URL(input);
    const requestPath = `${url.pathname}${url.search}`;
    if (requestPath === "/repos/Makepad-fr/nginx/actions/runs/1234") return json(evidence.run);
    if (requestPath === "/repos/Makepad-fr/nginx/actions/runs/1234/attempts/2/jobs?per_page=100") return json(evidence.jobs);
    if (requestPath === "/repos/Makepad-fr/nginx/actions/jobs/5678") return json(evidence.job);
    if (requestPath === "/repos/Makepad-fr/nginx/pulls/7") return json(evidence.pullRequest);
    if (requestPath === "/repos/Makepad-fr/nginx/installation") return json({app_id: 4242, id: 91});
    if (requestPath === "/app/installations/91/access_tokens") {
      assert.equal(init.method, "POST");
      assert.deepEqual(JSON.parse(init.body), {repositories: ["nginx"], permissions: {checks: "write", organization_self_hosted_runners: "read"}});
      return json({token: "checks-installation-token"});
    }
    if (requestPath === "/orgs/Makepad-fr/actions/runners?per_page=1") return json({total_count: 1, runners: [{id: 99}]});
    if (requestPath === "/orgs/Makepad-fr/actions/runners/44") return json({message: "Not Found"}, 404);
    if (requestPath.startsWith(`/repos/Makepad-fr/nginx/commits/${"b".repeat(40)}/check-runs?`)) return json({total_count: 0, check_runs: []});
    if (requestPath === "/repos/Makepad-fr/nginx/check-runs") {
      assert.equal(init.method, "POST");
      const body = JSON.parse(init.body);
      published.push(body);
      return json({...body, id: 501, app: {id: 4242}});
    }
    throw new Error(`unexpected GitHub request: ${init.method || "GET"} ${requestPath}`);
  };

  try {
    const result = await publishPRCheck({environment, fetchImpl, now});
    assert.equal(result.conclusion, "success");
    assert.deepEqual(result.checkRunIDs, {"policy-and-render": 501});
    assert.equal(published.length, 1);
    assert.equal(published[0].external_id, `nginx-ci:pull_request_target:1234:2:${"A".repeat(43)}`);
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
});
