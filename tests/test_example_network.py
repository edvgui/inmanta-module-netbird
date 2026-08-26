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
import contextlib
import pathlib
import subprocess

import pytest
import pytest_inmanta.plugin
import requests
from conftest import (
    CONTAINER_PREFIX,
    facts,
    get,
    network_subnet,
    peer_network,
    ping,
    run_container,
    underlay_address,
    update_example,
    wait_until,
)
from test_peer import (
    NETBIRD_CLIENT_IMAGE,
    PEER_CAPABILITIES,
    PEER_REGISTRATION_TIMEOUT,
    find_peer,
)

# The two groups the example expresses the whole account in terms of.
GATEWAY_GROUP = "office-gateways"
CLIENT_GROUP = "office-clients"
LAN_GROUP = "office-lan"

# The service user the orchestrator itself authenticates as.
SERVICE_USER = "inmanta"

NETWORK_NAME = "office"
NETWORK_RESOURCE_NAME = "office-lan"
ROUTER_NAME = "office-gateways"
POLICY_NAME = "office"

ZONE_DOMAIN = "office.example.com"
PRINTER_RECORD = f"printer.{ZONE_DOMAIN}"
SCANNER_RECORD = f"scanner.{ZONE_DOMAIN}"

# The hostnames the two clients register under, and the name of the container standing
# in for a host of the office lan that never heard of netbird.
GATEWAY_HOSTNAME = "office-gateway"
CLIENT_HOSTNAME = "office-client"
PRINTER_CONTAINER = f"{CONTAINER_PREFIX}-printer"

# The addresses the readme shows in place of the ones podman handed out: the example is
# about a lan behind a gateway, not about the bridge network this test built one on.
README_SUBNET = "10.10.0.0/24"
README_PRINTER_ADDRESS = "10.10.0.9"

# How long to give the account's changes to reach the peers.  The client polls the
# management server for its configuration, and it sets a connection up on the first
# packet with somewhere to go, so both the route and the name are a moment behind the
# api call that created them.
ROUTE_TIMEOUT = 180.0


@contextlib.contextmanager
def netbird_client(
    netbird: requests.Session,
    setup_key: str,
    hostname: str,
    network: str,
    *,
    routing: bool = False,
) -> collections.abc.Iterator[str]:
    """
    Run a netbird client registering with the given setup key, stop it again afterwards,
    and yield the name of the container running it.

    :param setup_key: The key the client registers with, which is what puts the peer in
        the group the key's auto groups name.
    :param hostname: The hostname the client registers itself under.
    :param network: The bridge network to run the client in.
    :param routing: Whether this client routes for other peers.  A routing peer forwards
        packets that are not for itself, and rootless podman mounts /proc/sys read only,
        so the sysctl is set from the outside rather than by the client.
    """
    container = f"{CONTAINER_PREFIX}-client-{hostname}"
    run_container(
        container,
        [
            # A bridge of its own, which the server joined too: the client reaches the
            # api by name, and nothing else.
            "--network",
            network,
            *PEER_CAPABILITIES,
            *(["--sysctl", "net.ipv4.ip_forward=1"] if routing else []),
            # A uts namespace of its own, which is what makes the hostname settable:
            # podman refuses --hostname in the host uts namespace, and that is the
            # default in the container the ci job runs in.
            "--uts",
            "private",
            "--hostname",
            hostname,
            "-e",
            f"NB_SETUP_KEY={setup_key}",
            "-e",
            f"NB_MANAGEMENT_URL={netbird.peer_management_url}",
            NETBIRD_CLIENT_IMAGE,
        ],
    )
    try:
        wait_until(
            lambda: find_peer(netbird, hostname) is not None,
            container,
            f"the netbird client did not register itself as {hostname}",
            timeout=PEER_REGISTRATION_TIMEOUT,
        )
        yield container
    finally:
        subprocess.run(["podman", "rm", "-f", container], capture_output=True)


@contextlib.contextmanager
def office_host(network: str) -> collections.abc.Iterator[str]:
    """
    Run a container standing in for a host of the office lan, and yield its name.

    It runs no netbird client: it is only reachable through the gateway routing for the
    subnet it sits in, which is the whole point of routing a network.  The netbird image
    is reused as a plain busybox, so that this test pulls nothing extra.
    """
    container = PRINTER_CONTAINER
    run_container(
        container,
        [
            "--network",
            network,
            "--entrypoint",
            "sleep",
            NETBIRD_CLIENT_IMAGE,
            "infinity",
        ],
    )
    try:
        yield container
    finally:
        subprocess.run(["podman", "rm", "-f", container], capture_output=True)


def network_model(
    management_url: str,
    subnet: str,
    printer_address: str,
    *,
    policy_purged: bool = False,
) -> str:
    """
    The model the readme shows, with the values the test needs to deploy it for real
    substituted in: the api it drives, the subnet podman gave the office bridge, and
    the address of the host standing in for the printer on it.

    :param policy_purged: Whether the policy is asked to be gone, which the test uses
        to show that it is the policy the traffic goes through.

    Nothing in it names a netbird id: every object pointing at another one reads the
    ``id`` of the resource managing it, which is a reference on the fact that resource
    publishes and is resolved on the agent, at deploy time.
    """
    purged = "\n            purged=true," if policy_purged else ""
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
        # left null, so the model does not manage the membership — a peer gets there by
        # registering with the key whose auto groups name the group.
        gateways = netbird::Group(
            api=api,
            name="{GATEWAY_GROUP}",
        )
        clients = netbird::Group(
            api=api,
            name="{CLIENT_GROUP}",
        )

        # And the group the office lan itself is in.  The groups of a network resource
        # are what the policies of the account point at, they are not the peers reaching
        # it: a resource is a destination, not a member.
        lan = netbird::Group(
            api=api,
            name="{LAN_GROUP}",
        )

        # One key per role, so that what a peer is follows from the key it joined with.
        # The api generates the key and shows it once: the model never holds the value,
        # it is published as a fact when the key is created.
        netbird::SetupKey(
            api=api,
            name="{GATEWAY_GROUP}",
            type="reusable",
            expires_in=86400,
            auto_groups=[gateways.id],
            # The api refuses an auto group it doesn't know, and a reference is not a
            # dependency of its own.
            requires=gateways,
        )
        netbird::SetupKey(
            api=api,
            name="{CLIENT_GROUP}",
            type="reusable",
            expires_in=86400,
            auto_groups=[clients.id],
            requires=clients,
        )

        # The network holding what the gateways give access to.  A netbird network is
        # nothing but that container: the addresses and the routers are objects of
        # their own, addressed under its id.
        network = netbird::Network(
            api=api,
            name="{NETWORK_NAME}",
            description="The office lan, reached through the gateways",
        )

        # What the network gives access to.  The api derives the type of a resource
        # from its address — a host address, a subnet or a domain — so there is no type
        # to set here.  Its groups are what the policy below points at, they are not the
        # peers reaching it.
        netbird::NetworkResource(
            api=api,
            _network=network.id,
            name="{NETWORK_RESOURCE_NAME}",
            address="{subnet}",
            enabled=true,
            groups=[lan.id],
            # The api refuses a resource in a network that does not exist, and silently
            # drops a group id it does not know.
            requires=[network, lan],
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
            # The hosts of the office lan know nothing of netbird, so the gateway
            # rewrites the source address of what it routes for them.
            masquerade=true,
            enabled=true,
            requires=[network, gateways],
        )

        # Routing the subnet is not the same as being allowed to reach it: a peer
        # reaches the office lan because this policy says the peers of the client group
        # may.  Nothing reaches a network resource without one — the account's own
        # `Default` policy only covers the peers of its `All` group, and a resource is
        # in no group but the ones it was given.
        netbird::Policy(
            api=api,
            name="{POLICY_NAME}",
            enabled=true,{purged}
            rule=netbird::PolicyRule(
                sources=[clients.id],
                destinations=[lan.id],
                # A rule needs a protocol and an action to be created at all, and the
                # api refuses ports on an `all` rule.
                protocol="all",
                action="accept",
                bidirectional=true,
                enabled=true,
            ),
            requires=[clients, lan],
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
            content="{printer_address}",
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
    Deploy a whole routed network on a fresh account and check that it routes: the
    groups it is expressed in terms of, the keys the peers join with, the office subnet
    and the gateways routing towards it, and the dns zone naming the hosts behind it.

    Three containers make that real.  A gateway client and a client sit on bridge
    networks that are isolated from each other, and a third container stands in for a
    printer on the gateway's own bridge, running no netbird client at all.  Before the
    network resource and its router are deployed the client has no way to reach that
    printer; afterwards it reaches it by address, and once the zone is deployed by name.

    Every object here is addressed under the opaque id of another one, which the model
    never writes down: it reads the ``id`` of the resource managing it.  That reference
    resolves from the facts that resource publishes, so the deploy walks down the tree —
    and this test seeds the facts a real orchestrator would have collected, which
    ``std::create_fact_reference`` snapshots at compile time.  Hence one compile per
    level.
    """
    monkeypatch.setenv("NETBIRD_TOKEN", netbird.token)

    # The office lan is the bridge network the gateway runs on: that is the subnet the
    # network resource names, and the printer's address on it is what the record holds.
    office_network = peer_network(0)
    client_network = peer_network(1)
    subnet = network_subnet(office_network)

    with office_host(office_network) as printer:
        printer_address = underlay_address(printer)

        model = network_model(netbird.management_url, subnet, printer_address)
        project.compile(model, no_dedent=False)

        # The service user first: it is what the account is driven with, and it depends
        # on nothing else.  The api stores no email address for a service user.
        project.deploy_resource("netbird::User")
        user = next(u for u in get(netbird, "users") if u["name"] == SERVICE_USER)
        assert user["is_service_user"] is True
        assert user["email"] == ""

        # Then the groups, whose ids everything below points at.
        group_ids = {}
        for name in [GATEWAY_GROUP, CLIENT_GROUP, LAN_GROUP]:
            resource = project.deploy_resource("netbird::Group", name=name)
            group_ids[name] = facts(project)["id"]
            project.add_fact(resource.id.resource_str(), "id", group_ids[name])

        # The keys the two clients join with, which land them in those groups.  The
        # value is only known through the fact the create published.
        project.compile(model, no_dedent=False)
        setup_keys = {}
        for name in [GATEWAY_GROUP, CLIENT_GROUP]:
            project.deploy_resource("netbird::SetupKey", name=name)
            setup_keys[name] = facts(project)["key"]
            assert "*" not in setup_keys[name]

        # The network, which the resource and the router below are addressed under.
        network_resource = project.deploy_resource("netbird::Network")
        network_id = facts(project)["id"]
        project.add_fact(network_resource.id.resource_str(), "id", network_id)

        with (
            netbird_client(
                netbird,
                setup_keys[GATEWAY_GROUP],
                GATEWAY_HOSTNAME,
                office_network,
                routing=True,
            ) as gateway,
            netbird_client(
                netbird,
                setup_keys[CLIENT_GROUP],
                CLIENT_HOSTNAME,
                client_network,
            ) as client,
        ):
            # Both peers joined the group their key names, without the model ever
            # listing a peer: it does not know the ids the api handed out.
            peers = {peer["hostname"]: peer for peer in get(netbird, "peers")}
            for hostname, group in [
                (GATEWAY_HOSTNAME, GATEWAY_GROUP),
                (CLIENT_HOSTNAME, CLIENT_GROUP),
            ]:
                members = next(
                    g for g in get(netbird, "groups") if g["id"] == group_ids[group]
                )
                assert [m["id"] for m in members["peers"]] == [peers[hostname]["id"]]

            # The printer answers on its own bridge, and there is no path to it from
            # the client: the two bridges are isolated, and nothing routes between them
            # yet.  That is what the resources below change.
            assert ping(gateway, printer_address)
            assert not ping(client, printer_address), (
                "the client reaches the printer before anything routes towards it, "
                "the isolation this test rests on is not there"
            )

            # With the network and the groups known, the resource and its router
            # converge, and the gateway starts routing for the office lan.
            project.compile(model, no_dedent=False)

            project.deploy_resource("netbird::NetworkResource")
            resources = get(netbird, f"networks/{network_id}/resources")
            assert [r["name"] for r in resources] == [NETWORK_RESOURCE_NAME]
            assert resources[0]["address"] == subnet
            # The api derives the type from the address, and reports the groups as
            # objects rather than as the ids it took.
            assert resources[0]["type"] == "subnet"
            assert [g["id"] for g in resources[0]["groups"]] == [group_ids[LAN_GROUP]]

            project.deploy_resource("netbird::NetworkRouter")
            routers = get(netbird, f"networks/{network_id}/routers")
            assert len(routers) == 1
            assert routers[0]["peer_groups"] == [group_ids[GATEWAY_GROUP]]
            assert routers[0]["masquerade"] is True
            assert routers[0]["metric"] == 9999

            # And the policy the peers of the client group reach it under.  Without
            # it the route is there and nothing goes through.
            project.deploy_resource("netbird::Policy")
            policy = next(
                p for p in get(netbird, "policies") if p["name"] == POLICY_NAME
            )
            assert policy["enabled"] is True
            (policy_rule,) = policy["rules"]
            assert [g["id"] for g in policy_rule["sources"]] == [
                group_ids[CLIENT_GROUP]
            ]
            assert [g["id"] for g in policy_rule["destinations"]] == [
                group_ids[LAN_GROUP]
            ]

            # And the client reaches the printer, through the gateway: the packets go
            # over the overlay to a peer on the office bridge, which forwards them to a
            # host that never heard of netbird.
            wait_until(
                lambda: ping(client, printer_address),
                client,
                "the client did not reach the printer through the gateway",
                timeout=ROUTE_TIMEOUT,
            )

            # The zone gives that address a name, distributed to the client's group.
            zone_resource = project.deploy_resource("netbird::DnsZone")
            zone = next(
                z for z in get(netbird, "dns/zones") if z["domain"] == ZONE_DOMAIN
            )
            assert zone["enabled"] is True
            assert zone["distribution_groups"] == [group_ids[CLIENT_GROUP]]
            project.add_fact(zone_resource.id.resource_str(), "id", zone["id"])

            # And the records, addressed under the id of the zone that was just
            # created.
            project.compile(model, no_dedent=False)
            for name in [PRINTER_RECORD, SCANNER_RECORD]:
                project.deploy_resource("netbird::DnsZoneRecord", name=name)

            records = {
                record["name"]: record
                for record in get(netbird, f"dns/zones/{zone['id']}/records")
            }
            assert records[PRINTER_RECORD]["type"] == "A"
            assert records[PRINTER_RECORD]["content"] == printer_address
            assert records[PRINTER_RECORD]["ttl"] == 300
            assert records[SCANNER_RECORD]["type"] == "CNAME"
            assert records[SCANNER_RECORD]["content"] == PRINTER_RECORD

            # The client resolves both names with the account's dns and reaches the
            # printer by either: the A record it was given, and the CNAME pointing at
            # it.
            for name in [PRINTER_RECORD, SCANNER_RECORD]:
                wait_until(
                    lambda name=name: ping(client, name),
                    client,
                    f"the client did not reach the printer as {name}",
                    timeout=ROUTE_TIMEOUT,
                )

            # And the policy is what the traffic goes through: purge it and the
            # route is still there, with nothing going over it any more.
            project.compile(
                network_model(
                    netbird.management_url,
                    subnet,
                    printer_address,
                    policy_purged=True,
                ),
                no_dedent=False,
            )
            project.deploy_resource("netbird::Policy")
            assert (
                next(
                    (p for p in get(netbird, "policies") if p["name"] == POLICY_NAME),
                    None,
                )
                is None
            )
            wait_until(
                lambda: not ping(client, printer_address),
                client,
                "the client still reaches the printer without a policy allowing it",
                timeout=ROUTE_TIMEOUT,
            )

    # The readme shows the model the example is about, not the variant that purges the
    # policy to prove a point: compile it once more, so that what is written back is it.
    project.compile(
        network_model(netbird.management_url, subnet, printer_address), no_dedent=False
    )
    tested_model = pathlib.Path(project._test_project_dir, "main.cf").read_text()
    tested_model = tested_model.replace(
        netbird.management_url, "https://api.netbird.io"
    )
    # The readme shows a lan, not the bridge network podman happened to build.
    tested_model = tested_model.replace(subnet, README_SUBNET)
    tested_model = tested_model.replace(printer_address, README_PRINTER_ADDRESS)
    update_example("netbird-network", tested_model)
