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

import pathlib

import pytest
import pytest_inmanta.plugin
import requests
from conftest import facts, get, update_example

# The two groups the example expresses the whole account in terms of.
GATEWAY_GROUP = "office-gateways"
CLIENT_GROUP = "office-clients"

# The service user the orchestrator itself authenticates as.
SERVICE_USER = "inmanta"

NETWORK_NAME = "office"
NETWORK_RESOURCE_NAME = "office-lan"
NETWORK_RESOURCE_ADDRESS = "10.10.0.0/24"
ROUTER_NAME = "office-gateways"

ZONE_DOMAIN = "office.example.com"
PRINTER_RECORD = f"printer.{ZONE_DOMAIN}"
SCANNER_RECORD = f"scanner.{ZONE_DOMAIN}"
PRINTER_ADDRESS = "10.10.0.9"


def network_model(management_url: str) -> str:
    """
    The model the readme shows, with the url of the api the test drives substituted in.

    Nothing in it names an id: every object pointing at another one reads the
    ``id`` of the resource managing it, which is a reference on the fact that
    resource publishes and is resolved on the agent, at deploy time.
    """
    return f"""
        import netbird
        import std

        api = netbird::Api(
            agent_name="netbird",
            management_url="{management_url}",
            # A reference, not std::get_env: the token stays out of the desired state
            # and is resolved on the agent, at deploy time.
            api_token=std::create_environment_reference("NETBIRD_TOKEN"),
        )

        # The service user whose access token the orchestrator drives the account
        # with.  A service user can not log in and the api keeps no email address for
        # it, so its name is all there is to identify it by.
        netbird::User(
            api=api,
            name="{SERVICE_USER}",
            role="admin",
            is_service_user=true,
        )

        # The two groups the account is expressed in terms of: the peers routing
        # towards the office lan, and the peers allowed to reach it.  Their `peers` are
        # left null, so the model does not manage the membership — the peers get there
        # by registering with a setup key whose auto groups name these groups.
        gateways = netbird::Group(
            api=api,
            name="{GATEWAY_GROUP}",
        )
        clients = netbird::Group(
            api=api,
            name="{CLIENT_GROUP}",
        )

        # The network holding what the gateways give access to.  A netbird network is
        # nothing but that container: the addresses and the routers are objects of
        # their own, addressed under its id.
        network = netbird::Network(
            api=api,
            name="{NETWORK_NAME}",
            description="The office lan, reached through the gateways",
        )

        # What the network gives access to, and who may reach it.  The api derives the
        # type of a resource from its address — a host address, a subnet or a domain —
        # so there is no type to set here.
        netbird::NetworkResource(
            api=api,
            _network=network.id,
            name="{NETWORK_RESOURCE_NAME}",
            address="{NETWORK_RESOURCE_ADDRESS}",
            enabled=true,
            groups=[clients.id],
            # A reference is not a dependency of its own: the api refuses a resource in
            # a network that does not exist, and silently drops a group id it does not
            # know.
            requires=[network, clients],
        )

        # And who routes the traffic there: the peers of the gateway group rather than
        # one named peer.  Exactly one of `peer` and `peer_groups` may be set, the api
        # refuses a router with neither and a router with both.
        netbird::NetworkRouter(
            api=api,
            _network=network.id,
            # The api gives a router no name, it only knows it by what it routes for.
            # This one identifies the resource inmanta deploys, nothing else.
            _name="{ROUTER_NAME}",
            peer_groups=[gateways.id],
            metric=9999,
            masquerade=true,
            enabled=true,
            requires=[network, gateways],
        )

        # The zone that gives the addresses of that subnet names, resolved by the peers
        # of the client group.  Its domain is what identifies it, and the api refuses a
        # change to it once the zone exists.
        zone = netbird::DnsZone(
            api=api,
            domain="{ZONE_DOMAIN}",
            name="{NETWORK_NAME}",
            enabled=true,
            # The domain is pushed to the peers as a search domain, so that they
            # resolve the names of the zone unqualified as well.
            enable_search_domain=true,
            distribution_groups=[clients.id],
            requires=clients,
        )

        # The records of that zone.  A record name is fully qualified inside the domain
        # of its zone, without a trailing dot, and the api validates the content
        # against the type: an address for an A record, a target name for a CNAME.
        netbird::DnsZoneRecord(
            api=api,
            _zone=zone.id,
            name="{PRINTER_RECORD}",
            type="A",
            content="{PRINTER_ADDRESS}",
            ttl=300,
            requires=zone,
        )
        netbird::DnsZoneRecord(
            api=api,
            _zone=zone.id,
            name="{SCANNER_RECORD}",
            type="CNAME",
            content="{PRINTER_RECORD}",
            ttl=300,
            requires=zone,
        )
    """


def test_netbird_network(
    project: pytest_inmanta.plugin.Project,
    netbird: requests.Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Deploy a whole routed network on a fresh account: the groups it is expressed in
    terms of, the network and what it gives access to, the peers routing towards it,
    and the dns zone naming the addresses behind it.

    Every object here is addressed under the opaque id of another one, which the model
    never writes down: it reads the ``id`` of the resource managing it.  That reference
    resolves from the facts that resource publishes, so the deploy has to walk down the
    tree — and this test seeds the facts a real orchestrator would have collected,
    which ``std::create_fact_reference`` snapshots at compile time.  Hence one compile
    per level.
    """
    monkeypatch.setenv("NETBIRD_TOKEN", netbird.token)

    model = network_model(netbird.management_url)
    project.compile(model, no_dedent=False)

    # The service user first: it is what the account is driven with, and it depends on
    # nothing else.  The api stores no email address for a service user.
    project.deploy_resource("netbird::User")
    user = next(u for u in get(netbird, "users") if u["name"] == SERVICE_USER)
    assert user["is_service_user"] is True
    assert user["email"] == ""

    # Then the groups, whose ids everything below points at.
    group_ids = {}
    for name in [GATEWAY_GROUP, CLIENT_GROUP]:
        resource = project.deploy_resource("netbird::Group", name=name)
        group_ids[name] = facts(project)["id"]
        project.add_fact(resource.id.resource_str(), "id", group_ids[name])

    # The network, which the resource and the router below are addressed under.  Its
    # own desired state names nothing, so it deploys before any fact is seeded.
    project.compile(model, no_dedent=False)
    network_resource = project.deploy_resource("netbird::Network")
    network_id = facts(project)["id"]
    project.add_fact(network_resource.id.resource_str(), "id", network_id)

    # With the network and the groups known, everything hanging off them converges.
    project.compile(model, no_dedent=False)

    project.deploy_resource("netbird::NetworkResource")
    resources = get(netbird, f"networks/{network_id}/resources")
    assert [r["name"] for r in resources] == [NETWORK_RESOURCE_NAME]
    assert resources[0]["address"] == NETWORK_RESOURCE_ADDRESS
    # The api derives the type from the address, and reports the groups as objects.
    assert resources[0]["type"] == "subnet"
    assert [g["id"] for g in resources[0]["groups"]] == [group_ids[CLIENT_GROUP]]

    project.deploy_resource("netbird::NetworkRouter")
    routers = get(netbird, f"networks/{network_id}/routers")
    assert len(routers) == 1
    assert routers[0]["peer_groups"] == [group_ids[GATEWAY_GROUP]]
    assert routers[0]["masquerade"] is True
    assert routers[0]["metric"] == 9999

    zone_resource = project.deploy_resource("netbird::DnsZone")
    zone = next(z for z in get(netbird, "dns/zones") if z["domain"] == ZONE_DOMAIN)
    assert zone["enabled"] is True
    assert zone["distribution_groups"] == [group_ids[CLIENT_GROUP]]
    project.add_fact(zone_resource.id.resource_str(), "id", zone["id"])

    # And the records, addressed under the id of the zone that was just created.
    project.compile(model, no_dedent=False)
    for name in [PRINTER_RECORD, SCANNER_RECORD]:
        project.deploy_resource("netbird::DnsZoneRecord", name=name)

    records = {
        record["name"]: record
        for record in get(netbird, f"dns/zones/{zone['id']}/records")
    }
    assert records[PRINTER_RECORD]["type"] == "A"
    assert records[PRINTER_RECORD]["content"] == PRINTER_ADDRESS
    assert records[PRINTER_RECORD]["ttl"] == 300
    assert records[SCANNER_RECORD]["type"] == "CNAME"
    assert records[SCANNER_RECORD]["content"] == PRINTER_RECORD

    tested_model = pathlib.Path(project._test_project_dir, "main.cf").read_text()
    tested_model = tested_model.replace(
        netbird.management_url, "https://api.netbird.io"
    )
    update_example("netbird-network", tested_model)
