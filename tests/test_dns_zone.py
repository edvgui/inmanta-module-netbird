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
import pytest
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP, facts, get

import inmanta.ast
import inmanta.plugins
from inmanta import const

ZONE_DOMAIN = "zone.example.com"
ZONE_NAME = "Example zone"
RECORD_NAME = f"www.{ZONE_DOMAIN}"

Compile = collections.abc.Callable[[str], None]


def find_zone(netbird: requests.Session, domain: str) -> dict | None:
    return next((z for z in get(netbird, "dns/zones") if z["domain"] == domain), None)


def find_record(
    netbird: requests.Session, zone: str, name: str, type: str = "A"
) -> dict | None:
    records = get(netbird, f"dns/zones/{zone}/records")
    return next(
        (r for r in records if r["name"] == name and r["type"] == type),
        None,
    )


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def attributes(**attributes: object) -> list[str]:
    """
    The attributes of an entity, as dsl lines.  The values are serialized as json,
    which the dsl takes as is for the primitive values a netbird object is made of.
    """
    return [f"    {key}={json.dumps(value)}," for key, value in attributes.items()]


def zone_model(**attrs: object) -> str:
    """
    Build a dns zone resource, with only the attributes the test has an opinion
    about: everything else is left null, and is therefore not managed.
    """
    return "\n".join(
        [
            "zone = netbird::DnsZone(",
            "    api=api,",
            f"    domain={json.dumps(ZONE_DOMAIN)},",
            *attributes(**attrs),
            ")",
        ]
    )


def record_model(variable: str = "record", **attrs: object) -> str:
    """
    Build a dns record resource under the zone the model declares, addressed by the id
    the zone published as a fact: the api knows the zone by an opaque id, and the model
    never sees it.

    :param variable: The name of the dsl variable to bind the record to, so that a
        model can hold more than one.
    """
    return "\n".join(
        [
            f"{variable} = netbird::DnsZoneRecord(",
            "    api=api,",
            "    _zone=zone.id,",
            *attributes(**attrs),
            ")",
        ]
    )


def deploy_zone(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> str:
    """
    Deploy the zone the records of the tests hang off, and feed the id the api gave it
    back to the compiler: the records read it from the facts of the zone, which the
    test runner only resolves once it put them there itself.
    """
    compile_model(
        zone_model(
            name=ZONE_NAME, distribution_groups=[group_id(netbird, DEFAULT_GROUP)]
        )
    )
    resource = project.deploy_resource("netbird::DnsZone")

    zone = find_zone(netbird, ZONE_DOMAIN)
    assert zone is not None
    project.add_fact(resource.id.resource_str(), "id", zone["id"])
    return zone["id"]


def test_zone_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A zone is created on the account, the settings the model has an opinion about are
    kept in line with it, and the zone is removed when the resource is purged.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(zone_model(name=ZONE_NAME, distribution_groups=[all_group]))
    project.deploy_resource("netbird::DnsZone")

    zone = find_zone(netbird, ZONE_DOMAIN)
    assert zone is not None
    # The id the api gave the zone is published, already on the deploy that created
    # it.
    assert facts(project) == {"id": zone["id"]}
    assert zone["name"] == ZONE_NAME
    assert zone["distribution_groups"] == [all_group]
    assert zone["enabled"] is True
    assert zone["enable_search_domain"] is False

    # A second deploy of the same desired state changes nothing.
    compile_model(zone_model(name=ZONE_NAME, distribution_groups=[all_group]))
    project.deploy_resource("netbird::DnsZone", change=const.Change.nochange)

    compile_model(
        zone_model(
            name="Renamed zone",
            distribution_groups=[all_group],
            enabled=False,
            enable_search_domain=True,
        )
    )
    project.deploy_resource("netbird::DnsZone")
    zone = find_zone(netbird, ZONE_DOMAIN)
    assert zone["name"] == "Renamed zone"
    assert zone["enabled"] is False
    assert zone["enable_search_domain"] is True

    compile_model(zone_model(purged=True))
    project.deploy_resource("netbird::DnsZone")
    assert find_zone(netbird, ZONE_DOMAIN) is None


def test_zone_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api replaces the whole zone on an update: every key left out of the body is
    reset to its zero value.  The handler writes back the account's own state with
    the desired one merged on top, so a value the model doesn't manage survives a
    deploy that changes one that it does.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(zone_model(name=ZONE_NAME, distribution_groups=[all_group]))
    project.deploy_resource("netbird::DnsZone")

    zone = find_zone(netbird, ZONE_DOMAIN)
    netbird.put(
        f"{netbird.base_url}/dns/zones/{zone['id']}",
        json={
            "name": ZONE_NAME,
            "domain": ZONE_DOMAIN,
            "distribution_groups": [all_group],
            "enabled": False,
            "enable_search_domain": True,
        },
    ).raise_for_status()

    # Neither flag is managed: the deploy leaves both as the dashboard set them.
    compile_model(zone_model(name=ZONE_NAME, distribution_groups=[all_group]))
    project.deploy_resource("netbird::DnsZone", change=const.Change.nochange)
    zone = find_zone(netbird, ZONE_DOMAIN)
    assert zone["enabled"] is False
    assert zone["enable_search_domain"] is True

    # Changing the name doesn't reset them either, even though the api takes the
    # whole object on the update that carries the new name.
    compile_model(zone_model(name="Renamed zone"))
    project.deploy_resource("netbird::DnsZone")
    zone = find_zone(netbird, ZONE_DOMAIN)
    assert zone["name"] == "Renamed zone"
    assert zone["enabled"] is False
    assert zone["enable_search_domain"] is True
    assert zone["distribution_groups"] == [all_group]


def test_zone_distribution_groups_order_is_not_a_change(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The distribution groups are a set, and the api keeps them in the order they were
    sent: listing the same groups in another order must not show up as a change.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    netbird.post(
        f"{netbird.base_url}/groups", json={"name": "other"}
    ).raise_for_status()
    other_group = group_id(netbird, "other")

    compile_model(
        zone_model(name=ZONE_NAME, distribution_groups=[all_group, other_group])
    )
    project.deploy_resource("netbird::DnsZone")
    assert sorted(find_zone(netbird, ZONE_DOMAIN)["distribution_groups"]) == sorted(
        [all_group, other_group]
    )

    compile_model(
        zone_model(name=ZONE_NAME, distribution_groups=[other_group, all_group])
    )
    project.deploy_resource("netbird::DnsZone", change=const.Change.nochange)


def test_record_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A record is created under its zone, and the values the model manages are kept in
    line with it.  The record is addressed by its name together with its type, and
    the api replaces the whole record on an update, so a ttl the model doesn't manage
    has to survive a change to the content.

    The zone is the reference the model carries, resolved on the agent at deploy time:
    every call this handler makes is addressed under an id no compile ever saw.
    """
    zone = deploy_zone(project, compile_model, netbird)

    def deploy_record(**attrs: object) -> None:
        compile_model(zone_model() + "\n" + record_model(name=RECORD_NAME, **attrs))
        project.deploy_resource("netbird::DnsZoneRecord")

    deploy_record(type="A", content="10.0.0.1", ttl=300)

    record = find_record(netbird, zone, RECORD_NAME)
    assert record is not None
    assert record["type"] == "A"
    assert record["content"] == "10.0.0.1"
    assert record["ttl"] == 300
    # The id the api gave the record is published, already on the deploy that
    # created it.
    assert facts(project) == {"id": record["id"]}

    # A second deploy of the same desired state changes nothing.
    compile_model(
        zone_model()
        + "\n"
        + record_model(name=RECORD_NAME, type="A", content="10.0.0.1", ttl=300)
    )
    project.deploy_resource("netbird::DnsZoneRecord", change=const.Change.nochange)

    deploy_record(type="A", content="10.0.0.2")
    record = find_record(netbird, zone, RECORD_NAME)
    assert record["content"] == "10.0.0.2"
    # The api resets a ttl left out of the update to zero.  The model doesn't manage
    # it, and the handler carries the account's own value along instead.
    assert record["ttl"] == 300

    deploy_record(type="A", content="10.0.0.2", purged=True)
    assert find_record(netbird, zone, RECORD_NAME) is None


def test_records_of_the_types_the_api_takes(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api takes an `A`, an `AAAA` and a `CNAME` record, and nothing else.  A record
    is identified by its name together with its type, so a dual stack host is two
    resources sharing one name, and neither of them is the other one's update.
    """
    zone = deploy_zone(project, compile_model, netbird)

    compile_model(
        "\n".join(
            [
                zone_model(),
                record_model("v4", name=RECORD_NAME, type="A", content="10.0.0.1"),
                record_model(
                    "v6", name=RECORD_NAME, type="AAAA", content="2001:db8::1"
                ),
                record_model(
                    "alias",
                    name=f"alias.{ZONE_DOMAIN}",
                    type="CNAME",
                    content=RECORD_NAME,
                ),
            ]
        )
    )
    for type in ("A", "AAAA", "CNAME"):
        project.deploy_resource("netbird::DnsZoneRecord", type=type)

    assert find_record(netbird, zone, RECORD_NAME, "A")["content"] == "10.0.0.1"
    # The ipv6 record didn't take the place of the ipv4 one, it is another record
    assert find_record(netbird, zone, RECORD_NAME, "AAAA")["content"] == "2001:db8::1"
    assert (
        find_record(netbird, zone, f"alias.{ZONE_DOMAIN}", "CNAME")["content"]
        == RECORD_NAME
    )

    # Any other type is refused by the api, so the model doesn't let one through
    # either: the compiler is where that is reported, not a failing deploy.
    with pytest.raises(inmanta.ast.RuntimeException, match="attribute `type`"):
        compile_model(
            zone_model()
            + "\n"
            + record_model(name=RECORD_NAME, type="TXT", content="hello")
        )


def test_record_the_zone_holds_twice_is_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api gives a record no key of its own, and holds a second one with the same
    name and type as long as the content differs.  This resource addresses a record by
    its name and its type: when the zone holds two of them, there is no telling which
    one the model means, and the deploy fails rather than picking one.
    """
    zone = deploy_zone(project, compile_model, netbird)

    for content in ("10.0.0.1", "10.0.0.2"):
        netbird.post(
            f"{netbird.base_url}/dns/zones/{zone}/records",
            json={"name": RECORD_NAME, "type": "A", "content": content},
        ).raise_for_status()

    compile_model(
        zone_model()
        + "\n"
        + record_model(name=RECORD_NAME, type="A", content="10.0.0.1")
    )
    project.deploy_resource("netbird::DnsZoneRecord", status=const.ResourceState.failed)


def test_record_zone_is_a_fact_reference(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    A record points at its zone by the opaque id the api gave it, which the model
    never knows: taking it from the zone resource yields a reference the agent
    resolves from the facts the zone's handler publishes.
    """
    compile_model(
        zone_model(name=ZONE_NAME)
        + "\n"
        + record_model(name=RECORD_NAME, type="A", content="10.0.0.1")
    )

    (record,) = project.get_instances("netbird::DnsZoneRecord")
    zone = inmanta.plugins.allow_reference_values(record)._zone
    assert isinstance(zone, inmanta_plugins.std.FactReference)
    assert zone.fact_name == "id"
