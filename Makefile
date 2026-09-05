SHELL := /usr/bin/env bash
CONTROL_IMAGE ?= site-control:local

.PHONY: help install test test-db test-db-down test-supabase test-oss console homepage-check image chart-lint chart-render benchmark quickstart-doctor quickstart quickstart-scale quickstart-status quickstart-access quickstart-token quickstart-clean standalone-install standalone-smoke standalone-uninstall

# Generated from the targets themselves rather than kept as a second list. The
# hand-written listing this replaces had already lost `help`, and nothing could
# have told anyone: a target and its description lived in two places that no
# check compared.
help: ## — list every target with its one-line description
	@grep -E '^[a-z][a-z0-9-]*:.*## — ' Makefile | sed 's/:.*## — / — /'

install: ## — install the CLI/package in editable mode
	uv sync --locked --extra dev

# Mirrors the postgres service container in .github/workflows/ci.yml. Without it the
# database-backed tenancy, versioning, and migration cases cannot execute.
TEST_DB_CONTAINER ?= site-test-db
TEST_DB_PORT ?= 55439

test-db: ## — start the local PostgreSQL the suite expects on 55439
	@docker rm -f $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(TEST_DB_CONTAINER) \
		-e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -e POSTGRES_DB=postgres \
		-e POSTGRES_HOST_AUTH_METHOD=trust \
		-p 127.0.0.1:$(TEST_DB_PORT):5432 postgres:17-alpine
	@printf 'waiting for postgres'
	@for i in $$(seq 1 60); do \
		if docker exec $(TEST_DB_CONTAINER) pg_isready -U postgres -q; then echo ' ready'; exit 0; fi; \
		printf '.'; sleep 1; \
	done; echo ' timed out'; exit 1

test-db-down: ## — stop and remove that container
	@docker rm -f $(TEST_DB_CONTAINER) >/dev/null 2>&1 || true

# sslmode is explicit: DatabaseConfig defaults to "require" and the test-db container
# above serves no TLS, so omitting it fails the PostgresMigrationTests class outright.
SITES_TEST_PG_DSN ?= host=127.0.0.1 port=$(TEST_DB_PORT) dbname=postgres user=postgres sslmode=disable

test: ## — run the standalone Python contract suite (needs test-db)
	SITES_TEST_DB_PORT=$(TEST_DB_PORT) \
	SITES_CLUSTER_POD_CIDR=$(CHART_POD_CIDR) \
	SITES_TEST_PG_DSN='$(SITES_TEST_PG_DSN)' \
	SITES_TEST_REGISTRY_PROXY=1 \
	uv run --locked --extra dev \
		python -m unittest discover -s tests -t . -p 'test_*.py'

test-supabase: ## — run the PostgreSQL integration tests against Supabase
	scripts/test-supabase.sh

test-oss: ## — run the optional private S3/OSS static artifact E2E
	scripts/test-oss.sh

console: ## — lint, typecheck, and build the console
	npm --prefix console ci --ignore-scripts
	npm --prefix console run lint
	npm --prefix console run typecheck
	npm --prefix console run build

homepage-check: ## — validate the GitHub Pages homepage contract
	python3 scripts/check-homepage.py

image: ## — build sites-control from this repository only
	docker build -t $(CONTROL_IMAGE) .

# clusterNetwork.podCIDR has no default in the chart: it is the Pod network of
# the target cluster and a wrong value silently disables tenant isolation. These
# targets pass the reference value so they still render; a real install must
# supply its own.
CHART_POD_CIDR ?= 10.201.0.0/16
CHART_VALUES ?= --set-string clusterNetwork.podCIDR=$(CHART_POD_CIDR)

chart-lint: ## — validate the product-owned Helm package
	helm lint charts/site $(CHART_VALUES)

chart-render: ## — render the self-contained Helm package
	helm template site charts/site $(CHART_VALUES) >/dev/null

benchmark: ## — run the fail-closed deterministic benchmark profile
	uv run --locked --extra dev python scripts/run-benchmark.py --profile contract

quickstart-doctor: ## — check every host prerequisite and local port before the trial
	scripts/quickstart-kubeadm.sh doctor

quickstart: ## — build and prove Site on a disposable multi-node kubeadm cluster
	scripts/quickstart-kubeadm.sh up

quickstart-scale: ## — reconcile the trial to SITES_QUICKSTART_WORKERS=1-4
	scripts/quickstart-kubeadm.sh scale

quickstart-status: ## — inspect the disposable kubeadm trial
	scripts/quickstart-kubeadm.sh status

quickstart-access: ## — open and serve the console from the disposable kubeadm trial
	scripts/quickstart-kubeadm.sh access

quickstart-token: ## — print the disposable trial's local admin token
	scripts/quickstart-kubeadm.sh token

quickstart-clean: ## — delete only the disposable Site kubeadm VMs and network
	scripts/quickstart-kubeadm.sh clean

standalone-install: ## — bootstrap local Secrets and install the Helm Chart
	scripts/standalone.sh install

standalone-smoke: ## — wait for a standalone install and probe /readyz
	scripts/standalone.sh smoke

standalone-uninstall: ## — remove the Helm release, preserving data and Secrets
	scripts/standalone.sh uninstall
