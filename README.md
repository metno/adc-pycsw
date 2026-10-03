# pycsw_solr_metno

A set of tools and configuration templates for deploying [pycsw](https://pycsw.org/) with a Solr backend, tailored for the Norwegian Meteorological Institute (MET Norway) and related Arctic/Polar data catalogues.

## Features

- **Custom pycsw Solr Repository**: Implements `SolrMETNORepository` for integrating pycsw with Solr-based metadata backends.
- **Automated Configuration Generation**: Uses Jinja2 templates and YAML mapping to generate service-specific configuration and Docker Compose files.
- **Ready for Containerized Deployment**: Includes Docker Compose templates and example configs for rapid deployment.

## Repository Structure

- `pycsw_solr_metno/`: Custom pycsw repository plugin for Solr at MET Norway (`SolrMETNORepository`).
- `outputschemas/`: pycsw outputschema plugins (DIF, WMO, ...) built on the MMD XSLT transforms.
- `configurator/`: Tools for generating configs and templates.
  - `collection_mapping.py`: Main script to generate the pycsw config and docker stack file of each service from the endpoint definitions.
  - `csw_endpoints_sample.yml`: Example YAML mapping of CSW endpoints/services. Copy it to `csw_endpoints.yml` (gitignored).
  - `templates/`: Jinja2 templates for config and docker stack files.
    - `pycsw_cfg_yaml.j2`: pycsw configuration template.
    - `docker_compose_yaml.j2`: docker stack template for deployment.
- `update_service.sh`: Deploys / restarts a generated stack.
- `host_setup.sh`, `host/traefik.yml`: Prepare a new host (docker, swarm, Traefik, MMD checkout, parent list). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/MACHINE_SETUP.md](docs/MACHINE_SETUP.md).
- `fetch_collections.py`: Builds `/usr/local/share/csw/parent_list.xml` from the Solr parent records.
- `.env.example`: Template for the local `.env` holding the Solr credentials.
- `artifacts/` (gitignored): everything generated or disposable.
  - `deploy/<service>/`: configurator output — `pycsw.yml`, `stack.yml`, `pycsw.log`.
  - `test-reports/`: junit / html / log output of test runs.
  - `notes/`, `scratchpad/`: working notes and archived files.

## Usage

Setting up a new machine:

- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — the short path with `sh host_setup.sh --deploy`.
- [docs/MACHINE_SETUP.md](docs/MACHINE_SETUP.md) — every step by hand, from a bare server to running services, plus operations.
- [docs/MACHINE_SETUP_IP_PORT.md](docs/MACHINE_SETUP_IP_PORT.md) — the same without DNS/Traefik/HTTPS: each service on `http://<ip>:<port>`.

The sections below describe the individual pieces.

### 1. Provide the Solr credentials

Credentials are never written to the config or stack files. Put them in `.env`
(gitignored):

```bash
cp .env.example .env    # then fill in SOLR_USER / SOLR_PASS
chmod 600 .env
```

The generated `pycsw.yml` only contains `username: ${SOLR_USER}` /
`password: ${SOLR_PASS}`; pycsw expands them from the container environment,
which `update_service.sh` populates from `.env` at deploy time. Note that the
values are visible to anyone who can run `docker service inspect` on the swarm.

### 2. Generate Service Configurations

```bash
python configurator/collection_mapping.py            # all services
python configurator/collection_mapping.py test adc   # only these
```

Each service gets its own directory, `artifacts/deploy/<name>/`, holding
`pycsw.yml` and `stack.yml`. Use `--output-dir` to write elsewhere and
`--endpoints` to read another definitions file. The script refuses endpoint
definitions with a clear-text `repository.username` / `repository.password`.

### 3. Deploy

```bash
sh update_service.sh test             # full teardown + redeploy of the `test` stack
sh update_service.sh test --restart   # only restart the running container
sh update_service.sh --all            # redeploy every generated service
```

The script loads `.env`, exports `REPO_DIR` (the stack file mounts the plugin
sources from it) and deploys `artifacts/deploy/<name>/stack.yml`. Use
`--restart` after editing the plugin sources; redeploy after changing `.env` or
regenerating the files.

### 4. Customization

- Edit `configurator/csw_endpoints.yml` to define your own endpoints and metadata
  (see `csw_endpoints_sample.yml` for the optional keys and the `_defaults` /
  `_overrides` blocks, e.g. `docker.base_domain` to serve everything under another DNS name).
- Update Jinja2 templates in `configurator/templates/` as needed.

## Running the test suite

The test suite drives the live CSW endpoint at `https://test.wps.met.no/csw`
(and the matching STAC endpoint at `/stac`) — no local Solr or container is
required to run the tests.

```bash
# 1. Clone
git clone -b pycsw-solr-metno https://github.com/metno/adc-pycsw.git pycsw_solr_metno
cd pycsw_solr_metno

# 2. Create a venv (Python 3.12 — matches the test_pycsw container)
python3.12 -m venv .venv

# 3. Install test dependencies (pulls in pycsw + SQLAlchemy + Flask
#    + pygeofilter[fes] at the versions the container runs)
.venv/bin/pip install -r requirements-test.txt

# 4. Run the suite
.venv/bin/python -m pytest tests/
```

A green run reports **`511 passed, 26 skipped, 5 xfailed`** in ~80s.

- `pytest.ini` defaults to `-m "not slow"`. To include slow pagination tests:
  `.venv/bin/python -m pytest tests/ -m slow`.
- The 26 skips are AnyText literals the upstream pygeofilter→Solr translator
  rejects as "Invalid query syntax" — recorded as `pytest.skip()` so they
  don't fail the build.
- The 5 xfails are 3 known STAC search bugs (`?datetime=` returns 500) and 2
  XSLT transform cases where the MMD source omits an optional field.
- No `.env` / secrets needed to run the tests — the Solr credentials are only
  used by the server, not by the test client.
- To keep reports, write them under `artifacts/test-reports/`, e.g.
  `--junitxml=artifacts/test-reports/junit.xml`.

### Rebuilding the fixture

Only needed if the running service is repointed at a different Solr core
(`repository.filter` in `configurator/csw_endpoints.yml`, e.g. `devcore` →
`refcore`). The Solr basic-auth credentials are not in the repo — ask a
teammate and put them in `.env` (see above).

```bash
set -a; . ./.env; set +a     # SOLR_USER / SOLR_PASS
SOLR_URL=https://metsis-solr.met.no/solr/devcore \
.venv/bin/python tests/build_fixtures.py \
  --target 30 --collections ADC,NBS,GCW,NSDN,SIOS,SIOSCD
```

`--collections` must match the service's `adc_collections` allowlist exactly,
or records will end up in the fixture but be filtered out by the live
endpoint and tests will fail.

## Requirements

- Python 3.7+
- [pycsw](https://pycsw.org/) (for service)
- [Jinja2](https://palletsprojects.com/p/jinja/) and [PyYAML](https://pyyaml.org/) for config generation
- Docker in swarm mode (for deployment)

## Authors

- Massimo Di Stefano (@epifanio)
- Magnar Martinsen
- Tom Kralidis
- Lara Ferrighi

## License

MIT License. See source files for details.
