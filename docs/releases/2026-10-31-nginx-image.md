# Shared proxy image release candidate — 31 October 2026

The candidate updates only the official Nginx image digest. Deploy it with
`scripts/upgrade-shared-image.py`, preserving the complete live service spec.

- Predecessor: `nginx:1.31-alpine3.24@sha256:060849a174666c53439dc7cecbf8f67e225c7cbfb1a44ca97915d9a172a81e6d`.
- Candidate: `nginx:1.31-alpine3.24@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606`.
- Source PR: #97 at `6f440e20024c2b1c101b5255e4729dfc4ffe893f`.
- Independent repository tests: 79 passed.
- Mendloo review: https://github.com/Makepad-fr/nginx/pull/97#pullrequestreview-5722367740

The live-host `--check` preflight passed with the candidate image against all
rendered routes, certificates, existing mounts and upstreams. A baseline recorded
33 HTTPS endpoint responses, including all 30 explicitly named public hosts,
Visitaki health (204), protected readiness (403), and identity discovery (200).
Existing root-route 404/421 responses and authentication redirects remain baseline
behavior, not evidence that those paths implement application health checks.

Deployment acceptance still requires an image-only receipt, a healthy converged
service, exact preservation of the remaining live service specification, and the
same 33 endpoint responses after rollout. The preflight does not claim deployment
has occurred. Swarm owns automatic health-failure rollback; inspect any failed
rollout before further mutation.
