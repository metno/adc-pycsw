# Deploying on a new host

From a bare machine to running pycsw services behind Traefik. Everything is
driven by three files you provide and two scripts:

| You provide | What it is |
|---|---|
| `.env` | Solr credentials and host settings (never committed) |
| `configurator/csw_endpoints.yml` | The service definitions (never committed) |
| DNS records | Every service domain must point at the host |

| Script | What it does |
|---|---|
| `host_setup.sh` | Installs docker, initialises the swarm and Traefik, fetches the MMD transforms and the parent list, generates the configs. Safe to re-run. |
| `update_service.sh` | Deploys / redeploys / restarts a generated service |

This page is the short path. [MACHINE_SETUP.md](MACHINE_SETUP.md) walks
through the same setup by hand, from a bare server: OS preparation, firewall,
Docker installation, swarm, Traefik, configuration, deployment, and day-to-day
operations. [MACHINE_SETUP_IP_PORT.md](MACHINE_SETUP_IP_PORT.md) is the same
without DNS, Traefik or HTTPS: every service is published on
`http://<server ip>:<port>`, which is handy for testing before the DNS
records exist.

## Requirements

- A Debian/Ubuntu host (other distributions work, but `host_setup.sh` only
  installs missing packages through `apt-get`), with `sudo` rights.
- Ports **80 and 443** reachable from the internet. Certificates come from
  Let's Encrypt through the TLS challenge on port 443.
- Outbound access to the Solr server, Docker Hub and GitHub.
- Roughly 1 GB of RAM per service with the default settings; see
  [Sizing](#sizing) for a small test machine.

## 1. Clone

```bash
git clone -b pycsw-solr-metno https://github.com/metno/adc-pycsw.git pycsw_solr_metno
cd pycsw_solr_metno
```

## 2. Settings: `.env`

```bash
cp .env.example .env && chmod 600 .env
```

| Variable | Needed | Purpose |
|---|---|---|
| `SOLR_USER`, `SOLR_PASS` | yes | Solr basic-auth credentials used by every service |
| `ACME_EMAIL` | yes | Contact address for the Let's Encrypt certificates |
| `PARENT_LIST_SOLR_URL` | recommended | Solr core listing the parent datasets, e.g. `https://metsis-solr.met.no/solr/adc`. Without it the ISO/DIF output has no parent/child links. |
| `SWARM_ADVERTISE_ADDR` | only with several network interfaces | Address passed to `docker swarm init` |

## 3. Service definitions: `configurator/csw_endpoints.yml`

The file is not in the repository. Copy it from the host you are replicating,
or start from the sample:

```bash
scp oldhost:dev/pycsw_solr_metno/configurator/csw_endpoints.yml configurator/
# or
cp configurator/csw_endpoints_sample.yml configurator/csw_endpoints.yml
```

To serve the same definitions under this host's DNS name, add one block at the
top instead of editing every entry:

```yaml
_overrides:
  docker:
    base_domain: newhost.example.org   # each service becomes <name>.newhost.example.org
```

`_overrides` wins over the individual entries; `base_domain` rewrites both the
Traefik host rule and the URL pycsw advertises in its capabilities document.

Remove entries that cannot run on this host. In the file used on the test
machine that means `rebased` and `adc-rebased`: they use locally built images
and a plugin directory that only exist there. Alternatively list the services
you want on the command line in step 5.

### Sizing

Every service reserves 0.5 CPU and 1 GB by default, and the definitions ask
for 16 gunicorn workers. A service whose reservation cannot be met stays
pending. For a small test machine:

```yaml
_overrides:
  docker:
    base_domain: newhost.example.org
    reservations: false
  server:
    workers: 2
```

## 4. DNS

Create the records **before** deploying: `<name>.<base_domain>` for every
service (a wildcard `*.newhost.example.org` record is the simplest). Traefik
asks Let's Encrypt for a certificate as soon as a service starts, and failed
validations are rate-limited (5 per hostname per hour).

## 5. Set up the host and deploy

```bash
sh host_setup.sh              # prepare everything, generate the configs
sh host_setup.sh --deploy     # ... and deploy every service
sh host_setup.sh --deploy adc nbs    # ... or only some of them
```

The script prints a TODO list and exits with status 2 when something is
missing (typically `.env` values or the endpoints file); fix it and run the
script again. On a host where docker was just installed it stops once after
adding you to the `docker` group: log out and back in, then re-run.

What it does, in order:

1. creates `.env` from `.env.example` if missing;
2. installs `git`, `curl`, `python3-venv` and docker (`get.docker.com`) if missing;
3. `docker swarm init` and the `traefik-public` overlay network;
4. deploys Traefik from `host/traefik.yml` (skipped if a `traefik` stack
   exists, or with `--skip-traefik`);
5. clones [metno/mmd](https://github.com/metno/mmd) into `mmd/` (XSLT and
   thesauri mounted into the containers);
6. writes `/usr/local/share/csw/parent_list.xml` (`fetch_collections.py`), or
   an empty placeholder when `PARENT_LIST_SOLR_URL` is not set;
7. creates `.venv` with the configurator's dependencies;
8. generates `artifacts/deploy/<service>/{pycsw.yml,stack.yml}`;
9. with `--deploy`, runs `update_service.sh` for the services.

## 6. Check

```bash
docker service ls                         # every service should be 1/1
curl -s "https://<name>.<base_domain>/csw?service=CSW&version=2.0.2&request=GetCapabilities" | head
curl -s "https://<name>.<base_domain>/stac/search?limit=1"
```

A new service takes up to ~30 s to become reachable: Traefik polls the swarm
every 15 s and then requests the certificate.

To run the test suite against a service on this host:

```bash
.venv/bin/pip install -r requirements-test.txt
CSW_URL=https://test.<base_domain>/csw STAC_URL=https://test.<base_domain>/stac \
    .venv/bin/python -m pytest tests/
```

The fixtures must have been built against the Solr core and collections that
service uses (see the README).

## Day-to-day

```bash
# after editing configurator/csw_endpoints.yml or a template
.venv/bin/python configurator/collection_mapping.py [service ...]
sh update_service.sh <service>            # or --all

# after editing the plugin sources (pycsw_solr_metno/, outputschemas/)
sh update_service.sh <service> --restart

# after changing .env
sh update_service.sh --all
```

Keep the parent list fresh with a cron entry (it only needs the system python):

```cron
0 4 * * * cd /path/to/pycsw_solr_metno && set -a && . ./.env && set +a && /usr/bin/python3 fetch_collections.py >> artifacts/fetch_collections.log 2>&1
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `required variable SOLR_USER is missing a value` | `.env` is missing or incomplete |
| Service stays `0/1`, `docker service ps <name>_pycsw --no-trunc` shows `bind source path does not exist` | `mmd/` or `/usr/local/share/csw/parent_list.xml` is missing, or the files were not generated — re-run `host_setup.sh` |
| Service stays `0/1` with `insufficient resources` | Reservations exceed the machine, see [Sizing](#sizing) |
| Service stays `0/1` with `No such image` | The entry uses a locally built image; build it (`docker build -t <image>:<tag> .`) or change `docker.image` |
| Container restarts, log shows `Undefined environment variable in config` | The generated `pycsw.yml` references a variable the stack did not receive; regenerate and redeploy |
| `404 page not found` from Traefik | Service not registered yet (wait 15 s), or the request's host name does not match the generated domain |
| Browser warns about the `TRAEFIK DEFAULT CERT` | Let's Encrypt could not validate: check DNS, port 443, and `docker service logs traefik_traefik` |
| CSW tests hit another host | `server.url` in `pycsw.yml` is advertised in GetCapabilities and followed by OWSLib; check that the domain was overridden (`base_domain`) |

Security notes: the Solr credentials are container environment variables, so
anyone who can run `docker service inspect` on the swarm can read them. The
Traefik dashboard is not exposed.
