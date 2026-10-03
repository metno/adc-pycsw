# syntax=docker/dockerfile:1.6
#
# pycsw_solr_metno CI/dev image.
#
# Builds pycsw from git at a configurable branch, installs the MET Norway
# plugin sources + extra catalogue tooling, and bakes in the MMD XSLT
# transforms — producing a self-contained image suitable for GitHub Actions
# CI runs (no host bind-mounts required).
#
# Stages:
#   build    — install everything; run pytest if RUN_TESTS=true (never aborts)
#   reports  — scratch stage with just test-reports/ (extract on host)
#   image    — default; inherits `build`, gates on pytest exit code
#
# Build (production-lean, no test deps):
#   docker build -t pycsw-solr-metno:prod .
#
# Build + install test deps + run pytest as a CI gate:
#   docker build --build-arg RUN_TESTS=true -t pycsw-solr-metno:ci .
#
# Stream pytest output during build (BuildKit hides successful RUN output
# by default; on failure it already dumps the step's log):
#   docker build --progress=plain --build-arg RUN_TESTS=true -t img .
#
# Extract test reports (junit.xml, report.html, pytest.log, exit-code) to
# the host — works even when tests failed:
#   docker build --build-arg RUN_TESTS=true --target reports \
#                --output type=local,dest=./artifacts/test-reports .
#
# CI workflow: extract reports first (always succeeds), then validate:
#   docker build --build-arg RUN_TESTS=true --target reports \
#                --output type=local,dest=./artifacts/test-reports .
#   docker build --build-arg RUN_TESTS=true -t pycsw-solr-metno:ci .
# BuildKit caches the heavy layers — the second build is fast.
#
# Test scope:
#   Default PYTEST_ARGS=tests/ runs the full suite:
#     - tests/test_xslt_transforms.py        pure lxml, always runs
#     - tests/test_temporal_extent_utils.py  pure unit, always runs
#     - tests/test_csw_*.py                  hit https://test.wps.met.no/csw
#     - tests/test_stac_*.py                 hit https://test.wps.met.no/stac
#   The CSW/STAC files `pytest.skip` themselves if the live endpoint is
#   unreachable (e.g. air-gapped CI), so they don't break the build. When
#   reachable, they validate the LIVE deployed pycsw, not the freshly-built
#   image — to validate the freshly-built image you need to start pycsw +
#   Solr in a sibling container and point the tests at that (future work).
#
# Restrict scope (e.g. XSLT only, or only STAC):
#   docker build --build-arg RUN_TESTS=true \
#                --build-arg PYTEST_ARGS=tests/test_xslt_transforms.py \
#                -t pycsw-solr-metno:ci .
#   docker build --build-arg RUN_TESTS=true \
#                --build-arg PYTEST_ARGS="tests/ -k stac" \
#                -t pycsw-solr-metno:ci .
# (PYTEST_ARGS is splat unquoted in the pytest call, so multi-token values
# work via --build-arg even though the in-Dockerfile default is single-token.)
#
# Override pycsw / mmd branches:
#   docker build --build-arg PYCSW_BRANCH=main \
#                --build-arg MMD_BRANCH=v4.0 \
#                -t pycsw-solr-metno:main .
#
# Run pycsw (CI-style — config baked in):
#   docker run --rm -p 8000:8000 pycsw-solr-metno:ci
#
# Run pytest inside an already-built image (requires RUN_TESTS=true at build):
#   docker run --rm --entrypoint /venv/bin/pytest pycsw-solr-metno:ci \
#       /home/pycsw/tests/
#
# Run with a generated config at runtime (see configurator/; the Solr
# credentials it references come from .env):
#   docker run --rm --env-file .env \
#       -v $(pwd)/artifacts/deploy/test/pycsw.yml:/etc/pycsw/pycsw.yml \
#       pycsw-solr-metno:ci
#

ARG PYTHON_TAG=3.12-slim
FROM python:${PYTHON_TAG} AS build

# --- Build-time configuration ------------------------------------------------
ARG PYCSW_REPO=https://github.com/geopython/pycsw.git
ARG PYCSW_BRANCH=repo-abstract
ARG MMD_REPO=https://github.com/metno/mmd.git
ARG MMD_BRANCH=master
ARG PYGEOFILTER_URL=https://github.com/geopython/pygeofilter/archive/main.zip
ARG PYGEOMETA_URL=git+https://github.com/geopython/pygeometa
ARG PYWCMP_URL=git+https://github.com/World-Meteorological-Organization/pywcmp

# Build-time test toggle:
#   RUN_TESTS=true     → install test deps into /venv AND run pytest as a
#                        build step (failed tests fail the build).
#   PYTEST_ARGS=...    → forwarded to pytest when RUN_TESTS=true. Defaults to
#                        the XSLT-only suite (the rest need a live CSW + Solr).
ARG RUN_TESTS=false
ARG PYTEST_ARGS=tests/

# Site-packages path inside /venv — pinned to the python:3.12 minor version.
# Bump if PYTHON_TAG is changed to a different minor.
ARG SITE_PACKAGES=/venv/lib/python3.12/site-packages

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYCSW_CONFIG=/etc/pycsw/pycsw.yml \
    PATH=/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# --- System packages ---------------------------------------------------------
# ca-certificates + git: clone pycsw and pip-install git+... URLs.
# build-essential + dev libs: fall back to source builds when no manylinux
# wheel exists for the target arch (lxml, shapely, pyproj).
RUN apt-get update && \
    apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        build-essential \
        libxml2-dev libxslt1-dev \
        libgeos-dev libproj-dev \
        libssl-dev libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# --- pycsw user (uid 1000, matches the base image we replaced) ---------------
RUN adduser --uid 1000 --gecos '' --disabled-password pycsw

# --- Clone pycsw + MET MMD repo at the chosen branches -----------------------
# mmd lives next to pycsw at /home/pycsw/mmd so test_xslt_transforms.py
# resolves REPO/mmd/xsd and REPO/mmd/xslt off the tests/ parent. xslt and
# thesauri are also symlinked into /usr/local/share/mmd/ where the runtime
# outputschemas (dif.py, dif10.py, wmo.py) hardcode their paths.
WORKDIR /home/pycsw
RUN git clone --depth 1 --branch "${PYCSW_BRANCH}" "${PYCSW_REPO}" pycsw && \
    git clone --depth 1 --branch "${MMD_BRANCH}" "${MMD_REPO}" mmd && \
    mkdir -p /usr/local/share/mmd && \
    ln -s /home/pycsw/mmd/xslt /usr/local/share/mmd/xslt && \
    ln -s /home/pycsw/mmd/thesauri /usr/local/share/mmd/thesauri && \
    chown -R pycsw:pycsw /home/pycsw /usr/local/share/mmd

# --- /venv: install pycsw, pygeofilter (with Magnar's fixes from main),
#     and the extra catalogue tooling --------------------------------------
RUN python3 -m venv /venv \
 && /venv/bin/pip install --upgrade pip setuptools wheel
WORKDIR /home/pycsw/pycsw
RUN /venv/bin/pip install --no-cache-dir \
        --requirement requirements.txt \
        --requirement requirements-standalone.txt \
        psycopg2-binary gunicorn \
 && /venv/bin/pip install --no-cache-dir --force-reinstall "${PYGEOFILTER_URL}" \
 && /venv/bin/pip install --no-cache-dir . \
 && /venv/bin/pip install --no-cache-dir "${PYGEOMETA_URL}" "${PYWCMP_URL}"

# Test runner deps — installed only when RUN_TESTS=true so production builds
# stay lean. Subset of requirements-test.txt; the rest are pycsw transitive deps.
RUN if [ "${RUN_TESTS}" = "true" ]; then \
        /venv/bin/pip install --no-cache-dir \
            pytest pytest-html \
            shapely OWSLib pygeoif geolinks requests ; \
    fi

# --- pycsw runtime config + entrypoint ---------------------------------------
# The tracked template only — never bake a deployment config into the image.
COPY --chown=pycsw:pycsw pycsw_cfg_template.yml /etc/pycsw/pycsw.yml
RUN install -m 0755 /home/pycsw/pycsw/docker/entrypoint.py /usr/local/bin/entrypoint.py \
 && mkdir -p /usr/local/share/csw \
 && chown -R pycsw:pycsw /usr/local/share/csw

# --- MET Norway plugin: install as a real Python package --------------------
# Installs into /venv so pycsw loads it via cfg.yml's
# `source: pycsw_solr_metno.solr_metno.SolrMETNORepository`.
# Tests' `from pycsw_solr_metno.solr_metno import ...` resolves to the same
# installed module — single source of truth, no smuggling into pycsw's tree.
COPY --chown=pycsw:pycsw pyproject.toml      /home/pycsw/plugin/pyproject.toml
COPY --chown=pycsw:pycsw pycsw_solr_metno/   /home/pycsw/plugin/pycsw_solr_metno/
COPY --chown=pycsw:pycsw README.md           /home/pycsw/plugin/README.md
RUN /venv/bin/pip install --no-cache-dir /home/pycsw/plugin
RUN /venv/bin/pip install niquests validators

# Outputschemas are still dropped into pycsw's own plugins/outputschemas/
# directory — pycsw discovers them by scanning that tree, not via dotted-path
# config, so they don't fit the same package model as the repository plugin.
COPY --chown=pycsw:pycsw outputschemas/      ${SITE_PACKAGES}/pycsw/plugins/outputschemas/

# MMD XSLT + thesauri come from the metno/mmd clone in the earlier step
# (mounted at /home/pycsw/mmd, symlinked into /usr/local/share/mmd).

# Test suite — baked in so CI can `docker run … pytest tests/`.
COPY --chown=pycsw:pycsw tests/             /home/pycsw/tests/
COPY --chown=pycsw:pycsw pytest.ini requirements-test.txt /home/pycsw/

# --- Optional: run pytest during build (CI gate) -----------------------------
# Only fires when RUN_TESTS=true. Defaults to the pure-lxml XSLT suite, which
# needs no external Solr/CSW endpoint. Override which tests run with:
#   --build-arg PYTEST_ARGS=tests/test_xslt_transforms.py
#
# Reports are written to /home/pycsw/test-reports/ regardless of pass/fail:
#   junit.xml    — machine-readable, for CI ingestion (GH Actions, etc.)
#   report.html  — human-readable
#   pytest.log   — raw stdout+stderr of the run
#   exit-code    — pytest's exit code (0 = pass; checked by the `image` stage)
#
# This step is deliberately written to NEVER fail the build itself — so the
# `reports` stage below can extract the artifacts even when tests failed. The
# pass/fail gate happens in the `image` stage further down.
WORKDIR /home/pycsw
RUN mkdir -p /home/pycsw/test-reports && \
    if [ "${RUN_TESTS}" = "true" ]; then \
        ( /venv/bin/pytest -v \
            --junitxml=/home/pycsw/test-reports/junit.xml \
            --html=/home/pycsw/test-reports/report.html \
            --self-contained-html \
            ${PYTEST_ARGS} \
            2>&1 ; echo $? > /home/pycsw/test-reports/exit-code \
        ) | tee /home/pycsw/test-reports/pytest.log ; \
    else \
        echo "RUN_TESTS=false — skipped" | tee /home/pycsw/test-reports/pytest.log ; \
        echo 0 > /home/pycsw/test-reports/exit-code ; \
    fi

# --- Reports export stage ----------------------------------------------------
# A scratch stage that contains ONLY the test reports. Extract with:
#   docker build --build-arg RUN_TESTS=true \
#                --target reports --output type=local,dest=./artifacts/test-reports .
# Produces ./artifacts/test-reports/{junit.xml,report.html,pytest.log,exit-code} on the
# host — even when the tests failed. The build stage above never aborts on
# pytest failure, so this target always succeeds whenever `build` does.
FROM scratch AS reports
COPY --from=build /home/pycsw/test-reports/ /

# --- Final runtime image -----------------------------------------------------
# Inherits everything from `build`, then gates on the pytest exit code: if
# RUN_TESTS=true and pytest failed, this RUN exits non-zero and the build
# fails — but only AFTER the `reports` stage has been able to capture logs.
FROM build AS image
RUN ec=$(cat /home/pycsw/test-reports/exit-code 2>/dev/null || echo 0) && \
    if [ "$ec" != "0" ]; then \
        echo "pytest exited with $ec — see /home/pycsw/test-reports/pytest.log" >&2 ; \
        exit "$ec" ; \
    fi

USER pycsw
EXPOSE 8000
WORKDIR /home/pycsw/pycsw
ENTRYPOINT ["/venv/bin/python3", "/usr/local/bin/entrypoint.py"]
