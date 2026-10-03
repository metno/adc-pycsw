"""
Generate the pycsw configuration and the docker stack file for each service
defined in csw_endpoints.yml.

Every service gets its own directory under the output directory
(default: <repo>/artifacts/deploy, which is gitignored):

    artifacts/deploy/<service_name>/
        pycsw.yml   pycsw configuration, mounted at /etc/pycsw/pycsw.yml
        stack.yml   docker stack file, deployed by update_service.sh
        pycsw.log   log file bind-mounted into the container

Nothing sensitive is written to these files. Solr credentials are referenced
as ${SOLR_USER} / ${SOLR_PASS}: pycsw expands them from the container
environment, and the stack file forwards them from the deploying shell, where
update_service.sh loads them from <repo>/.env.

Usage:
        python configurator/collection_mapping.py
        # Only some services:
        python configurator/collection_mapping.py test adc
        # Other endpoint definitions / output directory:
        python configurator/collection_mapping.py --endpoints /path/to/endpoints.yml
        python configurator/collection_mapping.py --output-dir /path/to/output
"""

import argparse
import copy
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

CONFIGURATOR_DIR = Path(__file__).resolve().parent
REPO_DIR = CONFIGURATOR_DIR.parent
DEFAULT_ENDPOINTS = CONFIGURATOR_DIR / 'csw_endpoints.yml'
DEFAULT_OUTPUT_DIR = REPO_DIR / 'artifacts' / 'deploy'

# Optional top-level keys in the endpoints file: `_defaults` holds values every
# service inherits, `_overrides` holds values forced onto every service (e.g.
# docker.base_domain when the same definitions are deployed on another host).
DEFAULTS_KEY = '_defaults'
OVERRIDES_KEY = '_overrides'

CFG_FILENAME = 'pycsw.yml'
STACK_FILENAME = 'stack.yml'
LOG_FILENAME = 'pycsw.log'

ENV_REFERENCE = re.compile(r'^\$\{[A-Za-z_][A-Za-z0-9_]*\}$')


def deep_merge(base: dict, override: dict) -> dict:
    """
    Return a copy of base with override merged in (nested dicts are merged).
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if value is None and isinstance(merged.get(key), dict):
            # an empty section (`docker:` with only comments) overrides nothing
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_endpoints(endpoints_file: Path) -> dict:
    """
    Load the service definitions, applying the optional `_defaults` and
    `_overrides` blocks and filling in the derived domain / port / server URL.

    Returns:
        dict: service name -> service definition.
    """
    with open(endpoints_file, 'r') as f:
        collections_mapping = yaml.safe_load(f) or {}

    defaults = collections_mapping.pop(DEFAULTS_KEY, None) or {}
    overrides = collections_mapping.pop(OVERRIDES_KEY, None) or {}
    services = {}
    for index, (endpoint, service_data) in enumerate(collections_mapping.items()):
        service_data = deep_merge(deep_merge(defaults, service_data or {}), overrides)
        name = service_data.get('name')
        if not name:
            raise ValueError(f"{endpoints_file}: '{endpoint}' has no 'name'")
        if name in services:
            raise ValueError(f"{endpoints_file}: duplicate service name '{name}'")
        set_domain(endpoint, service_data, index)
        services[name] = service_data

    published = {}
    for name, service_data in services.items():
        port = service_data['docker'].get('port')
        if service_data['docker']['expose'] == 'port':
            if port in published:
                raise ValueError(
                    f"{endpoints_file}: services '{published[port]}' and '{name}' both publish port {port}"
                )
            published[port] = name
    return services


def set_domain(endpoint: str, service_data: dict, index: int):
    """
    Fill in docker.expose, docker.domain / docker.port and server.server_url.

    docker.expose selects how the service is reached:

    traefik (default): behind Traefik at https://<domain>. docker.base_domain,
        when set, relocates the service to <name>.<base_domain> and takes
        precedence over an explicit domain / server_url. Otherwise the domain
        defaults to the endpoint key.
    port: published directly on the swarm node at http://<host>:<port>, no
        Traefik, DNS or TLS. docker.host is the address clients use;
        docker.port defaults to docker.base_port plus the position of the
        entry in the endpoints file, so a service keeps its port when only a
        subset is generated.
    """
    for section in ('docker', 'server'):
        if service_data.get(section) is None:
            service_data[section] = {}
    docker, server = service_data['docker'], service_data['server']
    name = service_data['name']
    docker.setdefault('expose', 'traefik')
    if docker['expose'] == 'port':
        host = docker.get('host')
        if not host:
            raise ValueError(f"service '{name}': docker.expose is 'port' but docker.host is not set")
        if 'port' not in docker:
            if 'base_port' not in docker:
                raise ValueError(f"service '{name}': docker.expose is 'port' but neither docker.port nor docker.base_port is set")
            docker['port'] = int(docker['base_port']) + index
        server['server_url'] = f"http://{host}:{docker['port']}"
        return
    if docker['expose'] != 'traefik':
        raise ValueError(f"service '{name}': docker.expose must be 'traefik' or 'port', not '{docker['expose']}'")
    base_domain = docker.get('base_domain')
    if base_domain:
        docker['domain'] = f"{service_data['name']}.{base_domain}"
        server['server_url'] = f"https://{docker['domain']}"
    else:
        docker.setdefault('domain', endpoint)
        server.setdefault('server_url', f"https://{docker['domain']}")


def check_no_literal_credentials(service_data: dict):
    """
    Refuse Solr credentials written in clear text in the endpoints file.
    """
    repository = service_data.get('repository', {})
    for key in ('username', 'password'):
        value = repository.get(key)
        if value is not None and not ENV_REFERENCE.match(str(value)):
            raise ValueError(
                f"service '{service_data['name']}': repository.{key} is set in clear text. "
                "Put the credentials in .env (SOLR_USER / SOLR_PASS) and remove "
                f"repository.{key}, or name other variables with "
                "repository.username_env / repository.password_env."
            )


def generate_service_files(output_dir: Path, endpoints_file: Path = DEFAULT_ENDPOINTS, only=None):
    """
    Generate the config and docker stack files for each service.

    Args:
        output_dir (Path): Directory that receives one sub-directory per service.
        endpoints_file (Path): YAML file with the service definitions.
        only (list): Service names to generate (default: all of them).

    Returns:
        list: Directories that were written.
    """
    env = Environment(
        loader=FileSystemLoader(CONFIGURATOR_DIR / 'templates'),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    cfg_template = env.get_template("pycsw_cfg_yaml.j2")
    dkr_template = env.get_template("docker_compose_yaml.j2")

    services = load_endpoints(endpoints_file)
    if only:
        unknown = sorted(set(only) - set(services))
        if unknown:
            raise ValueError(
                f"unknown service(s): {', '.join(unknown)} "
                f"(defined: {', '.join(sorted(services))})"
            )
        services = {name: services[name] for name in only}

    written = []
    for name, service_data in services.items():
        check_no_literal_credentials(service_data)

        rendered = {
            CFG_FILENAME: cfg_template.render(data=service_data),
            STACK_FILENAME: dkr_template.render(data=service_data),
        }
        # Catch template/endpoint mistakes here rather than at deploy time.
        for filename, content in rendered.items():
            try:
                yaml.safe_load(content)
            except yaml.YAMLError as err:
                raise ValueError(f"service '{name}': generated {filename} is not valid YAML: {err}")

        service_dir = output_dir / name
        service_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in rendered.items():
            (service_dir / filename).write_text(content)
        # Swarm rejects a bind mount whose source does not exist.
        (service_dir / LOG_FILENAME).touch()
        written.append(service_dir)
    return written


def main():
    """
    Parse command-line arguments and generate service files.
    """
    parser = argparse.ArgumentParser(
        description="Generate service config and docker stack YAML files from templates."
    )
    parser.add_argument(
        'services',
        nargs='*',
        help='Names of the services to generate (default: all services in the endpoints file)'
    )
    parser.add_argument(
        '--endpoints',
        type=str,
        default=str(DEFAULT_ENDPOINTS),
        help='YAML file with the service definitions (default: configurator/csw_endpoints.yml)'
    )
    parser.add_argument(
        '--output-dir',
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help='Directory that receives one sub-directory per service (default: <repo>/artifacts/deploy)'
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    print(f"Writing output files to: {output_dir}")
    try:
        written = generate_service_files(output_dir, Path(args.endpoints), args.services)
    except (OSError, ValueError) as err:
        sys.exit(f"ERROR: {err}")
    services = load_endpoints(Path(args.endpoints))
    for service_dir in written:
        print(f"  {service_dir}  ->  {services[service_dir.name]['server']['server_url']}")

if __name__ == "__main__":
    main()
