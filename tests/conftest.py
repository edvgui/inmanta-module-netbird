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


def free_port() -> int:
    """
    Ask the kernel for a free port.  The netbird server binds several of them, and
    they all default to fixed values that may well be taken on the machine running
    the tests.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def server_config(*, api_port: int, path: pathlib.Path) -> pathlib.Path:
    """
    Write the configuration of a single-node netbird server: management, signal,
    relay and stun, all served by one process.

    The address the server advertises to its peers is the one they reach the host on
    from their own network namespace, so that a netbird client container could
    register and pick up the signal and relay urls that go with it.

    :param api_port: The port the management api listens on.
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
                    "stunPorts": [free_port()],
                    "metricsPort": free_port(),
                    "healthcheckAddress": f":{free_port()}",
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

    Use host networking (and let the server bind the chosen ports directly) rather
    than published ports: rootless podman can not publish a port to the host loopback
    when it itself runs inside a container (as it does in CI).
    """
    if shutil.which("podman") is None:
        pytest.skip("podman is required to run the netbird server")

    api_port = free_port()
    config = server_config(api_port=api_port, path=tmp_path)
    data = tmp_path / "data"
    data.mkdir()

    container_id = subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--rm",
            "--network",
            "host",
            # Lets the setup api below create the first owner and hand us back a
            # token, instead of requiring a browser login against the embedded idp.
            "-e",
            "NB_SETUP_PAT_ENABLED=true",
            "-v",
            f"{config}:/etc/netbird/config.yaml:ro,Z",
            "-v",
            f"{data}:/var/lib/netbird:Z",
            NETBIRD_SERVER_IMAGE,
            "--config",
            "/etc/netbird/config.yaml",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    url = f"http://127.0.0.1:{api_port}"
    try:
        # The api answers 401 without a token, which is enough to know it is up.
        wait_until(
            lambda: requests.get(f"{url}/api/peers", timeout=2).status_code == 401,
            container_id,
            "the netbird server did not become ready",
        )

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


@pytest.fixture()
def get(
    netbird: requests.Session,
) -> collections.abc.Callable[[str], list[dict] | dict]:
    """
    Return a helper doing a GET on the netbird api and returning the parsed body.
    """

    def _get(path: str) -> list[dict] | dict:
        response = netbird.get(f"{netbird.base_url}/{path}")
        response.raise_for_status()
        return response.json()

    return _get


@pytest.fixture()
def facts(
    project: pytest_inmanta.plugin.Project,
) -> collections.abc.Callable[[], dict[str, str]]:
    """
    Return a helper listing the facts the last deploy published, by name.
    """

    def _facts() -> dict[str, str]:
        return {fact["id"]: fact["value"] for fact in project.ctx.facts}

    return _facts
