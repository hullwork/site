#!/usr/bin/env bash
#
# Fail the build on CodeQL findings at security-severity >= 7.0 that are not
# listed in the allowlist.
#
# This repository has no GitHub Advanced Security, so github/codeql-action's
# upload-sarif 403s and the alerts have nowhere to land. Without a gate here
# CodeQL only prints counts and exits 0 -- a scan that can never fail. The
# threshold matches the Trivy/audit jobs in ci.yml: CVSS >= 7.0 is what SARIF's
# security-severity encodes as HIGH.
#
# The gate makes two separate checks, and they need different amounts of
# evidence:
#
#   unexpected findings -- a finding with no allowlist entry. Safe to run
#     always. A run that looks at less than the whole tree can only report
#     fewer findings, never more, so a partial run cannot fail this wrongly.
#
#   stale entries -- an allowlist entry that matched nothing, meaning the code
#     it excused is gone and the line should be deleted. This one needs the
#     finding set to be COMPLETE for the entries being checked. Otherwise
#     "this entry matched nothing" and "this run never looked there" are
#     indistinguishable, and the check fires on every run until someone
#     deletes it. Two things make a run incomplete:
#       - it analyses one language, while the allowlist covers all of them;
#       - on pull_request, codeql-action generates a `pr-diff-range` extension
#         pack that fills CodeQL's `restrictAlertsTo` extensible predicate, so
#         every dataflow-based query only reports alerts on lines the PR
#         touched. Purely syntactic queries are unaffected, which is why a PR
#         run reports a lopsided subset rather than nothing.
#     So the stale check runs only over the rule-id prefixes named in
#     CODEQL_GATE_STALE_SCOPE, and the caller sets that to empty on
#     pull_request. When it is off, the gate says so out loud -- a disabled
#     check that announces itself is recoverable; a silent one is not.
#
# Two more habits, learned from a sibling repository whose identical gate
# stayed green for months without evaluating a single finding:
#   - no pipe carries the exit status. `jq ... | tee` reports tee's 0, so a jq
#     program that does not even compile still passes.
#   - `--self-test` exercises every decision this script makes and asserts each
#     one both ways. CI runs it before the real SARIF.
set -euo pipefail

usage() {
  echo "usage: codeql-gate.sh SARIF_DIR ALLOWLIST_FILE" >&2
  echo "       codeql-gate.sh --self-test" >&2
  echo "env:   CODEQL_GATE_STALE_SCOPE  comma-separated rule-id prefixes whose" >&2
  echo "                                allowlist entries this run may declare" >&2
  echo "                                stale. Empty/unset disables that check." >&2
  echo "       CODEQL_SEVERITY_THRESHOLD  default 7" >&2
}

threshold=${CODEQL_SEVERITY_THRESHOLD:-7}

self_test() {
  local dir rc
  dir=$(mktemp -d)
  trap 'rm -rf "$dir"' RETURN

  write_sarif() {
    cat > "$2" <<JSON
{"version":"2.1.0","runs":[{"tool":{"driver":{"name":"selftest","rules":[
  {"id":"py/selftest-rule","properties":{"security-severity":"$1"}}]}},
  "results":[{"ruleId":"py/selftest-rule",
    "partialFingerprints":{"primaryLocationLineHash":"deadbeef:1"},
    "locations":[{"physicalLocation":{
      "artifactLocation":{"uri":"selftest.py"},"region":{"startLine":42}}}]}]}]}
JSON
  }

  # Returns the gate's exit code without a pipe in the way.
  run_gate() {
    local scope=$1 sarif=$2 allow=$3 out=$4 rc=0
    CODEQL_GATE_STALE_SCOPE="$scope" "$0" "$sarif" "$allow" >"$out" 2>&1 || rc=$?
    echo "$rc"
  }

  expect() {
    local label=$1 want=$2 got=$3 out=$4
    if [ "$got" != "$want" ]; then
      echo "codeql-gate self-test: $label -- expected exit $want, got $got" >&2
      cat "$out" >&2
      return 1
    fi
  }

  : > "$dir/empty-allowlist"
  printf 'py/selftest-rule\tselftest.py\tdeadbeef:1\t# self-test\n' > "$dir/matching-allowlist"
  printf 'py/gone-rule\tgone.py\tcafebabe:1\t# self-test stale entry\n' > "$dir/stale-allowlist"

  mkdir -p "$dir/above" "$dir/below" "$dir/none"
  write_sarif 8.1 "$dir/above/selftest.sarif"
  write_sarif 3.0 "$dir/below/selftest.sarif"

  # 1. A finding above the threshold with no allowlist entry must fail, and the
  #    message must name the rule -- an exit code alone does not prove the gate
  #    looked at the right thing.
  rc=$(run_gate "py/" "$dir/above" "$dir/empty-allowlist" "$dir/1.out")
  expect "unallowlisted 8.1 finding" 1 "$rc" "$dir/1.out"
  if ! /bin/grep -q 'py/selftest-rule' "$dir/1.out"; then
    echo "codeql-gate self-test: failure did not name the rule" >&2
    cat "$dir/1.out" >&2
    return 1
  fi

  # 2. Below the threshold must pass. Without this the gate could be failing on
  #    everything, which looks identical to working from the failure side.
  rc=$(run_gate "py/" "$dir/below" "$dir/empty-allowlist" "$dir/2.out")
  expect "3.0 finding" 0 "$rc" "$dir/2.out"

  # 3. Allowlisted finding must pass.
  rc=$(run_gate "py/" "$dir/above" "$dir/matching-allowlist" "$dir/3.out")
  expect "allowlisted 8.1 finding" 0 "$rc" "$dir/3.out"

  # 4. A stale entry must fail when it is inside the declared scope...
  rc=$(run_gate "py/" "$dir/below" "$dir/stale-allowlist" "$dir/4.out")
  expect "stale entry, in scope" 1 "$rc" "$dir/4.out"

  # 5. ...and must NOT fail when the scope is off, because then the gate cannot
  #    tell a stale entry from one this run never looked for.
  rc=$(run_gate "" "$dir/below" "$dir/stale-allowlist" "$dir/5.out")
  expect "stale entry, scope off" 0 "$rc" "$dir/5.out"
  if ! /bin/grep -q 'stale-entry check is OFF' "$dir/5.out"; then
    echo "codeql-gate self-test: disabling the stale check was not announced" >&2
    cat "$dir/5.out" >&2
    return 1
  fi

  # 6. ...and must NOT fail when the scope covers a different language.
  rc=$(run_gate "js/" "$dir/below" "$dir/stale-allowlist" "$dir/6.out")
  expect "stale entry, out of scope" 0 "$rc" "$dir/6.out"

  # 7. An empty SARIF glob is a broken run, not a clean one.
  rc=$(run_gate "py/" "$dir/none" "$dir/empty-allowlist" "$dir/7.out")
  expect "no SARIF produced" 1 "$rc" "$dir/7.out"

  echo "codeql-gate self-test: 7/7 -- fails at 8.1 and names the rule, passes at 3.0,"
  echo "  honours the allowlist, fails on an in-scope stale entry, stays quiet on an"
  echo "  out-of-scope one, announces itself when the stale check is off, and treats"
  echo "  a missing SARIF as a failure."
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
  exit 0
fi

sarif_dir=${1:-}
allowlist=${2:-}
stale_scope=${CODEQL_GATE_STALE_SCOPE:-}
if [ -z "$sarif_dir" ] || [ -z "$allowlist" ]; then
  usage
  exit 2
fi

shopt -s nullglob
sarif_files=("$sarif_dir"/*.sarif)
shopt -u nullglob
# An empty glob here is the difference between "clean scan" and "the analyze
# step produced nothing"; the two must not look alike.
if [ ${#sarif_files[@]} -eq 0 ]; then
  echo "codeql-gate: no *.sarif under '$sarif_dir' -- nothing was evaluated" >&2
  exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Key on (ruleId, file, primaryLocationLineHash). The line hash is CodeQL's own
# content fingerprint, so an allowlist entry survives the file being reshuffled
# but stops applying the moment the flagged line itself changes. Measured stable
# across a rebase and across machines.
jq -s -r --argjson threshold "$threshold" '
  [ .[]
    | .runs[]
    | . as $run
    | ( [ $run.tool.driver.rules[]? ]
        + [ $run.tool.extensions[]?.rules[]? ] ) as $rules
    | $run.results[]?
    | select([.suppressions[]?] | length == 0)
    | . as $result
    | ( [ $rules[]
          | select(.id == $result.ruleId)
          | (.properties["security-severity"] // "0") | tonumber ]
        | max // 0 ) as $severity
    | select($severity >= $threshold)
    | [ $result.ruleId,
        ($result.locations[0].physicalLocation.artifactLocation.uri // "unknown"),
        ($result.partialFingerprints.primaryLocationLineHash // "no-fingerprint"),
        ($severity | tostring),
        ($result.locations[0].physicalLocation.region.startLine // 0 | tostring)
      ]
    | @tsv
  ]
  | sort | unique | .[]
' "${sarif_files[@]}" > "$work/findings.tsv"

cut -f1-3 "$work/findings.tsv" | sort -u > "$work/found.keys"
if [ -f "$allowlist" ]; then
  sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' "$allowlist" | sort -u \
    > "$work/allowed.keys"
else
  : > "$work/allowed.keys"
fi

comm -23 "$work/found.keys" "$work/allowed.keys" > "$work/unexpected.keys"

if [ -n "$stale_scope" ]; then
  awk -F'\t' -v scope="$stale_scope" '
    BEGIN { n = split(scope, prefix, ",") }
    {
      for (i = 1; i <= n; i++)
        if (prefix[i] != "" && index($1, prefix[i]) == 1) { print; next }
    }' "$work/allowed.keys" > "$work/inscope.keys"
  comm -13 "$work/found.keys" "$work/inscope.keys" > "$work/stale.keys"
else
  : > "$work/inscope.keys"
  : > "$work/stale.keys"
fi

status=0

if [ -s "$work/unexpected.keys" ]; then
  echo "codeql-gate: findings at security-severity >= $threshold with no allowlist entry:" >&2
  awk -F'\t' '
    NR == FNR { want[$0] = 1; next }
    ($1 "\t" $2 "\t" $3) in want {
      printf "  %s\n    %s:%s  (security-severity %s)\n    allowlist key: %s\t%s\t%s\n",
             $1, $2, $5, $4, $1, $2, $3
    }' "$work/unexpected.keys" "$work/findings.tsv" >&2
  echo "Fix the finding, or add the printed key to '$allowlist' with a reason." >&2
  status=1
fi

if [ -s "$work/stale.keys" ]; then
  echo "codeql-gate: allowlist entries in '$allowlist' that matched no finding:" >&2
  sed 's/^/  /' "$work/stale.keys" >&2
  echo "The code they excused has changed or is gone. Delete these lines." >&2
  status=1
fi

if [ -z "$stale_scope" ]; then
  echo "codeql-gate: stale-entry check is OFF for this run (CODEQL_GATE_STALE_SCOPE is empty)."
  echo "  This run sees only part of the allowlist's subject matter, so it cannot tell"
  echo "  a stale entry from one it never looked for. The push/schedule runs check it."
else
  echo "codeql-gate: stale-entry check covered $(wc -l < "$work/inscope.keys") entry(s) matching [$stale_scope]."
fi

if [ "$status" -eq 0 ]; then
  echo "codeql-gate: $(wc -l < "$work/found.keys") finding(s) at security-severity >= $threshold, all allowlisted; none new."
fi

exit "$status"
