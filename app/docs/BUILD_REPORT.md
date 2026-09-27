# Build report

Generated: 2026-09-02T16:03:59.306146+00:00

Overall result: **ATTENTION REQUIRED**

| Check | Exit code | Result |
|---|---:|---|
| Clean dependency install (`npm ci`) | 1 | FAIL |
| Source controls and unit tests | 99 | FAIL |
| Next.js production build | 99 | FAIL |
| HTTP smoke test: /, game, legal, CSRF | 99 | FAIL |
| Production preflight with complete synthetic env | 0 | PASS |

Runtime used: Node v22.16.0, npm 10.9.2.

The full sanitized tail of each command is in `docs/BUILD_LOG.txt`. A PASS here confirms source buildability and HTTP rendering in the test container; it does not replace acceptance testing with the operator's actual PostgreSQL, Platega merchant, fiscalization, infrastructure and legal details.
