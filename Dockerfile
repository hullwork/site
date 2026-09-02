# Build context is this repository root. No consumer-repository files are used.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS console
WORKDIR /build
# Keep lockfiles in their own layer: frontend-only source changes need not reinstall dependencies.
COPY console/package.json console/package-lock.json ./
RUN npm ci --ignore-scripts
COPY console/ ./
RUN npm run build

FROM python:3.14-alpine3.24@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

# Copyleft components this image distributes, recorded where someone can find
# them. Same shape a sibling repository uses for the AGPL MinIO client it
# ships: the upstream licence text, the exact upstream identity, and where to
# get the corresponding source.
#
# psycopg and psycopg-binary are LGPL-3.0-only and are unconditional runtime
# dependencies. This repository is MIT and merely imports them, which is fine at
# the source level -- but shipping them inside a container image is
# distribution, and distribution is what carries the obligation. Both wheels
# already install their own LICENSE.txt; it is copied to a discoverable path
# rather than left inside a dist-info directory nobody looks in.
#
# Neither package is modified here. That matters for LGPL 3.0 section 4: the
# relink/replace requirement is met by the library being an ordinary,
# unmodified, separately installable Python package -- `pip install
# psycopg==<other>` in a derived image replaces it, and SOURCE says so.
RUN python3 - <<'PY'
# Writes /usr/share/licenses/<name>/{LICENSE,SOURCE} for each copyleft runtime
# component, from the metadata pip actually installed rather than from anything
# restated here.
import importlib.metadata as md
import pathlib
import shutil

COPYLEFT = {"psycopg", "psycopg-binary"}
root = pathlib.Path("/usr/share/licenses")
for name in sorted(COPYLEFT):
    dist = md.distribution(name)
    licences = [f for f in (dist.files or []) if f.name.upper().startswith("LICENSE")]
    if not licences:
        raise SystemExit(f"{name} ships no LICENSE file; record it by hand before shipping")
    destination = root / name
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dist.locate_file(licences[0]), destination / "LICENSE")
    expression = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "unknown"
    (destination / "SOURCE").write_text(
        f"Component: {name} {dist.version}, {expression}\n"
        f"Upstream version: {dist.version}\n"
        f"Source: https://pypi.org/project/{name}/{dist.version}/#files\n"
        "Pinned by: requirements.lock (pip --require-hashes)\n"
        "Modifications: none; installed unmodified from the published wheel\n"
        f'Replacing it: pip install "{name}==<version>" in a derived image\n',
        encoding="utf-8",
    )
    assert (destination / "LICENSE").stat().st_size > 0, name
PY
COPY NOTICE /usr/share/licenses/NOTICE
# Keep the runtime image to the installed Python package's runtime files and the console
# output. Tests, local tooling, documentation, and frontend source are not runtime
# dependencies and must not enter the control-plane image.
COPY src/sites/ /app/sites/
# Copy output to the default SITES_CONSOLE_ROOT. There is intentionally no fallback:
# a failed build must fail here rather than producing an image whose /console/ breaks at
# runtime.
COPY --from=console /build/dist /app/console

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 65532:65532
CMD ["python3", "-m", "sites.operator"]
