# site cluster benchmark report — 2026-09-01

## Result

site passed its predeclared `cluster` profile at revision
`7d9b10df9a2006fd4398ecb72b7bdd9fc7a9f88a`. The run completed 60 valid trials and
300 scored lifecycle stages with no failed, blocked, or not-run stage.

| Measure | Result | Acceptance |
| --- | ---: | ---: |
| Valid trials | 60 / 60 | at least 60 |
| Scored stages | 300 passed, 0 failed | no failures |
| Revision-matched verification | 100% | 100% |
| Per-trial workload cleanup | 100% | 100% |
| Static publish | p50 6.668 s; p95 8.090 s | p95 at most 120 s |
| Dynamic publish | p50 6.908 s; p95 8.085 s | p95 at most 600 s |
| Rollback and recovery | p50 15.862 s; p95 77.826 s | measured, not thresholded |
| Final cluster cleanup | passed in 26.508 s | namespace and PVs removed |

Every trial exercised static publish, dynamic version publish, server-side
revision matching, version rollback/recovery, and deletion. The final cleanup
deleted the benchmark namespace and all three persistent volumes created by the
run. The evidence checksum manifest verifies successfully.

## Environment and reproducibility

- Profile: `cluster`, benchmark contract `1.0.0`
- Run id: `d29b64e1-5404-4a02-bb14-4caa9931b66c`
- Run window: 2026-08-31 23:09:41–23:52:24 UTC
- Kubernetes: Kind server v1.37.0 on Linux arm64
- Host: macOS 26.5.1 arm64; Python 3.13.2
- Chart source digest: `sha256:9b7b2d4de9e4551aed9ba82f5e53447be95dbf5ccf627bc56ae2d661eaca2545`
- Images were immutable digest references recorded in `cluster-result.json`.

The source of truth is the evidence directory
`benchmark-evidence/site-7d9b10d-20260901-r17` in the benchmark archive,
especially `cluster-plan.json`, `cluster-result.json`, `SHA256SUMS`, and
`residue-check.txt`. The evidence is intentionally not copied into this Git
repository because it contains roughly 20,000 lines of per-stage observations.

Run a new benchmark with `scripts/cluster_benchmark.py`; do not update this
report from a unit-test result or a dry-run plan. This report describes the
tested revision above. Later commits must not be presented as benchmarked until
another cluster run records them.

## Interpretation and limits

This is strong lifecycle correctness evidence for the reference Kind topology,
not a claim about Internet-scale hosting. It does not measure concurrent tenant
load, sustained request throughput, geographic latency, managed PostgreSQL, or
a production S3/OSS deployment. Rollback p95 has a long tail and remains a
specific optimization target even though all recovery stages completed.
