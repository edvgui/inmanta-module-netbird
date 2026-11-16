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
from typing import cast

import pytest_inmanta.plugin
import requests
from test_peer import peer  # noqa: F401 (the fixture registering a peer)

from inmanta.agent.handler import HandlerContext
from inmanta.data.model import DiscoveredResourceInput, DiscoveredResourceOutput
from inmanta.protocol import Client
from inmanta.protocol.common import Result
from inmanta.resources import Id
from inmanta.types import ResourceIdStr

Compile = collections.abc.Callable[[str], None]

MODEL = "discovery = netbird::PeerDiscovery(api=api)"
DISCOVERY = ResourceIdStr("netbird::PeerDiscovery[netbird,agent_name=netbird]")


def reported(
    result: pytest_inmanta.plugin.DeployResultV2,
) -> dict[str, dict[str, object]]:
    """
    The values of the peers a deployed discovery reported, by id.
    """
    return {
        discovered.discovered_resource_id: discovered.values
        for discovered in cast(
            list[DiscoveredResourceInput], result.discovered_resources
        )
    }


def test_discovered_peers_join_their_user(
    project: pytest_inmanta.plugin.Project,
) -> None:
    """
    Each peer comes with the email of the user owning it: none for a peer registered
    with a setup key, nor for a service user, which has no email.  A peer is reported
    under the id of the resource that would manage it, unless another peer has the
    same hostname: neither is identified by it then.
    """
    # The module under test is loaded by the project fixture
    from inmanta_plugins.netbird import discovered_peers

    users = [
        {"id": "u-alice", "email": "alice@example.com"},
        {"id": "u-robot", "email": ""},
    ]
    peers = [
        {
            "id": "p1",
            "name": "laptop",
            "hostname": "laptop",
            "ip": "100.64.0.9",
            "ipv6": "fd00::9",
            "user_id": "u-alice",
        },
        {"id": "p2", "name": "ci", "hostname": "ci", "ip": "100.64.0.2", "user_id": ""},
        {"id": "p3", "name": "a", "hostname": "pi", "ip": "100.64.0.3", "user_id": ""},
        {
            "id": "p4",
            "name": "b",
            "hostname": "pi",
            "ip": "100.64.0.4",
            "user_id": "u-robot",
        },
    ]

    discovered = {
        discovered_id: discovered_peer.model_dump()
        for discovered_id, discovered_peer in discovered_peers(
            "netbird", peers, users
        ).items()
    }
    assert discovered == {
        "netbird::Peer[netbird,hostname=laptop]": {
            "id": "p1",
            "name": "laptop",
            "hostname": "laptop",
            "ip": "100.64.0.9",
            "ipv6": "fd00::9",
            "user_id": "u-alice",
            "user_email": "alice@example.com",
        },
        "netbird::Peer[netbird,hostname=ci]": {
            "id": "p2",
            "name": "ci",
            "hostname": "ci",
            "ip": "100.64.0.2",
            "ipv6": None,
            "user_id": None,
            "user_email": None,
        },
        "netbird::Peer[netbird,id=p3]": {
            "id": "p3",
            "name": "a",
            "hostname": "pi",
            "ip": "100.64.0.3",
            "ipv6": None,
            "user_id": None,
            "user_email": None,
        },
        "netbird::Peer[netbird,id=p4]": {
            "id": "p4",
            "name": "b",
            "hostname": "pi",
            "ip": "100.64.0.4",
            "ipv6": None,
            "user_id": "u-robot",
            "user_email": None,
        },
    }


class StubOrchestratorClient:
    """
    Stands in for the orchestrator api in the calls of the stale cleanup: it lists the
    given discovered resources, whatever the filter, and records the filters and the
    deleted ids.
    """

    def __init__(self, discovered: collections.abc.Sequence[DiscoveredResourceOutput]):
        self.discovered = discovered
        self.filters: list[collections.abc.Mapping[str, list[str]]] = []
        self.deleted: list[str] = []

    def discovered_resources_get_batch(
        self, tid: str, filter: collections.abc.Mapping[str, list[str]]
    ) -> "StubOrchestratorClient":
        self.filters.append(filter)
        return self

    async def all(self) -> collections.abc.AsyncIterator[DiscoveredResourceOutput]:
        for discovered in self.discovered:
            yield discovered

    async def discovered_resource_delete_batch(
        self, tid: str, discovered_resource_ids: collections.abc.Sequence[str]
    ) -> Result[None]:
        self.deleted.extend(discovered_resource_ids)
        return Result(code=200)


def discovered_by(
    discovered_id: ResourceIdStr, discovery_id: ResourceIdStr
) -> DiscoveredResourceOutput:
    """
    A discovered resource as the orchestrator lists it.
    """
    parsed = Id.parse_id(discovered_id)
    return DiscoveredResourceOutput(
        discovered_resource_id=discovered_id,
        values={},
        discovery_resource_id=discovery_id,
        resource_type=parsed.entity_type,
        agent=parsed.agent_name,
        resource_id_value=parsed.attribute_value,
    )


def test_peer_discovery_deletes_the_peers_that_left(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The stale cleanup only deletes the peers this discovery reported and didn't find
    anymore: not those another discovery reported.
    """
    compile_model(MODEL)
    discovery = project.get_resource("netbird::PeerDiscovery")
    assert discovery is not None
    handler = project.get_handler(discovery, run_as_root=False)

    found = ResourceIdStr("netbird::Peer[netbird,hostname=found]")
    gone = ResourceIdStr("netbird::Peer[netbird,hostname=gone]")
    client = StubOrchestratorClient(
        [
            discovered_by(found, DISCOVERY),
            discovered_by(gone, DISCOVERY),
            discovered_by(
                ResourceIdStr("netbird::Peer[netbird,hostname=other]"),
                ResourceIdStr("netbird::PeerDiscovery[other,agent_name=other]"),
            ),
        ]
    )
    handler._client = cast(Client, client)

    handler.delete_stale_discovered_resources(
        HandlerContext(discovery), discovery, [found]
    )

    assert client.filters == [
        {"agent": ["netbird"], "resource_type": ["netbird::Peer"]}
    ]
    assert client.deleted == [gone]


def test_peer_discovery_of_an_empty_account(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    An account without peers reports none.
    """
    compile_model(MODEL)
    result = project.deploy_resource_v2("netbird::PeerDiscovery")
    assert reported(result) == {}


def test_peer_discovery_reports_the_peers(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
    peer: dict,  # noqa: F811 (the fixture imported above)
) -> None:
    """
    A peer that joined the account is reported under the id of the netbird::Peer
    resource that would manage it.  It registered with a setup key, so it has no user.
    """
    compile_model(MODEL)
    result = project.deploy_resource_v2("netbird::PeerDiscovery")

    assert reported(result) == {
        f"netbird::Peer[netbird,hostname={peer['hostname']}]": {
            "id": peer["id"],
            "name": peer["name"],
            "hostname": peer["hostname"],
            "ip": peer["ip"],
            "ipv6": peer.get("ipv6") or None,
            "user_id": None,
            "user_email": None,
        },
    }
