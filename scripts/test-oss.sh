#!/usr/bin/env bash
set -euo pipefail

bucket="${SITES_TEST_OSS_BUCKET:-REPLACE_ME_OSS_BUCKET}"
region="${SITES_TEST_OSS_REGION:-cn-shanghai}"
endpoint="${SITES_TEST_OSS_ENDPOINT:-https://oss-cn-shanghai.aliyuncs.com}"
keychain_account="${SITES_TEST_OSS_KEYCHAIN_ACCOUNT:-site}"
key_id_service="${SITES_TEST_OSS_KEY_ID_SERVICE:-site-oss-test-access-key-id}"
key_secret_service="${SITES_TEST_OSS_KEY_SECRET_SERVICE:-site-oss-test-access-key-secret}"
credential_dir=""

# The default is a placeholder, not a bucket: this script used to ship a real
# bucket whose name embedded the account UID.  Fail loudly rather than let a
# forgotten export turn into a request against whatever "REPLACE_ME_*" resolves to.
if [[ "$bucket" == REPLACE_ME_* ]]; then
  echo "set SITES_TEST_OSS_BUCKET to your own test bucket" >&2
  exit 2
fi

cleanup() {
  if [[ -n "$credential_dir" && -d "$credential_dir" ]]; then
    rm -rf -- "$credential_dir"
  fi
}
trap cleanup EXIT

if [[ -z "${SITES_TEST_OSS_ACCESS_KEY_ID_FILE:-}" || -z "${SITES_TEST_OSS_ACCESS_KEY_SECRET_FILE:-}" ]]; then
  if ! command -v security >/dev/null 2>&1; then
    echo "dedicated OSS credential files are required outside macOS Keychain environments" >&2
    exit 2
  fi
  credential_dir="$(mktemp -d "${TMPDIR:-/tmp}/site-oss-e2e.XXXXXX")"
  security find-generic-password -a "$keychain_account" -s "$key_id_service" -w \
    >"$credential_dir/access-key-id"
  security find-generic-password -a "$keychain_account" -s "$key_secret_service" -w \
    >"$credential_dir/access-key-secret"
  chmod 600 "$credential_dir/access-key-id" "$credential_dir/access-key-secret"
  export SITES_TEST_OSS_ACCESS_KEY_ID_FILE="$credential_dir/access-key-id"
  export SITES_TEST_OSS_ACCESS_KEY_SECRET_FILE="$credential_dir/access-key-secret"
fi

export SITES_TEST_OSS_ENDPOINT="$endpoint"
export SITES_TEST_OSS_BUCKET="$bucket"
export SITES_TEST_OSS_PREFIX="${SITES_TEST_OSS_PREFIX:-site-e2e/static-artifacts}"
export SITES_TEST_OSS_REGION="$region"
export SITES_TEST_OSS_ADDRESSING_STYLE="${SITES_TEST_OSS_ADDRESSING_STYLE:-virtual}"
export SITES_TEST_OSS_SIGNATURE_VERSION="${SITES_TEST_OSS_SIGNATURE_VERSION:-s3}"

uv run --locked --extra dev python -m unittest test_static_artifacts_cloud
