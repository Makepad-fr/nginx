# Shared proxy image release — 31 October 2026

The candidate updates only the official Nginx image digest. Deploy it with
`scripts/upgrade-shared-image.py`, preserving the complete live service spec.

- Predecessor: `nginx:1.31-alpine3.24@sha256:060849a174666c53439dc7cecbf8f67e225c7cbfb1a44ca97915d9a172a81e6d`.
- Candidate: `nginx:1.31-alpine3.24@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606`.
- Source PR: #97 at `6f440e20024c2b1c101b5255e4729dfc4ffe893f`.
- Independent repository tests: 79 passed.
- Mendloo review: https://github.com/Makepad-fr/nginx/pull/97#pullrequestreview-5722373171

The live-host `--check` preflight passed with the candidate image against all
rendered routes, certificates, existing mounts and upstreams. A baseline recorded
33 HTTPS endpoint responses, including all 30 explicitly named public hosts,
Visitaki health (204), protected readiness (403), and identity discovery (200).
Existing root-route 404/421 responses and authentication redirects remain baseline
behavior, not evidence that those paths implement application health checks.

PR #97 merged as `b31b0ee119dc3347a6b9743f6a16cb6a9896ed76`. Exact-main CI
runs `41193580504` and `41193580609` passed. The image-only upgrade then
converged `makepad-edge_nginx` at service version `105402` with healthy tasks.
The receipt preserved the remaining service specification with SHA-256
`09cc9b9ad4e8dc4eb402c31819f82a902d427d1a0e6458ac04266b7cc9977211`.
All 33 HTTPS endpoint responses matched the baseline after deployment.

Swarm owns automatic health-failure rollback. The predecessor image above remains
the rollback target; inspect any future failed rollout before further mutation.
