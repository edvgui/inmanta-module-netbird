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
2. `netbird::Group`: a group of peers, which the policies, setup keys, users and dns
   objects of the account point at.
3. `netbird::SetupKey`: the token a peer registers itself with.
4. `netbird::Peer`: a peer that has joined the account.  A peer can not be created
   through the api, it registers itself: this resource adopts one that joined.
5. `netbird::Network`: a network of the account.
6. `netbird::NetworkResource`: an address, a subnet or a domain a network gives access
   to.
7. `netbird::NetworkRouter`: the peer, or the peers of the groups, routing the traffic
   towards the resources of a network.
8. `netbird::NameserverGroup`: a set of dns servers, and the peers that resolve their
   queries with them.
9. `netbird::DnsSettings`: the account-level dns settings.  They are part of the
   account, so this resource only ever updates them.
10. `netbird::DnsZone`: a dns zone the account serves.
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
    api_token=std::get_env("NETBIRD_TOKEN"),
)

netbird::User(
    api=api,
    name="Alice",
    email="alice@example.com",
    role="admin",
)
```

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
