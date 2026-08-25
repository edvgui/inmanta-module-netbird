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
from conftest import DEFAULT_GROUP, facts, get
from test_peer import peer  # noqa: F401

from inmanta import const

NETWORK_NAME = "office"
RESOURCE_NAME = "printer"
RESOURCE_ADDRESS = "10.0.0.1"
ROUTER_NAME = "gateway"

Compile = collections.abc.Callable[[str], None]


def find_network(netbird: requests.Session, name: str) -> dict | None:
    return next((n for n in get(netbird, "networks") if n["name"] == name), None)


def network_id(netbird: requests.Session) -> str:
    network = find_network(netbird, NETWORK_NAME)
    assert network is not None
    return network["id"]


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def attributes(**attributes: object) -> list[str]:
    """
    The attributes of an entity, as dsl lines.  The values are serialized as json,
    which the dsl takes as is for the primitive values a netbird object is made of.
    """
    return [f"    {key}={json.dumps(value)}," for key, value in attributes.items()]


def network_model(**attrs: object) -> str:
    """
    Build a network resource, with only the attributes the test has an opinion about:
    everything else is left null, and is therefore not managed.
    """
    return "\n".join(
        [
            "network = netbird::Network(",
            "    api=api,",
            f"    name={json.dumps(NETWORK_NAME)},",
            *attributes(**attrs),
            ")",
        ]
    )


def resource_model(address: str = RESOURCE_ADDRESS, **attrs: object) -> str:
    """
    Build a network resource of the network the model declares, addressed under the id
    the network published as a fact.

    The address is the one value that is not optional: the api refuses to store a
    resource without one.
    """
    return "\n".join(
        [
            "resource = netbird::NetworkResource(",
            "    api=api,",
            "    _network=network.id,",
            f"    name={json.dumps(RESOURCE_NAME)},",
            f"    address={json.dumps(address)},",
            *attributes(**attrs),
            ")",
        ]
    )


def router_model(**attrs: object) -> str:
    """
    Build a router of the network the model declares, addressed under the id the
    network published as a fact.
    """
    return "\n".join(
        [
            "router = netbird::NetworkRouter(",
            "    api=api,",
            "    _network=network.id,",
            f"    _name={json.dumps(ROUTER_NAME)},",
            *attributes(**attrs),
            ")",
        ]
    )


def deploy_network(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> str:
    """
    Deploy the network the resources and the routers of the tests hang off, and feed
    the id the api gave it back to the compiler: the objects addressed under it read
    it from the facts of the network, which the test runner only knows about once it
    put them there itself.
    """
    compile_model(network_model())
    resource = project.deploy_resource("netbird::Network")

    identifier = network_id(netbird)
    project.add_fact(resource.id.resource_str(), "id", identifier)
    return identifier


def test_network_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A network is created on the account, kept in line with the model, and removed when
    the resource is purged.
    """
    compile_model(network_model(description="The office network"))
    project.deploy_resource("netbird::Network")

    network = find_network(netbird, NETWORK_NAME)
    assert network is not None
    # The id the api gave the network is published, already on the deploy that
    # created it.
    assert facts(project) == {"id": network["id"]}
    assert network["description"] == "The office network"

    # A second deploy of the same desired state changes nothing: the routers, the
    # resources and the policies the api reports on a network are computed from the
    # objects pointing at it, they are no part of the desired state.
    compile_model(network_model(description="The office network"))
    project.deploy_resource("netbird::Network", change=const.Change.nochange)

    compile_model(network_model(description="The other office"))
    project.deploy_resource("netbird::Network")
    assert find_network(netbird, NETWORK_NAME)["description"] == "The other office"

    compile_model(network_model(purged=True))
    project.deploy_resource("netbird::Network")
    assert find_network(netbird, NETWORK_NAME) is None


def test_network_resource_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A resource of a network is addressed under the id of its network, which the model
    only knows as a reference to the fact the network publishes.  The api derives the
    type of the resource from its address, and reports a host address as the network
    it is alone in: neither of them may show up as a change on the next deploy.
    """
    identifier = deploy_network(project, compile_model, netbird)
    all_group = group_id(netbird, DEFAULT_GROUP)

    def resources() -> list[dict]:
        # A network holding no resource at all reports a json null rather than an empty
        # list.
        return get(netbird, f"networks/{identifier}/resources") or []

    compile_model(
        network_model() + "\n" + resource_model(enabled=True, groups=[all_group])
    )
    project.deploy_resource("netbird::NetworkResource")

    (resource,) = resources()
    assert facts(project) == {"id": resource["id"]}
    assert resource["name"] == RESOURCE_NAME
    # The api stores a host address as the network it is alone in
    assert resource["address"] == "10.0.0.1/32"
    # And it derives the type of the resource from the address: the model never sets it.
    assert resource["type"] == "host"
    assert resource["enabled"] is True
    assert [group["id"] for group in resource["groups"]] == [all_group]

    # The resources the api reports on the network are computed from the objects that
    # point at it: the network holding one now is no change to the network itself.
    project.deploy_resource("netbird::Network", change=const.Change.nochange)

    compile_model(
        network_model() + "\n" + resource_model(enabled=True, groups=[all_group])
    )
    project.deploy_resource("netbird::NetworkResource", change=const.Change.nochange)

    # A subnet, and the description the first deploy had no opinion about.  The api
    # resets everything the update doesn't carry, so the groups the model doesn't
    # manage anymore are the ones the account holds, not an empty list.
    compile_model(
        network_model()
        + "\n"
        + resource_model("10.0.0.0/24", description="The office subnet")
    )
    project.deploy_resource("netbird::NetworkResource")

    (resource,) = resources()
    assert resource["address"] == "10.0.0.0/24"
    # The type follows the address, without the model ever mentioning it.
    assert resource["type"] == "subnet"
    assert resource["description"] == "The office subnet"
    assert [group["id"] for group in resource["groups"]] == [all_group]
    assert resource["enabled"] is True

    compile_model(network_model() + "\n" + resource_model(purged=True))
    project.deploy_resource("netbird::NetworkResource")
    assert resources() == []


def test_network_router_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A router says which peers route the traffic of a network.  The api gives it no
    name of its own, it is only known by the peer or the peer groups it routes for,
    and it insists on getting that target back on every update.
    """
    identifier = deploy_network(project, compile_model, netbird)
    all_group = group_id(netbird, DEFAULT_GROUP)

    def routers() -> list[dict]:
        return get(netbird, f"networks/{identifier}/routers") or []

    compile_model(
        network_model()
        + "\n"
        + router_model(peer_groups=[all_group], metric=100, masquerade=True)
    )
    project.deploy_resource("netbird::NetworkRouter")

    (router,) = routers()
    assert facts(project) == {"id": router["id"]}
    assert router["peer_groups"] == [all_group]
    assert router["metric"] == 100
    assert router["masquerade"] is True

    compile_model(
        network_model()
        + "\n"
        + router_model(peer_groups=[all_group], metric=100, masquerade=True)
    )
    project.deploy_resource("netbird::NetworkRouter", change=const.Change.nochange)

    # The target of the router is not what is being changed here, and the api requires
    # it on every update all the same: it is carried along by the merged body.
    compile_model(
        network_model() + "\n" + router_model(peer_groups=[all_group], enabled=False)
    )
    project.deploy_resource("netbird::NetworkRouter")

    (router,) = routers()
    assert router["enabled"] is False
    assert router["peer_groups"] == [all_group]
    assert router["metric"] == 100
    assert router["masquerade"] is True

    compile_model(
        network_model() + "\n" + router_model(peer_groups=[all_group], purged=True)
    )
    project.deploy_resource("netbird::NetworkRouter")
    assert routers() == []


def test_network_router_for_a_peer(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
    peer: dict,  # noqa: F811
) -> None:
    """
    A router routes either for one peer or for the peers of a set of groups, and the api
    takes exactly one of the two: it refuses a router with neither and a router with
    both.  A router pointing at a peer carries its id, which the model never knows
    itself: the peer resource publishes it as a fact.

    The peer is a real netbird client, registered by the fixture this module imports from
    the peer tests: a peer can not be created through the api.
    """
    identifier = deploy_network(project, compile_model, netbird)
    all_group = group_id(netbird, DEFAULT_GROUP)

    def routers() -> list[dict]:
        return get(netbird, f"networks/{identifier}/routers") or []

    compile_model(
        network_model()
        + "\n"
        + router_model(peer=peer["id"], metric=100, masquerade=True)
    )
    project.deploy_resource("netbird::NetworkRouter")

    (router,) = routers()
    assert facts(project) == {"id": router["id"]}
    assert router["peer"] == peer["id"]
    # The api reports the half of the pair the router doesn't use as an empty value
    assert router["peer_groups"] is None
    assert router["metric"] == 100
    assert router["enabled"] is True

    compile_model(
        network_model()
        + "\n"
        + router_model(peer=peer["id"], metric=100, masquerade=True)
    )
    project.deploy_resource("netbird::NetworkRouter", change=const.Change.nochange)

    # The peer the router routes for is not what is being changed here, and the api
    # requires it on every update all the same: the merged body carries it along.
    compile_model(network_model() + "\n" + router_model(peer=peer["id"], metric=50))
    project.deploy_resource("netbird::NetworkRouter")

    (router,) = routers()
    assert router["peer"] == peer["id"]
    assert router["metric"] == 50
    assert router["masquerade"] is True

    # A router routing for a peer and for groups at once is not something the api
    # stores, and neither is one routing for nothing at all.
    compile_model(
        network_model() + "\n" + router_model(peer=peer["id"], peer_groups=[all_group])
    )
    project.deploy_resource("netbird::NetworkRouter", status=const.ResourceState.failed)

    compile_model(network_model() + "\n" + router_model(metric=10))
    project.deploy_resource("netbird::NetworkRouter", status=const.ResourceState.failed)

    compile_model(network_model() + "\n" + router_model(peer=peer["id"], purged=True))
    project.deploy_resource("netbird::NetworkRouter")
    assert routers() == []
