# Reproducible benchmark

`benchmark-spec.v1.json` fixes the scenario set; `thresholds.v1.json` fixes acceptance
before a run; `result.schema.json` defines portable evidence. Run the deterministic lane:

```bash
uv run --locked --extra dev python scripts/run-benchmark.py --profile contract \
  --output benchmark-result.json
```

The result records the commit, dirty state, Python/OS, available tool versions, timestamps,
every scored stage, and every unscored stage. A scored `failed`, `blocked`, missing, or
`not-run` stage fails closed. The agent profile remains declared but cannot be selected,
so the runner never fabricates agent evidence.

The cluster profile is executable only with an explicit context, a new benchmark namespace,
and digest-pinned control and dynamic fixture images. Inspect its plan without contacting
Kubernetes:

```bash
uv run --locked --extra dev python scripts/run-benchmark.py --profile cluster --dry-run \
  --context isolated-kind --namespace sites-benchmark-001 --trials 60 \
  --control-image registry.example/sites-control@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --dynamic-image registry.example/http-fixture@sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789
```

Remove `--dry-run` only on a disposable context. The runner refuses an existing namespace,
runs the scaffold evaluator, installs through the standalone Helm path, executes at least
60 static/dynamic publish and rollback trials, then uninstalls and deletes only its generated
namespace. Every command/HTTP event, per-trial stage, latency percentile, context, source
Chart digest, and image digest is recorded. Any failed, blocked, or not-run scored stage
fails closed.

Formal cluster results pin Chart/image digests, Kubernetes and database
versions, fixtures, trial seeds, raw tool events, and a cleanup ledger. Environment repair
during a scored run invalidates the run; restart it from a clean cluster. The
thresholds in `thresholds.v1.json` are predeclared and must not be relaxed after
seeing a result. Every threshold in that file is read by the profile that names
it; `test_benchmark.py` fails if one is added that nothing evaluates.
