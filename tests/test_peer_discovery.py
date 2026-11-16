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

import pytest_inmanta.plugin
import requests
from conftest import facts
from test_peer import peer  # noqa: F401 (the fixture registering a peer)

from inmanta import const

Compile = collections.abc.Callable[[str], None]

MODEL = "discovery = netbird::PeerDiscovery(api=api)"


def test_discovered_peers_join_their_user(
    project: pytest_inmanta.plugin.Project,
) -> None:
    """
    Each peer comes with the email of the user owning it: none for a peer registered
    with a setup key, nor for a service user, which has no email.  The peers are
    sorted by address, numerically.
    """
    # The module under test is loaded by the project fixture
    from inmanta_plugins.netbird import discovered_peers

    users = [
        {"id": "u-alice", "email": "alice@example.com"},
        {"id": "u-robot", "email": ""},
    ]
    peers = [
        {
            "id": "p3",
            "name": "nas",
            "hostname": "nas",
            "ip": "100.64.0.10",
            "user_id": "",
        },
        {
            "id": "p1",
            "name": "laptop",
            "hostname": "laptop",
            "ip": "100.64.0.9",
            "ipv6": "fd00::9",
            "user_id": "u-alice",
        },
        {
            "id": "p2",
            "name": "ci",
            "hostname": "ci",
            "ip": "100.64.0.2",
            "user_id": "u-robot",
        },
    ]

    assert discovered_peers(peers, users) == [
        {
            "id": "p2",
            "name": "ci",
            "hostname": "ci",
            "ip": "100.64.0.2",
            "ipv6": None,
            "user_id": "u-robot",
            "user_email": None,
        },
        {
            "id": "p1",
            "name": "laptop",
            "hostname": "laptop",
            "ip": "100.64.0.9",
            "ipv6": "fd00::9",
            "user_id": "u-alice",
            "user_email": "alice@example.com",
        },
        {
            "id": "p3",
            "name": "nas",
            "hostname": "nas",
            "ip": "100.64.0.10",
            "ipv6": None,
            "user_id": None,
            "user_email": None,
        },
    ]


def test_peer_discovery_of_an_empty_account(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    An account without peers publishes an empty list, the discovery never changes
    anything, and purging it does nothing either.
    """
    compile_model(MODEL)
    project.deploy_resource("netbird::PeerDiscovery", change=const.Change.nochange)
    assert json.loads(facts(project)["peers"]) == []

    compile_model("discovery = netbird::PeerDiscovery(api=api, purged=true)")
    project.deploy_resource("netbird::PeerDiscovery", change=const.Change.nochange)


def test_peer_discovery_publishes_the_peers(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
    peer: dict,  # noqa: F811 (the fixture imported above)
) -> None:
    """
    A peer that joined the account is published with its address and its name.  It
    registered with a setup key, so it has no user.
    """
    compile_model(MODEL)
    project.deploy_resource("netbird::PeerDiscovery", change=const.Change.nochange)

    assert json.loads(facts(project)["peers"]) == [
        {
            "id": peer["id"],
            "name": peer["name"],
            "hostname": peer["hostname"],
            "ip": peer["ip"],
            "ipv6": peer.get("ipv6") or None,
            "user_id": None,
            "user_email": None,
        },
    ]
