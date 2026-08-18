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

And the following resources:
1. `netbird::User`: a user, or a service user, of the netbird account.

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
itself.  The example below runs a netbird client in a container, hands it the key the
module created, and manages the peer that shows up on the account — with
[`inmanta-module-podman`](https://pypi.python.org/pypi/inmanta-module-podman/) running
the container.

Two things tie it together.  The key is a secret the api only ever shows once, so the
model never holds the value: it is published as a fact when the key is created, and only
resolved on the agent, at the moment the environment file is written.  And the client
reports the container's hostname when it registers, which is what `netbird::Peer` finds
it back by.

The key can not be handed to the container through `podman::Container.env`: the quadlet
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

# The client reports this as its hostname when it registers, and the hostname
# is what identifies a peer here: the api offers no way to change it, while the
# name of a peer is one of the values this model rewrites.
hostname = "lab-gateway"

# Everything below runs rootless, as this unprivileged user.  It needs access to
# /dev/net/tun, and `loginctl enable-linger` on it, so that its units keep
# running while it is not logged in.
user = "netbird"

# The token the client registers with.  The api generates it and the model
# never sees the value: it is published as a fact when the key is created.
setup_key = netbird::SetupKey(
    api=api,
    name="lab-gateways",
    type="reusable",
    expires_in=86400,
)

config_dir = files::Directory(
    host=host,
    path="/home/netbird/.config/netbird",
    owner=user,
    create_parents=true,
)

# The key can not go through podman::Container.env: the quadlet file is rendered
# at compile time, where the key is still a reference and not a string.  It goes
# through an environment file instead, whose content stays a reference until the
# agent writes it on the host.
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
    # A fact reference is not a dependency: the key has to exist, and its fact
    # to be published, before the agent can resolve it here.
    requires=setup_key,
)

# The netbird client itself.  NET_ADMIN and /dev/net/tun are what it takes to
# set up the wireguard interface.
client = podman::Container(
    host=host,
    owner=user,
    name="netbird",
    hostname=hostname,
    image="docker.io/netbirdio/netbird:latest",
    env_file=env_file.path,
    add_capability=["NET_ADMIN"],
    add_device=["/dev/net/tun"],
    requires=env_file,
)

# podman::Container is not a resource of its own: it is rendered into a quadlet
# unit, and that unit file is what gets deployed.
service = podman::services::SystemdContainer(
    container=client,
    state="running",
    enabled=true,
    quadlet=true,
    systemd_unit_dir="/home/netbird/.config/systemd/user",
    systemd_container_dir="/home/netbird/.config/containers/systemd",
    systemctl_command=["systemctl", "--user"],
)

# And the peer the client registered.  This resource adopts a peer rather than
# creating one, so it only deploys once the client has joined the account:
# before that it skips, saying so.
netbird::Peer(
    api=api,
    hostname=hostname,
    ssh_enabled=false,
    requires=service.resources,
)

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
test sees a fresh, empty account.  They are skipped when podman is not available.
