#!/usr/bin/env bash
set -euo pipefail

project_ref="${SUPABASE_PROJECT_REF:-nyezhasntfommjvobokc}"
region="${SUPABASE_REGION:-ap-southeast-1}"
keychain_service="${SUPABASE_DB_KEYCHAIN_SERVICE:-supabase-site-test-db}"

if [[ -z "${SITES_TEST_DB_PASSWORD:-}" ]]; then
  if ! command -v security >/dev/null 2>&1; then
    echo "SITES_TEST_DB_PASSWORD is required outside macOS Keychain environments" >&2
    exit 2
  fi
  SITES_TEST_DB_PASSWORD="$(
    security find-generic-password \
      -a site \
      -s "$keychain_service" \
      -w
  )"
fi

export SITES_TEST_DB_HOST="${SITES_TEST_DB_HOST:-aws-0-${region}.pooler.supabase.com}"
export SITES_TEST_DB_PORT="${SITES_TEST_DB_PORT:-5432}"
export SITES_TEST_DB_NAME="${SITES_TEST_DB_NAME:-postgres}"
export SITES_TEST_DB_USER="${SITES_TEST_DB_USER:-postgres.${project_ref}}"
export SITES_TEST_DB_PASSWORD
export SITES_TEST_DB_SSLMODE="${SITES_TEST_DB_SSLMODE:-require}"
export SITES_TEST_DB_CACHE_CONNECTIONS=true
export SUPABASE_PROJECT_REF="$project_ref"

uv run --locked --extra dev python -m unittest \
  test_supabase \
  test_site_database \
  test_site_versions \
  test_nl2sql
