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
GOOGLE = {"ip": "8.8.8.8", "ns_type": "udp", "port": 53}

Compile = collections.abc.Callable[[str], None]


def find_nameserver_group(netbird: requests.Session, name: str) -> dict | None:
    return next(
        (g for g in get(netbird, "dns/nameservers") if g["name"] == name),
        None,
    )


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def by_ip(nameservers: list[dict]) -> list[dict]:
    """
    The nameservers of a group, in a deterministic order.  The api keeps them in the
    order it was given, but the desired state addresses each of them by its ip
    address, so the model can not express one: the order is not managed.
    """
    return sorted(nameservers, key=lambda nameserver: nameserver["ip"])


def nameserver_group_model(
    *,
    nameservers: list[dict] | None = None,
    **attributes: object,
) -> str:
    """
    Build a nameserver group resource, with only the attributes the test has an
    opinion about: everything else is left null, and is therefore not managed.

    The dns servers of the group are embedded entities rather than an attribute: the
    api holds them as a list of objects, which the dsl has no attribute type for.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    servers = ", ".join(
        "netbird::Nameserver("
        + ", ".join(f"{key}={json.dumps(value)}" for key, value in server.items())
        + ")"
        for server in nameservers or []
    )
    return "\n".join(
        [
            "group = netbird::NameserverGroup(",
            "    api=api,",
            f"    name={json.dumps(GROUP_NAME)},",
            *([f"    nameservers=[{servers}],"] if nameservers is not None else []),
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
    # The model said nothing about the enabled flag, and the api does not default it
    # to true: a group is created disabled.
    assert group["enabled"] is False

    # A second deploy of the same desired state changes nothing.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)

    # Google's nameserver is declared by its address alone: the api requires a protocol
    # and a port on every one of them, and the handler completes the ones the model
    # didn't spell out.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9, CLOUDFLARE, {"ip": GOOGLE["ip"]}],
            groups=[all_group],
            primary=True,
            enabled=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup")
    group = find_nameserver_group(netbird, GROUP_NAME)
    assert by_ip(group["nameservers"]) == by_ip([QUAD9, CLOUDFLARE, GOOGLE])
    assert group["enabled"] is True

    # And the nameservers the model spelled out again are not a change.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9, CLOUDFLARE, GOOGLE],
            groups=[all_group],
            primary=True,
            enabled=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)

    # A dns server the model stops naming is not removed: the desired state addresses
    # each of them by ip address, so the model only manages the ones it names, and the
    # account keeps the others.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
            enabled=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)
    group = find_nameserver_group(netbird, GROUP_NAME)
    assert by_ip(group["nameservers"]) == by_ip([QUAD9, CLOUDFLARE, GOOGLE])

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
    it, even when it was changed in the dashboard behind our back.  The api replaces
    the whole group on an update, so those values are carried along rather than reset.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    model = nameserver_group_model(
        nameservers=[QUAD9],
        groups=[all_group],
        primary=True,
        description="managed by inmanta",
    )

    compile_model(model)
    project.deploy_resource("netbird::NameserverGroup")

    group = find_nameserver_group(netbird, GROUP_NAME)
    netbird.put(
        f"{netbird.base_url}/dns/nameservers/{group['id']}",
        json=dict(group, description="changed in the dashboard", enabled=True),
    ).raise_for_status()

    # The enabled flag isn't managed: the deploy leaves it as the dashboard set it,
    # and the description is enforced back.
    compile_model(model)
    project.deploy_resource("netbird::NameserverGroup")
    group = find_nameserver_group(netbird, GROUP_NAME)
    assert group["description"] == "managed by inmanta"
    assert group["enabled"] is True


def test_nameserver_group_distribution_groups_are_ids(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api identifies the groups of peers resolving with a nameserver group by id,
    and so does the model: no name is translated on the way in or out, so the same
    desired state is a no-op on the second deploy whatever order they are written in.
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

    # The api keeps the distribution groups in the order it was given them, but they
    # are a set here: writing them in another order is not a change.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=list(reversed(distribution)),
            primary=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)


def test_nameserver_group_resolves_domains_when_it_is_not_primary(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A group that is not the primary one resolves the domains it is given, and can
    push them to the peers as search domains.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=False,
            domains=["example.com", "aaa.com"],
            search_domains_enabled=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup")

    group = find_nameserver_group(netbird, GROUP_NAME)
    assert group["primary"] is False
    # The api keeps the domains in the order it was given them, the handler sorts
    # them: they are a set here, like the distribution groups.
    assert group["domains"] == ["aaa.com", "example.com"]
    assert group["search_domains_enabled"] is True

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=False,
            domains=["example.com", "aaa.com"],
            search_domains_enabled=True,
        )
    )
    project.deploy_resource("netbird::NameserverGroup", change=const.Change.nochange)


def test_a_group_the_api_rejects_fails_the_deploy(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api has rules of its own about what a nameserver group may look like, and
    this module does not repeat them: it reports what the api answered as a failed
    deploy rather than deciding on its own that the model can not be applied.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    # A group holds one to three nameservers.  The api says "the list of nameservers
    # should be 1 or 3" and takes two all the same, four is what it refuses.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9, CLOUDFLARE, GOOGLE, {"ip": "8.8.4.4"}],
            groups=[all_group],
            primary=True,
        )
    )
    project.deploy_resource(
        "netbird::NameserverGroup", status=const.ResourceState.failed
    )

    # And it needs at least one group to distribute the nameservers to.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[],
            primary=True,
        )
    )
    project.deploy_resource(
        "netbird::NameserverGroup", status=const.ResourceState.failed
    )

    # A group is either the primary one, or it has domains.
    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=True,
            domains=["example.com"],
        )
    )
    project.deploy_resource(
        "netbird::NameserverGroup", status=const.ResourceState.failed
    )

    compile_model(
        nameserver_group_model(
            nameservers=[QUAD9],
            groups=[all_group],
            primary=False,
            domains=[],
        )
    )
    project.deploy_resource(
        "netbird::NameserverGroup", status=const.ResourceState.failed
    )

    assert find_nameserver_group(netbird, GROUP_NAME) is None


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


def test_dns_settings_can_not_be_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api has no endpoint to delete the dns settings of an account, they are part
    of the account itself.  Purging the resource is therefore reported as a failed
    deploy rather than as a deploy that did nothing.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(dns_settings_model(disabled_management_groups=[all_group]))
    project.deploy_resource("netbird::DnsSettings")

    compile_model(
        dns_settings_model(disabled_management_groups=[all_group], purged=True)
    )
    project.deploy_resource("netbird::DnsSettings", status=const.ResourceState.failed)
    assert get(netbird, "dns/settings") == {"disabled_management_groups": [all_group]}
