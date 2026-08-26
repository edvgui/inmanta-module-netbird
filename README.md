# inmanta-module-netbird

[![pypi version](https://img.shields.io/pypi/v/inmanta-module-netbird.svg)](https://pypi.python.org/pypi/inmanta-module-netbird/)
[![build status](https://img.shields.io/github/actions/workflow/status/edvgui/inmanta-module-netbird/continuous-integration.yml)](https://github.com/edvgui/inmanta-module-netbird/actions)

This package is an adapter that is meant to be used with the inmanta orchestrator: https://docs.inmanta.com

## Features

This module allows to manage [netbird](https://netbird.io) resources, through the netbird management api.

The module is a work in progress, it currently contains the base entities every
resource of this module builds upon:
1. `netbird::Api`: the endpoint and credentials used to reach the netbird management api.
2. `netbird::ResourceABC`: the base entity for all the resources managed by this module.
3. `netbird::JsonObjectABC`: the base entity for every object of the netbird api.

And the following resources, one per object of the api:
1. `netbird::User`: a user, or a service user, of the account.
2. `netbird::Group`: a group of peers, which every access rule of the account is
   expressed in terms of.
3. `netbird::SetupKey`: the token a peer registers itself with.  The api generates the
   key and shows it once, so the model never holds it: it is published as a fact.
4. `netbird::Peer`: a peer that joined the account.  It can not be created through the
   api — a peer registers itself — so this resource adopts one and manages what the api
   lets it change.
5. `netbird::Network`: a network of the account.
6. `netbird::NetworkResource`: an address, a subnet or a domain a network gives access
   to.
7. `netbird::NetworkRouter`: the peer, or the peers of the groups, routing towards them.
8. `netbird::NameserverGroup`: a set of dns servers and the peer groups resolving with
   them.  Its servers are `netbird::Nameserver` entities embedded in it, not a resource
   of their own.
9. `netbird::DnsSettings`: the dns settings of the account.  They are a singleton the
   api creates with the account and has no endpoint to delete, so this resource only
   ever updates them.
10. `netbird::DnsZone`: a dns zone the account serves, resolved by the peers it is
    distributed to.
11. `netbird::DnsZoneRecord`: a record held by one of those zones.

Every netbird object is co-managed with whoever else edits the account: an attribute
left `null` in the model keeps the value the api currently holds, only the values the
model sets are enforced.  The api addresses its objects by opaque ids, and so does
this module: `netbird::ResourceABC.id` is a reference resolving the id of an object
from the facts its resource publishes, to be fed to whatever other resource points at
it.

## Example

```
import netbird

api = netbird::Api(
    agent_name="netbird",
    management_url="https://api.netbird.io",
    api_token=std::create_environment_reference("NETBIRD_TOKEN"),
)

netbird::User(
    api=api,
    name="Alice",
    email="alice@example.com",
    role="admin",
)
```

### Registering a peer

A peer can not be created through the api: it comes into existence by registering
itself.  The example below runs two netbird clients in containers, hands them the key
the module created, and manages the peers that show up on the account — with
[`inmanta-module-podman`](https://pypi.python.org/pypi/inmanta-module-podman/) running
the containers.

Two gateways rather than one, because a single peer has nobody to talk to: they join the
same account with the same reusable key, and reach each other over the overlay wherever
they sit on the underlay.  The test running this example checks exactly that, with each
client on a network of its own from which there is no path to the other.

The account they join is described by the same model: a group the key drops every peer
registering with it into, a nameserver group resolving the lab's own domain for the peers
of that group, and the account's dns settings.  None of it names an id — the key's
`auto_groups` and the nameserver group's `groups` read `netbird::Group.id`, the reference
on the fact the group's own resource publishes, and the value is resolved on the agent.
The group's `peers` are left null on purpose: the peers get there by registering, and
what the model does not set, it does not manage.

Two things tie it together.  The key is a secret the api only ever shows once, so the
model never holds the value: it is published as a fact when the key is created, and only
resolved on the agent, at the moment the environment file is written.  And each client
reports its container's hostname when it registers, which is what `netbird::Peer` finds
it back by.

The key can not be handed to the containers through `podman::Container.env`: the quadlet
file is rendered at compile time, where the key is still a reference rather than a
string.  It goes through an environment file, and the reference is created inside the
template — `files::jinja` declares its arguments as `object`, and the dsl refuses to
pass a reference there, so `netbird::SetupKey._key` can not be forwarded from the model.
The template reaches the same fact through the same `std::create_fact_reference` that
`_key` is built from.

The project provides the template the environment file is rendered from:

<x-example-netbird-client-template>

```
NB_SETUP_KEY={{ setup_key | std.create_fact_reference("key") }}
NB_MANAGEMENT_URL={{ management_url }}
```

</x-example-netbird-client-template>

<x-example-netbird-client>

```
import files
import mitogen
import netbird
import podman
import podman::services
import std

api = netbird::Api(
    agent_name="netbird",
    management_url="https://api.netbird.io",
    # A reference, not std::get_env: the token stays out of the desired state
    # and is resolved on the agent, at deploy time.
    api_token=std::create_environment_reference("NETBIRD_TOKEN"),
)

host = std::Host(
    name="localhost",
    os=std::linux,
    via=mitogen::Local(),
)

# Each client reports its own hostname when it registers, and the hostname is
# what identifies a peer here: the api offers no way to change it, while the
# name of a peer is one of the values this model rewrites.  Two gateways, so
# that there is an overlay to speak of: once both have joined the account they
# reach each other over it, wherever they sit on the underlay.
hostnames = ["lab-gateway-a", "lab-gateway-b"]

# Everything below runs rootless, as this unprivileged user.  It needs access to
# /dev/net/tun, and `loginctl enable-linger` on it, so that its units keep
# running while it is not logged in.
user = "netbird"

# The group the two gateways end up in.  Its `peers` are left null, so the
# model does not manage them: the clients join by registering with the key
# below, which is what the key's auto groups do.
gateways = netbird::Group(
    api=api,
    name="lab-gateways",
)

# The token both clients register with.  The api generates it and the model
# never sees the value: it is published as a fact when the key is created.  A
# reusable key, since more than one client joins with it.
setup_key = netbird::SetupKey(
    api=api,
    name="lab-gateways",
    type="reusable",
    expires_in=86400,
    # The id of a group the model never reads either: it is a reference on the
    # fact the group's own resource publishes.  Every peer registering with
    # this key lands in that group.
    auto_groups=[gateways.id],
    # The api refuses an auto group it doesn't know, so the group has to be
    # there first.  A reference is not a dependency of its own.
    requires=gateways,
)

# The dns the gateways resolve the lab's own domain with.  The api wants one to
# three servers, at least one group to distribute them to, and either the
# primary flag or a domain — never both, never neither.
netbird::NameserverGroup(
    api=api,
    name="lab-dns",
    enabled=true,
    groups=[gateways.id],
    domains=["lab.example.com"],
    nameservers=[
        netbird::Nameserver(ip="9.9.9.9", ns_type="udp", port=53),
        netbird::Nameserver(ip="1.1.1.1", ns_type="udp", port=53),
    ],
    requires=gateways,
)

# And netbird resolves for every peer of the account: no group opts out of it.
# These settings are a singleton the api creates with the account, so this
# resource only ever updates them — purging it is an error.
netbird::DnsSettings(
    api=api,
    disabled_management_groups=[],
)

for hostname in hostnames:
    # One configuration directory per client: they share the account and the
    # key, nothing else.
    config_dir = files::Directory(
        host=host,
        path=f"/home/netbird/.config/netbird/{hostname}",
        owner=user,
        create_parents=true,
    )

    # The key can not go through podman::Container.env: the quadlet file is
    # rendered at compile time, where the key is still a reference and not a
    # string.  It goes through an environment file instead, whose content stays
    # a reference until the agent writes it on the host.
    env_file = files::TextFile(
        host=host,
        # No need to require the directory, the files exporter wires that up.
        path=f"{config_dir.path}/client.env",
        content=files::jinja(
            "template:///netbird-client.env.j2",
            setup_key=setup_key,
            management_url=api.management_url,
        ),
        owner=user,
        # The key is a secret: only its owner gets to read it.
        permissions=600,
        # A fact reference is not a dependency: the key has to exist, and its
        # fact to be published, before the agent can resolve it here.
        requires=setup_key,
    )

    # The netbird client itself.  NET_ADMIN, NET_RAW and /dev/net/tun are what
    # it takes to bring the wireguard interface up.
    client = podman::Container(
        host=host,
        owner=user,
        name=hostname,
        hostname=hostname,
        image="docker.io/netbirdio/netbird:latest",
        env_file=env_file.path,
        add_capability=["NET_ADMIN", "NET_RAW"],
        add_device=["/dev/net/tun"],
        requires=env_file,
    )

    # podman::Container is not a resource of its own: it is rendered into a
    # quadlet unit, and that unit file is what gets deployed.
    service = podman::services::SystemdContainer(
        container=client,
        state="running",
        enabled=true,
        quadlet=true,
        systemd_unit_dir="/home/netbird/.config/systemd/user",
        systemd_container_dir="/home/netbird/.config/containers/systemd",
        systemctl_command=["systemctl", "--user"],
    )

    # And the peer the client registered.  This resource adopts a peer rather
    # than creating one, so it only deploys once the client has joined the
    # account: before that it skips, saying so.
    netbird::Peer(
        api=api,
        hostname=hostname,
        ssh_enabled=false,
        requires=service.resources,
    )
end

```

</x-example-netbird-client>

Find more examples in the `tests` folder of this module!

## Development

```sh
python3 -m venv .venv
source .venv/bin/activate
make install
pytest tests
```

The tests deploy a netbird server locally, in a podman container of their own, and
run against its api.  The container is started again for each test, so that every
test sees a fresh, empty account.  Every peer the tests register runs in a bridge
network of its own, which the server container joins and which is isolated from the
others, so peers only ever reach each other over the netbird overlay.  They are skipped
when podman is not available.
