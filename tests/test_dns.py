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

from inmanta import const

GROUP_NAME = "dns"

QUAD9 = {"ip": "9.9.9.9", "ns_type": "udp", "port": 53}
CLOUDFLARE = {"ip": "1.1.1.1", "ns_type": "udp", "port": 53}

Compile = collections.abc.Callable[[str], None]


def find_nameserver_group(netbird: requests.Session, name: str) -> dict | None:
    return next(
        (g for g in get(netbird, "dns/nameservers") if g["name"] == name),
        None,
    )


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def nameserver_group_model(**attributes: object) -> str:
    """
    Build a nameserver group resource, with only the attributes the test has an
    opinion about: everything else is left null, and is therefore not managed.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    return "\n".join(
        [
            "group = netbird::NameserverGroup(",
            "    api=api,",
            f"    name={json.dumps(GROUP_NAME)},",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def dns_settings_model(**attributes: object) -> str:
    """
    Build the dns settings resource of the account.
    """
    return "\n".join(
        [
            "settings = netbird::DnsSettings(",
            "    api=api,",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_nameserver_group_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A nameserver group is created on the account, kept in line with the model, and
    removed when the resource is purged.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup")

    group = find_nameserver_group(netbird, GROUP_NAME)
    assert group is not None
    # The id the api gave the group is published, already on the deploy that
    # created it.
    assert facts(project) == {"id": group["id"]}
    assert group["nameservers"] == [QUAD9]
    assert group["groups"] == [all_group]
    assert group["primary"] is True
    assert group["enabled"] is True

    # A second deploy of the same desired state changes nothing.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9, CLOUDFLARE],
            groups=[all_group],
            primary=True,
            enabled=False,
        )
    )
    project.deploy_resource("netbird::NameserverGroup")
    group = find_nameserver_group(netbird, GROUP_NAME)
    # The nameservers are queried in the order they are written in, so that order is
    # part of the desired state and is not sorted away.
    assert group["nameservers"] == [QUAD9, CLOUDFLARE]
    assert group["enabled"] is False

    compile_model(nameserver_group_model(purged=True))
    project.deploy_resource("netbird::NameserverGroup")
    assert find_nameserver_group(netbird, GROUP_NAME) is None


def test_nameserver_group_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    Every attribute the model leaves null keeps whatever value the account holds for
    it, even when it was changed in the dashboard behind our back.  The api requires
    the full object on an update, so those values are carried along rather than reset.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
            description="managed by inmanta",
        )
    )
    project.deploy_resource("netbird::NameserverGroup")

    group = find_nameserver_group(netbird, GROUP_NAME)
    netbird.put(
        f"{netbird.base_url}/dns/nameservers/{group['id']}",
        json=dict(group, description="changed in the dashboard", enabled=False),
    ).raise_for_status()

    # The enabled flag isn't managed: the deploy leaves it as it is, and the
    # description is enforced back.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
            description="managed by inmanta",
        )
    )
    project.deploy_resource("netbird::NameserverGroup")
    group = find_nameserver_group(netbird, GROUP_NAME)
    assert group["description"] == "managed by inmanta"
    assert group["enabled"] is False


def test_nameserver_group_distribution_groups_are_ids(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api identifies the groups of peers resolving with a nameserver group by id,
    and so does the model: no name is translated on the way in or out, so the same
    desired state is a no-op on the second deploy whatever order the api returns
    them in.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    other = netbird.post(f"{netbird.base_url}/groups", json={"name": "other"})
    other.raise_for_status()
    other_group = other.json()["id"]
    distribution = sorted([all_group, other_group])

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=distribution,
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup")
    assert sorted(find_nameserver_group(netbird, GROUP_NAME)["groups"]) == distribution

    # The api doesn't preserve the order of the distribution groups, which is not a
    # change of the desired state.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=list(reversed(distribution)),
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)


def test_dns_settings_updated(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The dns settings are a singleton of the account: they always exist, so the
    resource only ever updates them, and deploying the same desired state twice is a
    no-op.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(dns_settings_model(disabled_management_groups=[all_group]))
    project.deploy_resource("netbird::DnsSettings")
    assert get(netbird, "dns/settings") == {"disabled_management_groups": [all_group]}

    compile_model(dns_settings_model(disabled_management_groups=[all_group]))
    project.deploy_resource("netbird::DnsSettings", change=const.Change.nochange)

    compile_model(dns_settings_model(disabled_management_groups=[]))
    project.deploy_resource("netbird::DnsSettings")
    assert get(netbird, "dns/settings") == {"disabled_management_groups": []}
