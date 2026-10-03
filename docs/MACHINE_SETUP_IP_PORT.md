# Machine setup without DNS: services on `http://<ip>:<port>`

A variant of [MACHINE_SETUP.md](MACHINE_SETUP.md) for testing a deployment
before any DNS records exist. There is no Traefik, no domain names and no
TLS: every pycsw service runs in docker swarm and publishes its own port on
the server, and clients reach it at `http://<server ip>:<port>`.

Moving to the DNS/HTTPS setup later only means changing one block in the
endpoints file and deploying Traefik. See
[Switching to DNS and HTTPS](#switching-to-dns-and-https).

| | This guide | [MACHINE_SETUP.md](MACHINE_SETUP.md) |
|---|---|---|
| URL | `http://10.20.30.40:8003` | `https://adc.newhost.example.org` |
| Needs | the server's IP and a free port range | a DNS record per service, ports 80/443 open to the internet |
| Reverse proxy / TLS | none | Traefik + Let's Encrypt |
| CORS headers | none (see [Limitations](#limitations)) | added by Traefik |

The commands assume **Ubuntu 22.04 or 24.04**. The examples use the server
address `10.20.30.40`, ports from `8001`, and a non-root user with sudo
rights (any name: `deploy` below, `ubuntu` on Ubuntu cloud images).

Contents:

1. [Server prerequisites](#1-server-prerequisites)
2. [Operating system basics](#2-operating-system-basics)
3. [Install Docker](#3-install-docker)
4. [Initialise the swarm](#4-initialise-the-swarm)
5. [Get the repository and its data](#5-get-the-repository-and-its-data)
6. [Configure](#6-configure)
7. [Generate and deploy the services](#7-generate-and-deploy-the-services)
8. [Verify](#8-verify)
9. [Operations](#9-operations)
10. [Starting over](#10-starting-over)

Shortcut: after steps 1–2, with `.env` and the endpoints file from step 6 in
place, `sh host_setup.sh --skip-traefik --deploy` does steps 3–7 for you.

---

## 1. Server prerequisites

| Item | Requirement |
|---|---|
| OS | Ubuntu 22.04 / 24.04 LTS, 64-bit |
| CPU / RAM | about 0.5 CPU and 1 GB RAM per pycsw service with the default reservations; 4 CPU / 8 GB is comfortable for a handful of services with reduced settings (see step 6) |
| Disk | 20 GB free under `/var/lib/docker` (the pycsw image is about 0.9 GB) |
| Inbound | TCP **22** (ssh), plus **one port per service** (e.g. 8001–8020) from the networks the test clients are on |
| Outbound | HTTPS to the Solr server, Docker Hub and GitHub |
| Address | The IP (or any host name) clients will use to reach the server. It is written into each service's configuration (see step 6). |
| Access | A Solr account allowed to read the cores used by the services |

Find the address and make sure the port range is free:

```bash
hostname -I                        # the server's addresses
ss -ltn | grep -E ':80(0[1-9]|1[0-9]|20)\b' || echo "8001-8020 free"
```

## 2. Operating system basics

Run as root, or with sudo, once:

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y git curl ca-certificates python3 python3-venv

# a non-root user for running the services: skip if you already log in as
# one with sudo rights, such as `ubuntu` on Ubuntu cloud images
sudo adduser deploy
sudo usermod -aG sudo deploy
```

Firewall: if you use `ufw`, allow ssh and the service ports:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8001:8020/tcp
sudo ufw enable
```

> **The service ports are reachable even if `ufw` says otherwise.** Docker
> manages its own iptables rules, and ports it publishes bypass `ufw`. Every
> port in step 7 is open to anything that can reach the server. Restrict who
> can connect at the network level (security groups, IT firewall), and keep
> this setup on an internal network: it is plain HTTP.

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
# docker swarm init --advertise-addr 10.20.30.40
```

That is all. The `traefik-public` network is not needed in this setup, though
`host_setup.sh` creates it anyway, which is harmless.

> Published ports go through the swarm routing mesh: a service answers on
> its port on every node of the swarm. The stacks also bind-mount files from
> this machine and are pinned to manager nodes, so a single node is the
> tested setup.

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

It is filled in step 6, once the credentials are in `.env`.

**Configurator environment:**

```bash
python3 -m venv .venv
.venv/bin/pip install -r configurator/requirements.txt
```

(`requirements-test.txt` is only needed to run the test suite.)

## 6. Configure

### `.env`: credentials

```bash
cp .env.example .env
chmod 600 .env
nano .env        # or any editor: vi .env, ...
```

| Variable | Purpose |
|---|---|
| `SOLR_USER`, `SOLR_PASS` | Solr basic auth. Single-quote the password if it has shell special characters. |
| `PARENT_LIST_SOLR_URL` | Solr core with the parent datasets: the same core as the services' `repository.filter`, normally `https://metsis-solr.met.no/solr/adc2` |

`ACME_EMAIL` is not needed: there is no Traefik. `.env` is gitignored and is
the **only** place credentials are written; the generated files only
reference `${SOLR_USER}` / `${SOLR_PASS}`. The credentials are used by the
containers to talk to Solr and never reach the HTTP clients.

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

Switch every service to direct ports with one block at the top of the file.
The entries themselves stay untouched:

```yaml
_overrides:
  docker:
    expose: port            # publish a port instead of going through Traefik
    host: 10.20.30.40       # address clients use; becomes http://10.20.30.40:<port>
    base_port: 8001         # first port
    reservations: false     # no CPU/RAM reservations (small test machine)
  server:
    workers: 2              # gunicorn workers per service
```

How the ports are assigned:

- Each service gets `base_port` plus its position in the file: the first
  entry gets 8001, the second 8002, and so on. The position is counted over
  **all** entries, so a service keeps its port when you generate or deploy
  only some of them.
- Adding or reordering entries shifts the ports. To fix a service's port, set
  `docker.port` in that entry; it takes precedence over `base_port`.
- The configurator refuses two services publishing the same port.

The address in `host` is written into each service's `pycsw.yml` as
`server.url`. pycsw advertises it in its capabilities document, and OWSLib
clients follow it for every request after GetCapabilities, so it must be an
address the **clients** can reach, not `localhost`.

Remove, or skip in step 7, entries whose image does not exist on this machine,
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

## 7. Generate and deploy the services

```bash
.venv/bin/python configurator/collection_mapping.py            # all services
.venv/bin/python configurator/collection_mapping.py adc nbs    # or some
```

The script prints the URL of each service, for example:

```
  .../artifacts/deploy/adc  ->  http://10.20.30.40:8003
  .../artifacts/deploy/nbs  ->  http://10.20.30.40:8006
```

Each service gets `artifacts/deploy/<name>/` with `pycsw.yml`, `stack.yml`
and `pycsw.log`; the stack file publishes the service's port and has no
Traefik labels.

Deploy:

```bash
sh update_service.sh adc        # one service
sh update_service.sh --all      # every generated service
```

`update_service.sh` loads `.env`, exports `REPO_DIR` (the stack files mount
the plugin sources from the repository), checks that every variable is set,
removes the stack if it exists, waits for it to be gone and deploys it again.

Or let `host_setup.sh` do steps 3–7. `--skip-traefik` leaves Traefik out:

```bash
sh host_setup.sh --skip-traefik --deploy            # every service
sh host_setup.sh --skip-traefik --deploy adc nbs    # only these
```

## 8. Verify

On the server:

```bash
docker service ls                          # every service 1/1, PORTS *:8003->8000/tcp ...
docker service ps adc_pycsw --no-trunc     # why a task is not running
docker service logs -f adc_pycsw           # pycsw / gunicorn log
```

From a client machine:

```bash
U=http://10.20.30.40:8003
curl -s "$U/csw?service=CSW&version=2.0.2&request=GetCapabilities" | grep -o 'xlink:href="[^"]*/csw"' | sort -u
curl -s "$U/stac/search?limit=1" | head -c 300; echo
```

The first command must print `xlink:href="http://10.20.30.40:8003/csw"`. If it
shows another address, `docker.host` is wrong: OWSLib would send its requests
there.

With OWSLib:

```python
from owslib.csw import CatalogueServiceWeb
csw = CatalogueServiceWeb("http://10.20.30.40:8003/csw")
csw.getrecords2(maxrecords=3)
print(csw.results)          # {'matches': ..., 'returned': 3, ...}
```

The test suite runs against a service the same way:

```bash
.venv/bin/pip install -r requirements-test.txt
CSW_URL=http://10.20.30.40:8014/csw STAC_URL=http://10.20.30.40:8014/stac \
    .venv/bin/python -m pytest tests/
```

The fixtures must have been built against the Solr core and collections that
service uses (see the README).

A service answers a few seconds after `update_service.sh` returns, once
gunicorn has started.

| Symptom | Cause |
|---|---|
| `connection refused` on the port | Service not running yet or failed: `docker service ps <name>_pycsw --no-trunc` |
| Connection hangs / times out | A firewall between client and server; the host's `ufw` does not block docker ports |
| `port is already in use` / `port already allocated` when deploying | Another service or process owns the port: change `base_port` or set `docker.port` |
| `docker.expose is 'port' but docker.host is not set` | Add `host` to the `_overrides` block |

For other failures (missing mounts, images, resources, variables) see
[Troubleshooting](DEPLOYMENT.md#troubleshooting).

## 9. Operations

| Task | Command |
|---|---|
| Change a service (collections, Solr core, image, ...) | edit `configurator/csw_endpoints.yml`, `.venv/bin/python configurator/collection_mapping.py <name>`, `sh update_service.sh <name>` |
| Pick up edited plugin sources (`pycsw_solr_metno/`, `outputschemas/`) | `sh update_service.sh <name> --restart` |
| Change credentials | edit `.env`, `sh update_service.sh --all` (`--restart` keeps the old values) |
| Change the server address or ports | edit `_overrides`, regenerate, `sh update_service.sh --all` |
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

**Back up:** `.env` and `configurator/csw_endpoints.yml`. Everything under
`artifacts/` can be regenerated.

## 10. Starting over

```bash
# remove the services
for d in artifacts/deploy/*/; do docker stack rm "$(basename "$d")"; done
# regenerate from scratch
rm -rf artifacts/deploy
```

To leave the swarm entirely: `docker swarm leave --force`.

---

## Limitations

- **Plain HTTP.** Requests and responses are not encrypted. That is fine for
  public catalogue metadata on an internal network, but don't expose the
  ports to the internet. The Solr credentials are not affected: they stay on
  the server.
- **No CORS headers.** In the DNS setup Traefik adds
  `Access-Control-Allow-Origin: *`; pycsw itself does not. Server-side clients
  (OWSLib, curl, harvesters, the test suite) are unaffected, but a browser
  application on another origin, such as a STAC browser, cannot call the
  services.
- **No security headers** (HSTS, nosniff, frame deny), for the same reason.
- **Ports follow the entry order** unless pinned with `docker.port`.

## Switching to DNS and HTTPS

Once the DNS records exist (`<name>.<base_domain>` → the server):

1. In `.env`, set `ACME_EMAIL`.
2. In `configurator/csw_endpoints.yml`, replace the `_overrides` block:

   ```yaml
   _overrides:
     docker:
       base_domain: newhost.example.org
       reservations: false
     server:
       workers: 2
   ```

3. Open ports 80 and 443, then deploy Traefik and redeploy the services:

   ```bash
   sh host_setup.sh --deploy
   ```

   This creates the `traefik-public` network if needed, deploys Traefik,
   regenerates every service without published ports and redeploys them.

From there, follow [MACHINE_SETUP.md](MACHINE_SETUP.md) step 9 to verify.
