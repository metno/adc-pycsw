# Machine setup: from a bare server to running services

This guide covers every step by hand, from a freshly installed server to
pycsw services answering over HTTPS. It is the manual equivalent of
`host_setup.sh`. Use it to see what the script does, to set up a host where
the script cannot run unattended, or to repair one step on its own.

If you just want the result, the short path is in
[DEPLOYMENT.md](DEPLOYMENT.md): fill in `.env` and the endpoints file, then
run `sh host_setup.sh --deploy`.

The commands assume **Ubuntu 22.04 or 24.04** (Debian works with the
`ubuntu` → `debian` substitution noted in step 3). The examples use a host
called `newhost.example.org` and a non-root user with sudo rights (any
name: `deploy` below, `ubuntu` on Ubuntu cloud images).

Contents:

1. [Server prerequisites](#1-server-prerequisites)
2. [Operating system basics](#2-operating-system-basics)
3. [Install Docker](#3-install-docker)
4. [Initialise the swarm](#4-initialise-the-swarm)
5. [Get the repository and its data](#5-get-the-repository-and-its-data)
6. [Deploy Traefik](#6-deploy-traefik)
7. [Configure](#7-configure)
8. [Generate and deploy the services](#8-generate-and-deploy-the-services)
9. [Verify](#9-verify)
10. [Operations](#10-operations)
11. [Starting over](#11-starting-over)

---

## 1. Server prerequisites

| Item | Requirement |
|---|---|
| OS | Ubuntu 22.04 / 24.04 LTS, 64-bit |
| CPU / RAM | about 0.5 CPU and 1 GB RAM per pycsw service with the default reservations; 4 CPU / 8 GB is comfortable for a handful of services with reduced settings (see [Sizing](DEPLOYMENT.md#sizing)) |
| Disk | 20 GB free under `/var/lib/docker` (the pycsw image is about 0.9 GB) |
| Inbound | TCP **22** (ssh), **80** and **443** from the internet. Let's Encrypt validates certificates on port 443. |
| Outbound | HTTPS to the Solr server, Docker Hub, GitHub and Let's Encrypt |
| DNS | One A (and/or AAAA) record per service, `<name>.<base_domain>`, pointing at the server. A wildcard `*.newhost.example.org` is simplest. |
| Access | A Solr account allowed to read the cores used by the services |

Create the DNS records now. They need time to propagate, and every service
asks Let's Encrypt for a certificate as soon as it starts. Failed validations
are rate-limited to 5 per hostname per hour.

```bash
# from any machine: should print the server's public address
dig +short adc.newhost.example.org
```

## 2. Operating system basics

Run as root, or with sudo, once:

```bash
# updates and the tools used later
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y git curl ca-certificates python3 python3-venv

# a non-root user for running the services: skip if you already log in as
# one with sudo rights, such as `ubuntu` on Ubuntu cloud images
sudo adduser deploy
sudo usermod -aG sudo deploy

# correct time matters for TLS and for log timestamps
timedatectl status          # "System clock synchronized: yes"
```

Firewall: if you use `ufw`, allow ssh, http and https:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

> Docker manages its own iptables rules, and ports it publishes bypass `ufw`.
> Do not rely on `ufw` to hide a container port; only Traefik publishes ports
> in this setup (80 and 443).

Log in as that user for the rest of the guide. Nothing depends on its name.

## 3. Install Docker

`host_setup.sh` uses Docker's convenience script
(`curl -fsSL https://get.docker.com | sudo sh`). The manual equivalent from
Docker's apt repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

(On Debian, replace `ubuntu` with `debian` in both URLs.)

Cap container log size before anything runs. pycsw logs at DEBUG level, and
Docker's default json-file logs grow without limit:

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
EOF
sudo systemctl enable --now docker
sudo systemctl restart docker
```

Let your user use docker without sudo. Group membership applies at the next
login:

```bash
sudo usermod -aG docker "$USER"
# log out and back in (or: newgrp docker), then
docker info > /dev/null && echo ok
```

## 4. Initialise the swarm

The services are deployed as swarm stacks, on a single-node swarm:

```bash
docker swarm init
# with several network interfaces docker asks which one to use:
# docker swarm init --advertise-addr <server address>
```

Create the overlay network that Traefik and every pycsw service join:

```bash
docker network create --driver overlay --attachable traefik-public
```

> The stacks bind-mount files from this machine (the plugin sources,
> `mmd/`, `parent_list.xml`) and are pinned to manager nodes. Adding nodes
> to the swarm needs those files on each manager, plus ports 2377/tcp,
> 7946/tcp+udp and 4789/udp between the nodes. A single node is the tested
> setup.

## 5. Get the repository and its data

```bash
cd ~
git clone -b pycsw-solr-metno https://github.com/metno/adc-pycsw.git pycsw_solr_metno
cd pycsw_solr_metno
```

**MMD transforms.** The MMD → ISO/DIF/WMO XSLT and the thesauri are
bind-mounted into every container from `mmd/`:

```bash
git clone --depth 1 https://github.com/metno/mmd.git mmd
```

**Parent list.** `/usr/local/share/csw/parent_list.xml` lists the parent
datasets for the XSLT transforms. It is bind-mounted into every container, so
it **must exist** before deploying; swarm refuses to start a task whose
mount source is missing:

```bash
sudo mkdir -p /usr/local/share/csw
sudo chown "$(id -u):$(id -g)" /usr/local/share/csw
```

It is filled in step 7, once the credentials are in `.env`.

**Configurator environment:**

```bash
python3 -m venv .venv
.venv/bin/pip install -r configurator/requirements.txt
```

(`requirements-test.txt` is only needed to run the test suite.)

## 6. Deploy Traefik

Traefik terminates TLS, gets certificates from Let's Encrypt and routes each
domain to its pycsw service. `host/traefik.yml` in the repository provides
everything the generated stack files expect:

| Provided | Used by the generated stack files as |
|---|---|
| entrypoints `http` (:80), `https` (:443) | `...entrypoints=http` / `https` |
| certificate resolver `le` (TLS challenge) | `...tls.certresolver=le` |
| middleware `https-redirect` | http router → https |
| swarm provider filtered on `traefik.constraint-label=traefik-public` | `traefik.constraint-label=traefik-public` |

```bash
cd ~/pycsw_solr_metno
ACME_EMAIL=you@example.org docker stack deploy -c host/traefik.yml traefik
```

`ACME_EMAIL` must be a real address: Let's Encrypt rejects placeholder
domains such as `example.org`. The image tag defaults to `v3.6`; override it
with `TRAEFIK_TAG=...`. Certificates are stored in the
`traefik_traefik-public-certificates` volume and survive redeploys.

Check:

```bash
docker service ls --filter name=traefik     # REPLICAS 1/1
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/   # 404 = Traefik answers, no route yet
```

## 7. Configure

### `.env`: credentials and host settings

```bash
cp .env.example .env
chmod 600 .env
nano .env        # or any editor: vi .env, ...
```

| Variable | Purpose |
|---|---|
| `SOLR_USER`, `SOLR_PASS` | Solr basic auth. Single-quote the password if it has shell special characters. |
| `ACME_EMAIL` | Let's Encrypt contact (used by `host/traefik.yml`) |
| `PARENT_LIST_SOLR_URL` | Solr core with the parent datasets: the same core as the services' `repository.filter`, normally `https://metsis-solr.met.no/solr/adc2` |

`.env` is gitignored and is the **only** place credentials are written. The
generated files only reference `${SOLR_USER}` / `${SOLR_PASS}`.

Now fill the parent list:

```bash
set -a; . ./.env; set +a
python3 fetch_collections.py           # writes /usr/local/share/csw/parent_list.xml
```

Without Solr access you can deploy with an empty list for now; ISO/DIF output
then has no parent/child links:

```bash
printf '<?xml version="1.0" ?>\n<parent/>\n' > /usr/local/share/csw/parent_list.xml
```

### `configurator/csw_endpoints.yml`: the services

This file defines one entry per service. It is gitignored: copy it from the
host you are replicating, or start from the commented sample:

```bash
scp oldhost:dev/pycsw_solr_metno/configurator/csw_endpoints.yml configurator/
# or
cp configurator/csw_endpoints_sample.yml configurator/csw_endpoints.yml
```

To serve the services under this machine's DNS name and size them for it,
put an `_overrides` block at the top:

```yaml
_overrides:
  docker:
    base_domain: newhost.example.org   # every service becomes <name>.newhost.example.org
    reservations: false                # no CPU/RAM reservations
  server:
    workers: 2                         # gunicorn workers per service
```

Remove, or skip in step 8, entries whose image does not exist on this machine,
for example images built locally on another host. To build the image from
this repository instead:

```bash
docker build -t pycsw-solr-metno:local .
```

Then point the entries at it with `_defaults` (or per entry):

```yaml
_defaults:
  docker:
    image: pycsw-solr-metno
    image_tag: local
```

All available keys are documented in `configurator/csw_endpoints_sample.yml`.

## 8. Generate and deploy the services

```bash
.venv/bin/python configurator/collection_mapping.py            # all services
.venv/bin/python configurator/collection_mapping.py adc nbs    # or some
```

Each service gets `artifacts/deploy/<name>/` with `pycsw.yml`, `stack.yml`
and `pycsw.log`. The script prints the URL each service will be served at;
check that they match your DNS records.

Deploy:

```bash
sh update_service.sh adc        # one service
sh update_service.sh --all      # every generated service
```

`update_service.sh` loads `.env`, exports `REPO_DIR` (the stack files mount
the plugin sources from the repository), checks that every variable is set,
removes the stack if it exists, waits for it to be gone and deploys it again.

## 9. Verify

```bash
docker service ls                                   # every service 1/1
docker service ps adc_pycsw --no-trunc              # why a task is not running
docker service logs -f adc_pycsw                    # pycsw / gunicorn log
docker service logs traefik_traefik | grep -i acme  # certificate requests
```

From any machine:

```bash
B=newhost.example.org
curl -sI http://adc.$B/ | head -1                                   # 301 → https
curl -s "https://adc.$B/csw?service=CSW&version=2.0.2&request=GetCapabilities" | head -5
curl -s "https://adc.$B/stac/search?limit=1" | head -c 300; echo
```

The capabilities document must advertise `https://adc.newhost.example.org/csw`.
OWSLib clients follow that URL, so a wrong `server.url` silently sends
requests to another host.

A new service can take about 30 s to become reachable: Traefik polls the swarm
every 15 s and then requests the certificate. Until the certificate arrives,
browsers show `TRAEFIK DEFAULT CERT`.

See [Troubleshooting](DEPLOYMENT.md#troubleshooting) for the usual failures.

## 10. Operations

| Task | Command |
|---|---|
| Change a service (collections, Solr core, image, ...) | edit `configurator/csw_endpoints.yml`, `.venv/bin/python configurator/collection_mapping.py <name>`, `sh update_service.sh <name>` |
| Pick up edited plugin sources (`pycsw_solr_metno/`, `outputschemas/`) | `sh update_service.sh <name> --restart` |
| Change credentials | edit `.env`, `sh update_service.sh --all` (`--restart` keeps the old values) |
| Update the code | `git pull`, regenerate, `sh update_service.sh --all` |
| Pull a newer `latest` image | `sh update_service.sh <name>` (the deploy resolves the tag again) |
| Update MMD transforms | `git -C mmd pull`, then `--restart` the services |
| Remove a service | `docker stack rm <name>`, delete its entry and `artifacts/deploy/<name>/` |

Refresh the parent list daily with cron (`crontab -e`). Only the system python
is needed:

```cron
0 4 * * * cd $HOME/pycsw_solr_metno && set -a && . ./.env && set +a && /usr/bin/python3 fetch_collections.py >> artifacts/fetch_collections.log 2>&1
```

The file is rewritten in place, so running containers see the new content
without a restart.

**Back up:** `.env`, `configurator/csw_endpoints.yml`, and the
`traefik_traefik-public-certificates` volume. Everything under `artifacts/`
can be regenerated.

## 11. Starting over

```bash
# remove the services
for d in artifacts/deploy/*/; do docker stack rm "$(basename "$d")"; done
# remove Traefik (certificates stay in the volume unless you delete it)
docker stack rm traefik
# regenerate from scratch
rm -rf artifacts/deploy
```

To leave the swarm entirely: `docker swarm leave --force`. This also removes
the `traefik-public` network.
