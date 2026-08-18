"""
Copyright 2026 Guillaume Everarts de Velp

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Contact: edvgui@gmail.com
"""

import collections.abc
import pathlib
import shutil
import socket
import subprocess
import textwrap
import time

import pytest
import pytest_inmanta.plugin
import requests
import yaml

NETBIRD_SERVER_IMAGE = "docker.io/netbirdio/netbird-server:latest"

# The owner the setup api creates on the fresh account.  Its password is never used,
# the tests drive the api with the personal access token minted along with it.
SETUP_OWNER_EMAIL = "admin@example.com"
SETUP_OWNER_PASSWORD = "a-long-enough-test-password-1234"

# Every fresh netbird account comes with this group, holding all of its peers.  This
# module resolves groups by name and never creates one, so the tests need a group
# that is there from the start.
DEFAULT_GROUP = "All"

# How many times to try starting the server before giving up.  A start only fails
# when one of the ports it picked was taken in between, so a new set of ports is
# usually all it takes.
SERVER_START_ATTEMPTS = 3

# Every container these tests start carries a fixed name under this prefix.  A run that
# is interrupted leaves its containers behind — the fixtures tear them down in a finally
# block, which a killed process never reaches — and a fixed name means the next run
# finds that leftover and replaces it instead of piling a new container next to it.  The
# prefix keeps ``run_container`` away from anything on the machine that is not ours.
CONTAINER_PREFIX = "netbird-test"
SERVER_CONTAINER = f"{CONTAINER_PREFIX}-server"


def run_container(name: str, args: collections.abc.Sequence[str]) -> str:
    """
    Start a detached container under a fixed name, replacing whatever holds that name
    already, and return its id.

    Only one container of a given name runs at a time, so **two copies of the suite can
    no longer run next to each other**: the second one takes the first one's containers
    away.  That is the trade for never leaving a server behind, which is worth it — a
    leaked server holds its memory and its sqlite store until the machine is cleaned up
    by hand.

    :param name: The name to give the container, which is also the name of any leftover
        to replace.
    :param args: The arguments to ``podman run``, image included.
    """
    subprocess.run(["podman", "rm", "-f", name], capture_output=True)

    started = subprocess.run(
        ["podman", "run", "-d", "--name", name, *args],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        # Say what podman said: a bare returncode is nothing to go on when this fails
        # somewhere other than the machine the test was written on.
        raise RuntimeError(
            f"podman run {name} failed ({started.returncode}): {started.stderr.strip()}"
        )

    return started.stdout.strip()


def free_port() -> int:
    """
    Ask the kernel for a free port.  The port the api is reached on is forwarded from
    the host, and its default may well be taken on the machine running the tests.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def pasta_network(forwarded_ports: collections.abc.Mapping[int, int]) -> str:
    """
    The ``--network`` argument giving a container a network namespace of its own, with
    the given ports forwarded into it from the host.

    Every container these tests start needs a namespace of its own, so that several
    copies of the suite can run next to each other on one machine: the netbird server
    binds a hardcoded ``:33073`` for its management grpc whatever the configuration
    says, and two netbird clients would both want the same wireguard interface.

    Forwarding is done by pasta rather than by ``--publish``: podman's own port
    forwarder does not work when rootless podman itself runs inside a container, which
    is exactly the CI setup, while pasta binds the host port from the parent namespace
    and works in both.

    :param forwarded_ports: The ports to forward, host port to container port.  A
        container needing no port forwarded still needs its own namespace.
    """
    if not forwarded_ports:
        return "pasta"

    ports = ",".join(
        f"{host}:{container}" for host, container in forwarded_ports.items()
    )
    return f"pasta:--tcp-ports,{ports}"


def server_config(*, api_port: int, path: pathlib.Path) -> pathlib.Path:
    """
    Write the configuration of a single-node netbird server: management, signal,
    relay and stun, all served by one process.

    The address the server advertises to its peers is the one they reach the host on
    from their own network namespace, so that a netbird client container could
    register and pick up the signal and relay urls that go with it.

    Every other port the server binds (management grpc, stun, metrics, healthcheck)
    is left at its default: the container has a network namespace of its own, so those
    are private to it and can not collide with another server's.

    :param api_port: The port the management api listens on, inside the container and
        on the host alike — the same number on both ends keeps ``exposedAddress``
        valid for peers.
    :param path: The directory to write the configuration file in.
    """
    peer_url = f"http://host.containers.internal:{api_port}"
    config = path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "listenAddress": f":{api_port}",
                    "exposedAddress": peer_url,
                    "logLevel": "info",
                    "logFile": "console",
                    "authSecret": "test-auth-secret",
                    "dataDir": "/var/lib/netbird",
                    "auth": {
                        "issuer": f"{peer_url}/oauth2",
                        "dashboardRedirectURIs": [f"{peer_url}/nb-auth"],
                        "cliRedirectURIs": ["http://localhost:53000/"],
                    },
                    "store": {"engine": "sqlite"},
                },
            },
        )
    )
    return config


def wait_until(
    condition: collections.abc.Callable[[], bool],
    container_id: str,
    what: str,
    timeout: float = 120.0,
) -> None:
    """
    Poll a condition until it holds, and raise with the container logs when it never
    does, so a failure in CI says why rather than just timing out.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)

    logs = subprocess.run(
        ["podman", "logs", container_id], capture_output=True, text=True
    )
    raise TimeoutError(f"{what} within {timeout}s:\n{logs.stdout}\n{logs.stderr}")


def start_server(path: pathlib.Path) -> tuple[str, int]:
    """
    Start a netbird server and wait until its api answers, and return the id of the
    container running it together with the port the api listens on.

    The port the api is forwarded on is picked by asking the kernel for a free one and
    letting it go again, so another process on the machine can take it in between.
    That shows up as a container that exits on startup, which is worth another go on a
    new port rather than a failed test.

    :param path: The directory to write the configuration and the data of the server
        in.  Each attempt gets a subdirectory of its own.
    """
    for attempt in range(SERVER_START_ATTEMPTS):
        root = path / f"attempt{attempt}"
        (root / "data").mkdir(parents=True)
        api_port = free_port()
        config = server_config(api_port=api_port, path=root)

        # No --rm: a server that dies on startup would take its logs with it, and those
        # logs are all wait_until has to report.
        container_id = run_container(
            SERVER_CONTAINER,
            [
                "--network",
                pasta_network({api_port: api_port}),
                # Lets the setup api create the first owner and hand us back a token,
                # instead of requiring a browser login against the embedded idp.
                "-e",
                "NB_SETUP_PAT_ENABLED=true",
                "-v",
                f"{config}:/etc/netbird/config.yaml:ro,Z",
                "-v",
                f"{root / 'data'}:/var/lib/netbird:Z",
                NETBIRD_SERVER_IMAGE,
                "--config",
                "/etc/netbird/config.yaml",
            ],
        )

        try:
            # The api answers 401 without a token, which is enough to know it is up.
            wait_until(
                lambda: requests.get(
                    f"http://127.0.0.1:{api_port}/api/peers", timeout=2
                ).status_code
                == 401,
                container_id,
                "the netbird server did not become ready",
            )
        except TimeoutError:
            subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)
            if attempt + 1 == SERVER_START_ATTEMPTS:
                raise
            continue

        return container_id, api_port

    raise AssertionError("unreachable")


@pytest.fixture()
def netbird(tmp_path: pathlib.Path) -> collections.abc.Iterator[requests.Session]:
    """
    Start a fresh self-hosted netbird server in a podman container for each test, so
    that every test sees an empty account: the container is torn down and started
    again in between tests, which is all the cleanup the tests need.  Complete the
    initial setup of the account, and yield a session authenticated with the personal
    access token that setup minted.

    The session carries the urls the tests and the models need:
      - ``base_url``: the api, as reached from the host running the tests,
      - ``management_url``: the same server, without the ``/api`` suffix, which is
        what the ``netbird::Api`` entity takes.

    The server gets a network namespace of its own with only its api port forwarded to
    the host, never ``--network host``: it binds a hardcoded ``:33073`` no configuration
    key moves, so two servers sharing a namespace fight over it and the second one
    exits.  See ``pasta_network``.
    """
    if shutil.which("podman") is None:
        pytest.skip("podman is required to run the netbird server")

    container_id, api_port = start_server(tmp_path)

    url = f"http://127.0.0.1:{api_port}"
    try:
        setup = requests.post(
            f"{url}/api/setup",
            json={
                "email": SETUP_OWNER_EMAIL,
                "name": "Admin",
                "password": SETUP_OWNER_PASSWORD,
                "create_pat": True,
                "pat_expire_in": 1,
            },
            timeout=30,
        )
        setup.raise_for_status()
        token = setup.json()["personal_access_token"]

        session = requests.Session()
        session.headers["Authorization"] = f"Token {token}"
        session.base_url = f"{url}/api"
        session.management_url = url
        session.token = token
        yield session
    finally:
        subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)


@pytest.fixture()
def compile_model(
    project: pytest_inmanta.plugin.Project,
    netbird: requests.Session,
) -> collections.abc.Callable[[str], None]:
    """
    Return a helper compiling a model against the netbird server the tests run, with
    the api entity every netbird resource hangs off already declared as ``api``.
    """

    def compile(model: str) -> None:
        project.compile(
            "import netbird\n"
            "api = netbird::Api(\n"
            '    agent_name="netbird",\n'
            f"    management_url={netbird.management_url!r},\n"
            f"    api_token={netbird.token!r},\n"
            ")\n" + textwrap.dedent(model)
        )

    return compile


def get(netbird: requests.Session, path: str) -> list[dict] | dict:
    """
    Do a GET on the netbird api and return the parsed body.
    """
    response = netbird.get(f"{netbird.base_url}/{path}")
    response.raise_for_status()
    return response.json()


def facts(project: pytest_inmanta.plugin.Project) -> dict[str, str]:
    """
    The facts the last deploy published, by name.
    """
    return {fact["id"]: fact["value"] for fact in project.ctx.facts}
