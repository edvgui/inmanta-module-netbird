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
import json
import subprocess

import inmanta_plugins.std
import pytest
import pytest_inmanta.plugin
import requests
from conftest import facts, get, pasta_network, wait_until

import inmanta.plugins
from inmanta import const

NETBIRD_CLIENT_IMAGE = "docker.io/netbirdio/netbird:latest"

# How long to give the netbird client to start and register itself.
PEER_REGISTRATION_TIMEOUT = 180.0

# The host the netbird client reaches the server on from its own network namespace.
# It is the address the server advertises to its peers, see ``server_config``.
PEER_HOST = "host.containers.internal"

Compile = collections.abc.Callable[[str], None]


@pytest.fixture()
def peer(netbird: requests.Session) -> collections.abc.Iterator[dict]:
    """
    Register a peer on the account by starting a netbird client next to the server,
    and yield the peer as the api reports it.

    A peer can not be created through the api, so this is the only way to get one:
    the client joins with a setup key, and the server advertises to it the url it
    reaches the host on from its own network namespace.
    """
    key = netbird.post(
        f"{netbird.base_url}/setup-keys",
        json={
            "name": "peer-test",
            "type": "reusable",
            "expires_in": 86400,
            "usage_limit": 0,
            "auto_groups": [],
            "ephemeral": False,
        },
    )
    key.raise_for_status()

    container_id = subprocess.run(
        [
            "podman",
            "run",
            "-d",
            # A network namespace of its own, so that two clients started by two
            # copies of the suite do not fight over the same wireguard interface, plus
            # what it takes to set that interface up.
            "--network",
            pasta_network({}),
            "--cap-add",
            "NET_ADMIN",
            "--device",
            "/dev/net/tun",
            "-e",
            f"NB_SETUP_KEY={key.json()['key']}",
            "-e",
            "NB_MANAGEMENT_URL="
            + netbird.management_url.replace("127.0.0.1", PEER_HOST),
            NETBIRD_CLIENT_IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    try:
        wait_until(
            lambda: bool(get(netbird, "peers")),
            container_id,
            "the netbird client did not register itself",
            timeout=PEER_REGISTRATION_TIMEOUT,
        )
        yield get(netbird, "peers")[0]
    finally:
        subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)


def find_peer(netbird: requests.Session, hostname: str) -> dict | None:
    return next((p for p in get(netbird, "peers") if p["hostname"] == hostname), None)


def peer_model(hostname: str, **attributes: object) -> str:
    """
    Build a peer resource, with only the attributes the test has an opinion about:
    everything else is left null, and is therefore not managed.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    return "\n".join(
        [
            "peer = netbird::Peer(",
            "    api=api,",
            f"    hostname={json.dumps(hostname)},",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_peer_adopted_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
    peer: dict,
) -> None:
    """
    A peer that joined the account is adopted, the settings the api lets us change are
    kept in line with the model, and the peer is removed from the account when the
    resource is purged.
    """
    hostname = peer["hostname"]

    compile_model(peer_model(hostname, name="managed", ssh_enabled=True))
    project.deploy_resource("netbird::Peer")

    updated = find_peer(netbird, hostname)
    assert updated is not None
    # The id the api gave the peer is published.
    assert facts(project) == {"id": peer["id"]}
    assert updated["name"] == "managed"
    assert updated["ssh_enabled"] is True

    # A second deploy of the same desired state changes nothing.
    compile_model(peer_model(hostname, name="managed", ssh_enabled=True))
    project.deploy_resource("netbird::Peer", change=const.Change.nochange)

    compile_model(peer_model(hostname, name="renamed"))
    project.deploy_resource("netbird::Peer")
    renamed = find_peer(netbird, hostname)
    assert renamed["name"] == "renamed"
    # The ssh flag isn't managed anymore, and the api resets every key it takes but
    # doesn't receive: the value the account holds is carried along instead.
    assert renamed["ssh_enabled"] is True
    # The hostname is not settable, it is what identifies the peer.
    assert renamed["hostname"] == hostname

    compile_model(peer_model(hostname, purged=True))
    project.deploy_resource("netbird::Peer")
    assert find_peer(netbird, hostname) is None


def test_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
    peer: dict,
) -> None:
    """
    Every attribute the model leaves null keeps whatever value the account holds for
    it, even when it was changed in the dashboard behind our back.
    """
    hostname = peer["hostname"]

    compile_model(peer_model(hostname, ssh_enabled=True))
    project.deploy_resource("netbird::Peer")

    netbird.put(
        f"{netbird.base_url}/peers/{peer['id']}",
        json={"name": "dashboard", "ssh_enabled": True},
    ).raise_for_status()

    # The name isn't managed: the deploy leaves it as it is.
    compile_model(peer_model(hostname, ssh_enabled=True))
    project.deploy_resource("netbird::Peer", change=const.Change.nochange)
    assert find_peer(netbird, hostname)["name"] == "dashboard"

    # And a value the model does set is enforced, dashboard or not.
    compile_model(peer_model(hostname, ssh_enabled=False))
    project.deploy_resource("netbird::Peer")
    assert find_peer(netbird, hostname)["ssh_enabled"] is False
    assert find_peer(netbird, hostname)["name"] == "dashboard"


def test_peer_that_never_joined_is_skipped(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    A peer only comes into existence by registering itself, the api has no endpoint to
    create one.  Deploying a peer that never joined therefore skips, rather than
    quietly reporting a deploy that made nothing true.
    """
    compile_model(peer_model("never-joined", name="managed"))
    project.deploy_resource(
        "netbird::Peer",
        status=const.ResourceState.skipped,
    )


def test_id_is_a_fact_reference(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The api addresses every object by an opaque id the model never knows.  It is
    exposed on the resource all the same, as a reference that reads it back from the
    facts the handler publishes, so another resource can point at this peer without
    going digging through the api.
    """
    compile_model(peer_model("some-host"))

    (peer,) = project.get_instances("netbird::Peer")
    object_id = inmanta.plugins.allow_reference_values(peer).id
    assert isinstance(object_id, inmanta_plugins.std.FactReference)
    assert object_id.fact_name == "id"
