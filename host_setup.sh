#!/bin/sh
# Prepare a host for the pycsw stacks: docker, swarm, Traefik, MMD checkout,
# parent list, configurator environment, generated configs.
#
#   sh host_setup.sh [options] [service ...]
#
#   --deploy               also deploy the generated services at the end
#   --skip-docker-install  fail instead of installing docker when it is missing
#   --skip-traefik         do not deploy the Traefik stack (one is already
#                          provided some other way)
#   service ...            only generate/deploy these services (default: all
#                          entries of configurator/csw_endpoints.yml)
#
# Safe to re-run: every step is skipped when already done. Settings come from
# .env (see .env.example). Full walkthrough: docs/DEPLOYMENT.md
set -eu

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
ENV_FILE=${ENV_FILE:-$REPO_DIR/.env}
ENDPOINTS=$REPO_DIR/configurator/csw_endpoints.yml
PARENT_LIST=/usr/local/share/csw/parent_list.xml
MMD_REPO=${MMD_REPO:-https://github.com/metno/mmd.git}
MMD_BRANCH=${MMD_BRANCH:-master}

DEPLOY=0
INSTALL_DOCKER=1
TRAEFIK=1
SERVICES=""
for arg in "$@"; do
    case "$arg" in
        --deploy) DEPLOY=1 ;;
        --skip-docker-install) INSTALL_DOCKER=0 ;;
        --skip-traefik) TRAEFIK=0 ;;
        -h|--help) sed -n '2,15p' "$0" | cut -c3-; exit 0 ;;
        -*) echo "ERROR: unknown option $arg" >&2; exit 1 ;;
        *) SERVICES="$SERVICES $arg" ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
# Things the user still has to do; printed at the end.
TODO=""
todo() { TODO="$TODO
  - $*"; echo "    TODO: $*"; }
# Optional improvements; they do not block the deployment.
NOTES=""
note() { NOTES="$NOTES
  - $*"; echo "    NOTE: $*"; }

if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo"; fi

# --- .env --------------------------------------------------------------------
step "Settings (.env)"
if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_DIR/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "    created $ENV_FILE from .env.example"
fi
set -a
. "$ENV_FILE"
set +a
[ -n "${SOLR_USER:-}" ] && [ -n "${SOLR_PASS:-}" ] \
    || todo "set SOLR_USER / SOLR_PASS in $ENV_FILE (needed to deploy the services)"

# --- base packages -----------------------------------------------------------
step "Base packages (git, curl, python3 venv)"
missing=""
for tool in git curl python3; do
    command -v "$tool" > /dev/null 2>&1 || missing="$missing $tool"
done
python3 -c 'import venv, ensurepip' > /dev/null 2>&1 || missing="$missing python3-venv"
if [ -n "$missing" ]; then
    if command -v apt-get > /dev/null 2>&1; then
        $SUDO apt-get update
        $SUDO apt-get install -y git curl python3 python3-venv
    else
        echo "ERROR: install these with your package manager and re-run:$missing" >&2
        exit 1
    fi
else
    echo "    present"
fi

# --- docker ------------------------------------------------------------------
step "Docker"
if ! command -v docker > /dev/null 2>&1; then
    if [ "$INSTALL_DOCKER" = 0 ]; then
        echo "ERROR: docker is not installed" >&2
        exit 1
    fi
    curl -fsSL https://get.docker.com | $SUDO sh
    $SUDO systemctl enable --now docker 2> /dev/null || true
fi
if ! docker info > /dev/null 2>&1; then
    if [ -n "$SUDO" ] && $SUDO docker info > /dev/null 2>&1; then
        $SUDO usermod -aG docker "$(id -un)"
        echo "ERROR: $(id -un) was just added to the 'docker' group." >&2
        echo "Log out and back in (or run 'newgrp docker'), then re-run this script." >&2
    else
        echo "ERROR: the docker daemon is not running" >&2
    fi
    exit 1
fi
echo "    $(docker --version)"

# --- swarm -------------------------------------------------------------------
step "Docker swarm"
if [ "$(docker info -f '{{.Swarm.LocalNodeState}}')" != "active" ]; then
    if [ -n "${SWARM_ADVERTISE_ADDR:-}" ]; then
        docker swarm init --advertise-addr "$SWARM_ADVERTISE_ADDR"
    else
        docker swarm init || {
            echo "ERROR: set SWARM_ADVERTISE_ADDR in $ENV_FILE to this host's address and re-run" >&2
            exit 1
        }
    fi
else
    echo "    already active"
fi

step "Overlay network traefik-public"
if docker network inspect traefik-public > /dev/null 2>&1; then
    echo "    exists"
else
    docker network create --driver overlay --attachable traefik-public
fi

# --- traefik -----------------------------------------------------------------
step "Traefik"
if [ "$TRAEFIK" = 0 ]; then
    echo "    skipped (--skip-traefik)"
elif docker stack ls --format '{{.Name}}' | grep -qx traefik; then
    echo "    stack 'traefik' already deployed (remove it with 'docker stack rm traefik' to redeploy)"
elif [ -z "${ACME_EMAIL:-}" ]; then
    todo "set ACME_EMAIL in $ENV_FILE and re-run to deploy Traefik"
else
    docker stack deploy -c "$REPO_DIR/host/traefik.yml" traefik
fi

# --- MMD checkout (XSLT + thesauri mounted into the containers) --------------
step "MMD checkout ($REPO_DIR/mmd)"
if [ -d "$REPO_DIR/mmd/xslt" ]; then
    echo "    present"
else
    git clone --depth 1 --branch "$MMD_BRANCH" "$MMD_REPO" "$REPO_DIR/mmd"
fi

# --- parent list -------------------------------------------------------------
step "Parent list ($PARENT_LIST)"
if [ ! -w "$(dirname "$PARENT_LIST")" ]; then
    $SUDO mkdir -p "$(dirname "$PARENT_LIST")"
    $SUDO chown "$(id -u):$(id -g)" "$(dirname "$PARENT_LIST")"
fi
if [ -n "${PARENT_LIST_SOLR_URL:-}" ] && [ -n "${SOLR_USER:-}" ]; then
    python3 "$REPO_DIR/fetch_collections.py" --output "$PARENT_LIST" \
        || note "parent list could not be fetched; check PARENT_LIST_SOLR_URL / credentials"
elif [ ! -s "$PARENT_LIST" ]; then
    note "set PARENT_LIST_SOLR_URL in $ENV_FILE and re-run to fill $PARENT_LIST (parent/child links are omitted from ISO/DIF output until then)"
else
    echo "    present (set PARENT_LIST_SOLR_URL in $ENV_FILE to refresh it)"
fi
if [ ! -s "$PARENT_LIST" ]; then
    # Swarm rejects a bind mount whose source does not exist, so the services
    # need at least an empty list.
    printf '<?xml version="1.0" ?>\n<parent/>\n' > "$PARENT_LIST"
    echo "    wrote an empty placeholder"
fi

# --- configurator ------------------------------------------------------------
step "Python environment for the configurator ($REPO_DIR/.venv)"
if [ ! -x "$REPO_DIR/.venv/bin/python" ]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/python" -c 'import jinja2, yaml' 2> /dev/null \
    || "$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/configurator/requirements.txt"
echo "    ready"

step "Generate configs"
GENERATED=0
if [ ! -f "$ENDPOINTS" ]; then
    todo "create $ENDPOINTS (copy it from another host, or start from csw_endpoints_sample.yml) and re-run"
else
    # shellcheck disable=SC2086
    "$REPO_DIR/.venv/bin/python" "$REPO_DIR/configurator/collection_mapping.py" $SERVICES
    GENERATED=1
fi

# --- deploy ------------------------------------------------------------------
if [ "$DEPLOY" = 1 ]; then
    step "Deploy"
    if [ "$GENERATED" = 0 ] || [ -n "$TODO" ]; then
        echo "    skipped: finish the TODO items first"
    elif [ -n "$SERVICES" ]; then
        for service in $SERVICES; do
            sh "$REPO_DIR/update_service.sh" "$service"
        done
    else
        sh "$REPO_DIR/update_service.sh" --all
    fi
fi

step "Done"
[ -z "$NOTES" ] || echo "Optional:$NOTES"
if [ -n "$TODO" ]; then
    echo "Still to do, then re-run this script:$TODO"
    exit 2
fi
if [ "$DEPLOY" = 0 ]; then
    echo "Host is ready. Make sure the service domains resolve to this host, then:"
    echo "    sh update_service.sh <service>     # or: sh update_service.sh --all"
fi
