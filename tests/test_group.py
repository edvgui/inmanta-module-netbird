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

import inmanta_plugins.std
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP, facts, get

import inmanta.plugins
from inmanta import const

GROUP_NAME = "devs"

Compile = collections.abc.Callable[[str], None]


def find_group(netbird: requests.Session, name: str) -> dict | None:
    return next((g for g in get(netbird, "groups") if g["name"] == name), None)


def group_model(name: str = GROUP_NAME, **attributes: object) -> str:
    """
    Build a group resource, with only the attributes the test has an opinion about:
    everything else is left null, and is therefore not managed.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    return "\n".join(
        [
            "group = netbird::Group(",
            "    api=api,",
            f"    name={json.dumps(name)},",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_group_created_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A group is created on the account, deploying the same desired state again is a
    no-op, and the group is removed when the resource is purged.
    """
    compile_model(group_model(peers=[]))
    project.deploy_resource("netbird::Group")

    group = find_group(netbird, GROUP_NAME)
    assert group is not None
    # The id the api gave the group is published, already on the deploy that
    # created it.
    assert facts(project) == {"id": group["id"]}
    # An empty collection comes back as a json null rather than an empty list.
    assert group["peers"] is None
    assert group["peers_count"] == 0

    compile_model(group_model(peers=[]))
    project.deploy_resource("netbird::Group", change=const.Change.nochange)

    compile_model(group_model(purged=True))
    project.deploy_resource("netbird::Group")
    assert find_group(netbird, GROUP_NAME) is None


def test_group_without_peers_attribute(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The members of a group are not managed when the model has no opinion about them:
    the group is created empty, and whoever adds a peer to it afterwards keeps it.
    """
    compile_model(group_model())
    project.deploy_resource("netbird::Group")
    assert find_group(netbird, GROUP_NAME) is not None

    compile_model(group_model())
    project.deploy_resource("netbird::Group", change=const.Change.nochange)


def test_values_the_api_computes_are_not_a_change(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A group also carries the network resources it holds, which this module doesn't
    manage, and the issuer and the counts the api computes on its own.  None of them
    is something the model has an opinion about, so none of them may show up as a
    change, and the ones the api takes on a write are carried along untouched.
    """
    netbird.post(
        f"{netbird.base_url}/groups",
        json={
            "name": GROUP_NAME,
            "resources": [{"id": "unknown-resource", "type": "host"}],
        },
    ).raise_for_status()

    compile_model(group_model(peers=[]))
    project.deploy_resource("netbird::Group", change=const.Change.nochange)

    group = find_group(netbird, GROUP_NAME)
    assert group["resources"] == [{"id": "unknown-resource", "type": "host"}]
    assert group["issued"] == "api"


def test_the_default_group_can_not_be_removed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    Every fresh account comes with a group holding all of its peers, which the api
    refuses to delete.  That refusal is reported as a failed deploy rather than
    silently ignored: the model asked for something the account can not do.
    """
    compile_model(group_model(name=DEFAULT_GROUP))
    project.deploy_resource("netbird::Group", change=const.Change.nochange)

    compile_model(group_model(name=DEFAULT_GROUP, purged=True))
    project.deploy_resource("netbird::Group", status=const.ResourceState.failed)
    assert find_group(netbird, DEFAULT_GROUP) is not None


def test_id_is_a_fact_reference(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The api addresses every object by an opaque id the model never knows.  It is
    exposed on the resource all the same, as a reference that reads it back from the
    facts the handler publishes, so another resource can point at this group without
    going digging through the api.
    """
    compile_model(group_model())

    (group,) = project.get_instances("netbird::Group")
    object_id = inmanta.plugins.allow_reference_values(group).id
    assert isinstance(object_id, inmanta_plugins.std.FactReference)
    assert object_id.fact_name == "id"
