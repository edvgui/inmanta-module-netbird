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

import copy
import ipaddress
import typing
from dataclasses import asdict

import pydantic
import requests
from inmanta_plugins.files.json import Operation, serialize_for_resource, update

import inmanta.agent.handler
import inmanta.execute.proxy
import inmanta.export
import inmanta.plugins
import inmanta.resources
from inmanta.util import dict_path
from inmanta_plugins.netbird.helpers import Session


class ResourceABC(
    inmanta.resources.PurgeableResource, inmanta.resources.ManagedResource
):
    """
    Base exporter class for all netbird resources.
    """

    # inmanta infers the type of the parent's fields from its own literal, which
    # makes any tuple of another length look like an invalid override
    fields: tuple[str, ...] = (  # type: ignore[assignment]
        "management_url",
        "api_token",
        "desired_state",
    )
    management_url: str
    api_token: str

    # The json object the model wants, serialized from the entity itself: every
    # netbird object is a json object, and the resource is its own desired state.
    desired_state: list[dict]

    # The object as the api holds it, narrowed down to the keys that take part in the
    # diff.  Filled in by the handler, it is not exported.
    body: dict = {}

    @classmethod
    def get_desired_state(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> list[dict]:
        return [asdict(s) for s in serialize_for_resource(entity.root, entity)]

    @classmethod
    def get_management_url(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity.api.management_url

    @classmethod
    def get_api_token(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity.api.api_token


T = typing.TypeVar("T")


def process_netbird_response(
    response: requests.Response,
    *,
    expected_type: type[T] = object,  # type: ignore[assignment]
) -> T:
    """
    Helper method to process a response from the netbird api, and easily render
    the errors we received, if any.

    :param response: The response object requests built from the api response.
    :param expected_type: The type of the result we expect to get from the api.
    """
    if not response.ok:
        try:
            error = response.json()
        except requests.JSONDecodeError:
            # The error is not valid json, let requests build the error for us
            response.raise_for_status()
            raise

        match error:
            case {"message": str() as message, "code": int() as code}:
                raise RuntimeError(f"The api returned an error ({code}): {message}")
            case _:
                raise RuntimeError(f"The api returned an error: {error}")

    if not response.content:
        # Some endpoints, like the delete ones, answer with an empty body
        return typing.cast(T, None)

    try:
        body = response.json()
    except requests.JSONDecodeError as e:
        raise RuntimeError(f"Unexpected response from the api: {response.text}") from e

    if body is None and typing.get_origin(expected_type) is list:
        # A listing endpoint with nothing to list answers with a json null instead of
        # an empty list
        return typing.cast(T, [])

    adapter = pydantic.TypeAdapter(expected_type)
    try:
        return adapter.validate_python(body)
    except pydantic.ValidationError as e:
        raise RuntimeError(
            f"Unexpected response from api, expected {expected_type} but got {body}"
        ) from e


def find_user(session: Session, name: str) -> dict | None:
    """
    Find a user of the account by its name, and return None if it doesn't exist.  The
    listing is not filtered on the service_user query parameter, so it holds both the
    regular and the service users of the account.

    The name is what identifies a user here: the api doesn't keep the email address
    of a service user, so it is the only thing every user is guaranteed to have.
    """
    users = process_netbird_response(
        session.get(url="users"),
        expected_type=list[dict],
    )
    return next((u for u in users if u["name"] == name), None)


def find_group(session: Session, name: str) -> dict | None:
    """
    Find a group of the account by its name, and return None if it doesn't exist.
    The api rejects a second group with a name it already holds, so the name
    identifies one group at most.
    """
    groups = process_netbird_response(
        session.get(url="groups"),
        expected_type=list[dict],
    )
    return next((g for g in groups if g["name"] == name), None)


NR = typing.TypeVar("NR", bound=ResourceABC)


class HandlerABC(inmanta.agent.handler.CRUDHandler[NR]):
    """
    Base handler containing the basic logic to interact with the netbird api.
    """

    # Set up in pre, before any of the crud methods runs
    session: Session

    @inmanta.agent.handler.cache(
        timeout=60,
        call_on_delete=lambda s: s.close(),
    )
    def get_session(self, management_url: str, api_token: str) -> Session:
        """
        Setup a session towards a netbird management server api.
        """
        session = Session(
            base_url=management_url.rstrip("/") + "/api/",
            timeout=30,
            secrets={api_token: "******"},
            cache=True,
        )
        session.headers["Authorization"] = "Token " + api_token

        return session

    def pre(self, ctx: inmanta.agent.handler.HandlerContext, resource: NR) -> None:
        # Setup the session object that can be used to access the netbird api
        self.session = self.get_session(resource.management_url, resource.api_token)
        self.session.logger = ctx

    def publish_ids(
        self, ctx: inmanta.agent.handler.HandlerContext, **ids: str
    ) -> None:
        """
        Publish the identifiers the handler resolved on the api as facts.  The api
        addresses every object by an opaque id, which the model never knows: exposing
        them makes it possible to point at an object from outside the model without
        going digging through the api.

        :param ids: The identifiers to publish, by fact name.
        """
        for name, value in ids.items():
            ctx.set_fact(name, value, expires=False)

    def normalize(self, body: dict) -> dict:
        """
        Put a body in a canonical shape, so that the current and the desired state
        can be compared even when the api returns a collection in another order.
        """
        return body

    def merged_body(self, current: dict, resource: NR) -> dict:
        """
        The object as it should be once the desired state is applied onto the state
        the api currently holds.  Everything the model leaves null keeps the value
        the account has for it, and the values the api computes on its own are
        carried along rather than dropped, so that the body the handler writes back
        stays complete even where the model has no opinion.

        On a create there is nothing to merge onto, and this is the desired state on
        its own.

        :param current: The object as the api currently holds it.
        :param resource: The resource being deployed, carrying the desired state.
        """
        return self.normalize(apply_desired_state(current, resource.desired_state))

    def diff_body(self, body: dict) -> dict:
        """
        The part of a body that takes part in the diff.  The api of an object that
        only accepts some of its values on an update narrows this down: a difference
        the handler has no way to enforce would otherwise show up as a change on
        every single deploy, and never converge.

        :param body: A full object, as the api holds it or as the model wants it.
        """
        return body

    def calculate_diff(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        current: NR,
        desired: NR,
    ) -> dict[str, dict[str, object]]:
        desired.body = self.merged_body(current.body, desired)

        changes = super().calculate_diff(ctx, current, desired)
        current_body = self.diff_body(current.body)
        desired_body = self.diff_body(desired.body)
        if desired_body != current_body:
            changes["body"] = {"current": current_body, "desired": desired_body}

        return changes


def apply_desired_state(current: dict, desired_state: list[dict]) -> dict:
    """
    Apply the serialized entities describing the desired state onto the state the api
    currently holds.  Anything the model doesn't set is left as it is, which is what
    lets this module co-manage an object with whoever else edits it in the dashboard.

    :param current: The object as the api currently holds it.
    :param desired_state: The serialized object describing the desired state.
    """
    desired = copy.deepcopy(current)
    for item in desired_state:
        desired = update(
            desired,
            dict_path.to_path(item["path"]),
            Operation(item["operation"]),
            item["value"],
        )

    return desired


def select(body: dict, keys: typing.Sequence[str]) -> dict:
    """
    Narrow a body down to the keys one endpoint of the api takes.  The api doesn't
    accept the same values on a create as on an update, and rejects the ones that
    don't belong to the call being made.

    :param body: The object the handler wants to write.
    :param keys: The keys the endpoint takes.
    """
    return {key: body[key] for key in keys if key in body}


# The keys of the user object the api takes when a user is created, and the ones it
# takes when an existing user is updated.  They don't overlap fully: the identity of
# a user is fixed at creation, and a user can only be blocked afterwards.
USER_CREATE_KEYS = ("email", "name", "role", "auto_groups", "is_service_user")
USER_UPDATE_KEYS = ("role", "auto_groups", "is_blocked")


@inmanta.resources.resource("netbird::User", "name", "api.agent_name")
class UserResource(ResourceABC):
    fields = ("name",)
    name: str


@inmanta.agent.handler.provider("netbird::User", "")
class UserHandler(HandlerABC[UserResource]):
    def diff_body(self, body: dict) -> dict:
        # The api only takes USER_UPDATE_KEYS on an update, so the values it only
        # accepts at creation are kept out of the diff: the handler could not enforce
        # a change to them anyway.
        return select(body, USER_UPDATE_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the auto groups
        if body.get("auto_groups") is not None:
            body["auto_groups"] = sorted(body["auto_groups"])

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: UserResource,
    ) -> None:
        user = find_user(self.session, resource.name)
        if user is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", user["id"])
        self.publish_ids(ctx, id=user["id"])

        resource.body = self.normalize(user)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: UserResource,
    ) -> None:
        desired = self.merged_body({}, resource)
        user = process_netbird_response(
            self.session.post(url="users", json=select(desired, USER_CREATE_KEYS)),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=user["id"])
        ctx.set_created()

        if desired.get("is_blocked"):
            # The api doesn't take the blocked flag when a user is created, so a user
            # the model wants blocked from the start needs a second call.  It is
            # built from the user the api just made, as an update has to carry the
            # values the model has no opinion about too.
            process_netbird_response(
                self.session.put(
                    url=f"users/{user['id']}",
                    json=select(self.merged_body(user, resource), USER_UPDATE_KEYS),
                ),
            )

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: UserResource,
    ) -> None:
        # The body calculate_diff merged is the object as the api holds it, with the
        # desired state applied on top.  The api only takes part of it on an update,
        # and rejects everything else.
        process_netbird_response(
            self.session.put(
                url=f"users/{ctx.get('ID')}",
                json=select(resource.body, USER_UPDATE_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: UserResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"users/{ctx.get('ID')}"),
        )
        ctx.set_purged()


# The keys of the group object the api takes.  The create and the update endpoints
# take the same ones here, and both replace the whole object: a key left out of a PUT
# is emptied, which is why the merged body carries the values the model has no opinion
# about (the network resources of the group) along.  Everything else the api reports
# it computes on its own.
GROUP_KEYS = ("name", "peers", "resources")


@inmanta.resources.resource("netbird::Group", "name", "api.agent_name")
class GroupResource(ResourceABC):
    fields = ("name",)
    name: str


@inmanta.agent.handler.provider("netbird::Group", "")
class GroupHandler(HandlerABC[GroupResource]):
    def diff_body(self, body: dict) -> dict:
        # Only the keys the api takes on a write take part in the diff: the id, the
        # issuer and the counts it reports are computed, and a difference on one of
        # them is not something the handler could enforce.
        return select(body, GROUP_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api reports each member of the group as an object, but only takes the
        # peer ids on a write, and rejects the shape it returned itself.  The
        # canonical shape is therefore the one the write takes.  An empty collection
        # comes back as a json null rather than an empty list, on both collections.
        body["peers"] = sorted(
            peer["id"] if isinstance(peer, dict) else peer
            for peer in body.get("peers") or []
        )
        body["resources"] = body.get("resources") or []

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: GroupResource,
    ) -> None:
        group = find_group(self.session, resource.name)
        if group is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", group["id"])
        self.publish_ids(ctx, id=group["id"])

        resource.body = self.normalize(group)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: GroupResource,
    ) -> None:
        group = process_netbird_response(
            self.session.post(
                url="groups",
                json=select(self.merged_body({}, resource), GROUP_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=group["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: GroupResource,
    ) -> None:
        # The body calculate_diff merged is the object as the api holds it, with the
        # desired state applied on top.  The api replaces the whole group with what
        # this call carries, so it has to be the complete one.
        process_netbird_response(
            self.session.put(
                url=f"groups/{ctx.get('ID')}",
                json=select(resource.body, GROUP_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: GroupResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"groups/{ctx.get('ID')}"),
        )
        ctx.set_purged()


def find_setup_key(session: Session, name: str) -> dict | None:
    """
    Find a setup key of the account by its name, and return None if it doesn't exist.

    The api allows several keys to carry the same name and only tells them apart by
    their id, which the model never knows: this module uses the name as the identity
    of a key, and manages the first one the listing reports.
    """
    setup_keys = process_netbird_response(
        session.get(url="setup-keys"),
        expected_type=list[dict],
    )
    return next((k for k in setup_keys if k["name"] == name), None)


# The keys of the setup key object the api takes when a key is created, and the ones
# it takes when an existing key is updated.  Nearly everything about a key is fixed
# at creation: only its revocation and its auto groups can be changed afterwards.
SETUP_KEY_CREATE_KEYS = (
    "name",
    "type",
    "expires_in",
    "revoked",
    "auto_groups",
    "usage_limit",
    "ephemeral",
    "allow_extra_dns_labels",
)
SETUP_KEY_UPDATE_KEYS = ("revoked", "auto_groups")


@inmanta.resources.resource("netbird::SetupKey", "name", "api.agent_name")
class SetupKeyResource(ResourceABC):
    fields = ("name",)
    name: str


@inmanta.agent.handler.provider("netbird::SetupKey", "")
class SetupKeyHandler(HandlerABC[SetupKeyResource]):
    def diff_body(self, body: dict) -> dict:
        # The api only takes SETUP_KEY_UPDATE_KEYS on an update, so the values it only
        # accepts at creation are kept out of the diff: the handler could not enforce
        # a change to them anyway.  That also covers expires_in, which the api takes
        # as a duration but reports back as the expires timestamp, and would show up
        # as a change on every deploy if it took part in the diff.
        return select(body, SETUP_KEY_UPDATE_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the auto groups
        if body.get("auto_groups") is not None:
            body["auto_groups"] = sorted(body["auto_groups"])

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: SetupKeyResource,
    ) -> None:
        setup_key = find_setup_key(self.session, resource.name)
        if setup_key is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", setup_key["id"])
        self.publish_ids(ctx, id=setup_key["id"])

        resource.body = self.normalize(setup_key)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: SetupKeyResource,
    ) -> None:
        desired = self.merged_body({}, resource)
        setup_key = process_netbird_response(
            self.session.post(
                url="setup-keys",
                json=select(desired, SETUP_KEY_CREATE_KEYS),
            ),
            expected_type=dict,
        )
        # This response is the only place the api ever shows the generated key in
        # clear, it is masked everywhere else.  It is a secret, and this module only
        # ever publishes ids: whoever needs the key reads it from the api itself.
        self.publish_ids(ctx, id=setup_key["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: SetupKeyResource,
    ) -> None:
        # The body calculate_diff merged is the object as the api holds it, with the
        # desired state applied on top.  The api only takes part of it on an update,
        # and requires both of the values it does take on every call.
        process_netbird_response(
            self.session.put(
                url=f"setup-keys/{ctx.get('ID')}",
                json=select(resource.body, SETUP_KEY_UPDATE_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: SetupKeyResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"setup-keys/{ctx.get('ID')}"),
        )
        ctx.set_purged()


def find_peer(session: Session, hostname: str) -> dict | None:
    """
    Find a peer of the account by its hostname, and return None when no peer with that
    hostname joined the account.

    The hostname is what identifies a peer here: its name is settable, and a peer this
    module renames could not be found back by it.

    :param session: The session towards the api of the account.
    :param hostname: The hostname the peer reported when it registered.
    """
    peers = process_netbird_response(
        session.get(url="peers"),
        expected_type=list[dict],
    )
    return next((p for p in peers if p["hostname"] == hostname), None)


# The keys of the peer object the api takes when a peer is updated.  There is no
# create: a peer joins the account on its own.  The update is a full replacement of
# these keys and of nothing else, so every one of them has to be sent on every call.
PEER_UPDATE_KEYS = (
    "name",
    "ssh_enabled",
    "login_expiration_enabled",
    "inactivity_expiration_enabled",
)


@inmanta.resources.resource("netbird::Peer", "hostname", "api.agent_name")
class PeerResource(ResourceABC):
    fields = ("hostname",)
    hostname: str


@inmanta.agent.handler.provider("netbird::Peer", "")
class PeerHandler(HandlerABC[PeerResource]):
    def diff_body(self, body: dict) -> dict:
        # The api only takes PEER_UPDATE_KEYS on an update, and everything else it
        # reports about a peer is either the peer's own doing or managed from the
        # other end of the relation.  Comparing those would report a change on every
        # deploy that the update could never enforce.
        return select(body, PEER_UPDATE_KEYS)

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: PeerResource,
    ) -> None:
        peer = find_peer(self.session, resource.hostname)
        if peer is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", peer["id"])
        self.publish_ids(ctx, id=peer["id"])

        resource.body = self.normalize(peer)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: PeerResource,
    ) -> None:
        # A peer can only join the account by registering itself with a setup key or
        # an sso login, the api has no endpoint to create one.  Doing nothing here
        # would report a deploy that made the desired state true while it did not, so
        # the handler skips instead, and says why.
        raise inmanta.agent.handler.SkipResource(
            f"No peer with hostname {resource.hostname} has joined this netbird "
            "account.  A peer can not be created through the api: it has to register "
            "itself with a setup key or an sso login first, and this resource then "
            "adopts it."
        )

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: PeerResource,
    ) -> None:
        # The update replaces every key it takes, and reads a key the body leaves out
        # as its zero value: leaving one out doesn't keep it, it resets it to false.
        # The body calculate_diff merged holds the values the model has no opinion
        # about, which is what keeps them from being reset here.
        process_netbird_response(
            self.session.put(
                url=f"peers/{ctx.get('ID')}",
                json=select(resource.body, PEER_UPDATE_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: PeerResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"peers/{ctx.get('ID')}"),
        )
        ctx.set_purged()


# The keys of the network object the api takes, on a create as well as on an update.
# Everything else it reports about a network (the routers, the resources and the
# policies it holds) is computed from the objects that point at it, and can not be set
# here.
NETWORK_KEYS = ("name", "description")


@inmanta.resources.resource("netbird::Network", "name", "api.agent_name")
class Network(ResourceABC):
    fields = ("name",)
    name: str


@inmanta.agent.handler.provider("netbird::Network", "")
class NetworkHandler(HandlerABC[Network]):
    def diff_body(self, body: dict) -> dict:
        # The lists of routers, resources and policies of a network are computed by
        # the api from the objects that point at it: they would show up as a change on
        # every deploy that no call could ever enforce.
        return select(body, NETWORK_KEYS)

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: Network,
    ) -> None:
        networks = process_netbird_response(
            self.session.get(url="networks"),
            expected_type=list[dict],
        )
        network = next((n for n in networks if n["name"] == resource.name), None)
        if network is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", network["id"])
        self.publish_ids(ctx, id=network["id"])

        resource.body = self.normalize(network)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: Network,
    ) -> None:
        network = process_netbird_response(
            self.session.post(
                url="networks",
                json=select(self.merged_body({}, resource), NETWORK_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=network["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: Network,
    ) -> None:
        process_netbird_response(
            self.session.put(
                url=f"networks/{ctx.get('ID')}",
                json=select(resource.body, NETWORK_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: Network,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"networks/{ctx.get('ID')}"),
        )
        ctx.set_purged()


# The keys of a network resource the api takes, on a create as well as on an update.
# The type of the resource is not one of them: the api derives it from the address.
NETWORK_RESOURCE_KEYS = ("name", "description", "address", "enabled", "groups")


@inmanta.resources.resource("netbird::NetworkResource", "name", "api.agent_name")
class NetworkResource(ResourceABC):
    # The network a resource belongs to is not part of the object the api holds, it is
    # the url the object is addressed under.  The model only knows it as a reference
    # to the id its network publishes, which is resolved here, on the agent.
    fields = ("name", "network")
    name: str
    network: str

    @classmethod
    def get_network(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity._network


@inmanta.agent.handler.provider("netbird::NetworkResource", "")
class NetworkResourceHandler(HandlerABC[NetworkResource]):
    def diff_body(self, body: dict) -> dict:
        # The type the api derived from the address is not something the model can
        # set, and the id of the object doesn't take part in the diff either.
        return select(body, NETWORK_RESOURCE_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api stores a host address as the network it is alone in, and reports it
        # that way: `1.1.1.1` comes back as `1.1.1.1/32`, which is the same address.
        address = body.get("address")
        if isinstance(address, str) and "/" not in address:
            try:
                prefix_length = ipaddress.ip_address(address).max_prefixlen
            except ValueError:
                # A domain, which the api keeps as it is
                pass
            else:
                body["address"] = f"{address}/{prefix_length}"

        # The api reports the groups of a resource as objects while it takes them as
        # ids, it doesn't preserve their order, and it reports a resource that is in no
        # group at all with no list rather than an empty one.
        if "groups" in body:
            body["groups"] = sorted(
                group["id"] if isinstance(group, dict) else group
                for group in body["groups"] or []
            )

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkResource,
    ) -> None:
        resources = process_netbird_response(
            self.session.get(url=f"networks/{resource.network}/resources"),
            expected_type=list[dict],
        )
        current = next((r for r in resources if r["name"] == resource.name), None)
        if current is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", current["id"])
        self.publish_ids(ctx, id=current["id"])

        resource.body = self.normalize(current)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkResource,
    ) -> None:
        current = process_netbird_response(
            self.session.post(
                url=f"networks/{resource.network}/resources",
                json=select(self.merged_body({}, resource), NETWORK_RESOURCE_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=current["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: NetworkResource,
    ) -> None:
        process_netbird_response(
            self.session.put(
                url=f"networks/{resource.network}/resources/{ctx.get('ID')}",
                json=select(resource.body, NETWORK_RESOURCE_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(
                url=f"networks/{resource.network}/resources/{ctx.get('ID')}"
            ),
        )
        ctx.set_purged()


# The keys of a network router the api takes, on a create as well as on an update.
NETWORK_ROUTER_KEYS = ("peer", "peer_groups", "metric", "masquerade", "enabled")


def routing_target(body: dict) -> tuple[str | None, list[str]]:
    """
    The peer, or the peer groups, a router routes for.  The api gives a router no name
    of its own, this is the only thing it is known by.

    :param body: A router, as the api holds it or as the model wants it.
    """
    return body.get("peer") or None, sorted(body.get("peer_groups") or [])


@inmanta.resources.resource("netbird::NetworkRouter", "name", "api.agent_name")
class NetworkRouter(ResourceABC):
    # The api knows a router by the target it routes for, which the model only knows
    # as references resolved on the agent.  The name is what identifies the resource
    # inmanta deploys, and the network is the url the router is addressed under.
    fields = ("name", "network")
    name: str
    network: str

    @classmethod
    def get_name(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity._name

    @classmethod
    def get_network(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity._network


@inmanta.agent.handler.provider("netbird::NetworkRouter", "")
class NetworkRouterHandler(HandlerABC[NetworkRouter]):
    def diff_body(self, body: dict) -> dict:
        return select(body, NETWORK_ROUTER_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the peer groups
        if body.get("peer_groups") is not None:
            body["peer_groups"] = sorted(body["peer_groups"])

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkRouter,
    ) -> None:
        routers = process_netbird_response(
            self.session.get(url=f"networks/{resource.network}/routers"),
            expected_type=list[dict],
        )
        # A router is only known by what it routes for, so that is what the desired
        # state is matched on: a router that should route for another target is a new
        # one, not an update of this one.
        target = routing_target(self.merged_body({}, resource))
        router = next((r for r in routers if routing_target(r) == target), None)
        if router is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", router["id"])
        self.publish_ids(ctx, id=router["id"])

        resource.body = self.normalize(router)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkRouter,
    ) -> None:
        router = process_netbird_response(
            self.session.post(
                url=f"networks/{resource.network}/routers",
                json=select(self.merged_body({}, resource), NETWORK_ROUTER_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=router["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: NetworkRouter,
    ) -> None:
        process_netbird_response(
            self.session.put(
                url=f"networks/{resource.network}/routers/{ctx.get('ID')}",
                json=select(resource.body, NETWORK_ROUTER_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NetworkRouter,
    ) -> None:
        process_netbird_response(
            self.session.delete(
                url=f"networks/{resource.network}/routers/{ctx.get('ID')}"
            ),
        )
        ctx.set_purged()


def find_nameserver_group(session: Session, name: str) -> dict | None:
    """
    Find a nameserver group of the account by its name, and return None if it doesn't
    exist.  The api has no endpoint to look a group up by anything but its id, which
    the model doesn't know, so the listing is filtered here.
    """
    groups = process_netbird_response(
        session.get(url="dns/nameservers"),
        expected_type=list[dict],
    )
    return next((g for g in groups if g["name"] == name), None)


# The keys of the nameserver group object the api takes.  The create and the update
# endpoint take the same ones, and both require the full object.
NAMESERVER_GROUP_KEYS = (
    "name",
    "description",
    "nameservers",
    "enabled",
    "groups",
    "primary",
    "domains",
    "search_domains_enabled",
)


@inmanta.resources.resource("netbird::NameserverGroup", "name", "api.agent_name")
class NameserverGroupResource(ResourceABC):
    fields = ("name",)
    name: str


@inmanta.agent.handler.provider("netbird::NameserverGroup", "")
class NameserverGroupHandler(HandlerABC[NameserverGroupResource]):
    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the distribution groups, nor of the
        # domains.  The nameservers are deliberately left alone: they are queried in
        # the order they are written in, so that order is part of the desired state.
        for key in ("groups", "domains"):
            if body.get(key) is not None:
                body[key] = sorted(body[key])

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NameserverGroupResource,
    ) -> None:
        group = find_nameserver_group(self.session, resource.name)
        if group is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", group["id"])
        self.publish_ids(ctx, id=group["id"])

        resource.body = self.normalize(group)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NameserverGroupResource,
    ) -> None:
        group = process_netbird_response(
            self.session.post(
                url="dns/nameservers",
                json=select(self.merged_body({}, resource), NAMESERVER_GROUP_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=group["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: NameserverGroupResource,
    ) -> None:
        # The api requires the full object on an update, even the parts the model has
        # no opinion about: the body calculate_diff merged carries them along.
        process_netbird_response(
            self.session.put(
                url=f"dns/nameservers/{ctx.get('ID')}",
                json=select(resource.body, NAMESERVER_GROUP_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: NameserverGroupResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"dns/nameservers/{ctx.get('ID')}"),
        )
        ctx.set_purged()


# The only key of the dns settings object, which the api takes on an update.  There
# is no create and no delete endpoint: the settings are a singleton of the account.
DNS_SETTINGS_KEYS = ("disabled_management_groups",)


@inmanta.resources.resource("netbird::DnsSettings", "agent_name", "api.agent_name")
class DnsSettingsResource(ResourceABC):
    # The dns settings are a singleton of the account, they have no identity of their
    # own: the agent managing the api is what tells two of them apart.
    fields = ("agent_name",)
    agent_name: str

    @classmethod
    def get_agent_name(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        return entity.api.agent_name


@inmanta.agent.handler.provider("netbird::DnsSettings", "")
class DnsSettingsHandler(HandlerABC[DnsSettingsResource]):
    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the groups
        if body.get("disabled_management_groups") is not None:
            body["disabled_management_groups"] = sorted(
                body["disabled_management_groups"]
            )

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsSettingsResource,
    ) -> None:
        # The settings of an account always exist, so this never reports the resource
        # as purged: there is only ever an update to do.
        resource.body = self.normalize(
            process_netbird_response(
                self.session.get(url="dns/settings"),
                expected_type=dict,
            )
        )

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsSettingsResource,
    ) -> None:
        raise RuntimeError(
            "The dns settings of a netbird account always exist, they can not be "
            "created"
        )

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: DnsSettingsResource,
    ) -> None:
        process_netbird_response(
            self.session.put(
                url="dns/settings",
                json=select(resource.body, DNS_SETTINGS_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsSettingsResource,
    ) -> None:
        raise RuntimeError(
            "The dns settings of a netbird account can not be deleted, this resource "
            "should not be purged"
        )


# The keys of the zone object the api takes.  It takes the same ones on a create and
# on an update, and it requires the name, the domain and the distribution groups on
# both.  The domain can not be changed once the zone exists, but it still has to be
# sent along on every update.
ZONE_KEYS = (
    "name",
    "domain",
    "enabled",
    "enable_search_domain",
    "distribution_groups",
)


def find_dns_zone(session: Session, domain: str) -> dict | None:
    """
    Find a dns zone of the account by the domain it serves, and return None if it
    doesn't exist.  The domain is what identifies a zone: the api refuses a second
    zone on a domain it already serves, while it happily holds two zones of the same
    name.
    """
    zones = process_netbird_response(
        session.get(url="dns/zones"),
        expected_type=list[dict],
    )
    return next((z for z in zones if z["domain"] == domain), None)


@inmanta.resources.resource("netbird::DnsZone", "domain", "api.agent_name")
class DnsZoneResource(ResourceABC):
    fields = ("domain",)
    domain: str


@inmanta.agent.handler.provider("netbird::DnsZone", "")
class DnsZoneHandler(HandlerABC[DnsZoneResource]):
    def diff_body(self, body: dict) -> dict:
        # The zone the api returns carries the records it holds, which are managed by
        # netbird::DnsZoneRecord and are not part of what this resource writes.
        return select(body, ZONE_KEYS)

    def normalize(self, body: dict) -> dict:
        # The api keeps the distribution groups in the order they were sent, but they
        # are a set: listing the same groups in another order is not a change.
        if body.get("distribution_groups") is not None:
            body["distribution_groups"] = sorted(body["distribution_groups"])

        return body

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneResource,
    ) -> None:
        zone = find_dns_zone(self.session, resource.domain)
        if zone is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", zone["id"])
        self.publish_ids(ctx, id=zone["id"])

        resource.body = self.normalize(zone)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneResource,
    ) -> None:
        zone = process_netbird_response(
            self.session.post(
                url="dns/zones",
                json=select(self.merged_body({}, resource), ZONE_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=zone["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: DnsZoneResource,
    ) -> None:
        # The update replaces the whole zone: every key left out of the body is reset
        # to its zero value, and the api requires the name, the domain and the
        # distribution groups anyway.  The merged body carries them all.
        process_netbird_response(
            self.session.put(
                url=f"dns/zones/{ctx.get('ID')}",
                json=select(resource.body, ZONE_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(url=f"dns/zones/{ctx.get('ID')}"),
        )
        ctx.set_purged()


# The keys of a record the api takes.  The same ones on a create and on an update,
# and it requires the name, the type and the content on both.
RECORD_KEYS = ("name", "type", "content", "ttl")


def find_dns_zone_record(
    session: Session, zone: str, name: str, type: str
) -> dict | None:
    """
    Find a record of a dns zone by its name and its type, and return None if it
    doesn't exist.

    The api gives a record no key of its own: it only refuses a new one when the
    name, the type and the content are all three identical to an existing one.  This
    module addresses a record by its name and its type, and refuses to guess which
    one is meant when the zone holds several of them.

    :param zone: The id of the zone holding the record.
    :param name: The fully qualified name of the record.
    :param type: The type of the record.
    """
    records = process_netbird_response(
        session.get(url=f"dns/zones/{zone}/records"),
        expected_type=list[dict],
    )
    matches = [r for r in records if r["name"] == name and r["type"] == type]
    if len(matches) > 1:
        raise RuntimeError(
            f"The zone holds {len(matches)} {type} records named {name}, which this "
            "resource can not tell apart"
        )

    return matches[0] if matches else None


@inmanta.resources.resource("netbird::DnsZoneRecord", "key", "api.agent_name")
class DnsZoneRecordResource(ResourceABC):
    fields = ("key", "zone", "name", "type")
    key: str
    zone: str
    name: str
    type: str

    @classmethod
    def get_key(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        # A record is identified by its name together with its type: an api the model
        # addresses by name alone could not hold both an A and an AAAA record for the
        # same name, which is an ordinary thing for a dual stack host.
        return f"{entity.name}/{entity.type}"

    @classmethod
    def get_zone(
        cls, _: inmanta.export.Exporter, entity: inmanta.execute.proxy.DynamicProxy
    ) -> str:
        # The zone is the opaque id of another resource, which the model only knows as
        # a reference resolved on the agent.  It is kept out of the body sent to the
        # api, which takes the zone from the url instead, hence the leading underscore
        # on the model attribute.
        return inmanta.plugins.allow_reference_values(entity)._zone


@inmanta.agent.handler.provider("netbird::DnsZoneRecord", "")
class DnsZoneRecordHandler(HandlerABC[DnsZoneRecordResource]):
    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneRecordResource,
    ) -> None:
        record = find_dns_zone_record(
            self.session, resource.zone, resource.name, resource.type
        )
        if record is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", record["id"])
        self.publish_ids(ctx, id=record["id"])

        resource.body = self.normalize(record)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneRecordResource,
    ) -> None:
        record = process_netbird_response(
            self.session.post(
                url=f"dns/zones/{resource.zone}/records",
                json=select(self.merged_body({}, resource), RECORD_KEYS),
            ),
            expected_type=dict,
        )
        self.publish_ids(ctx, id=record["id"])
        ctx.set_created()

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: DnsZoneRecordResource,
    ) -> None:
        # The update replaces the whole record: a ttl left out of the body is reset to
        # zero, so the merged body is what has to be written back, not the change.
        process_netbird_response(
            self.session.put(
                url=f"dns/zones/{resource.zone}/records/{ctx.get('ID')}",
                json=select(resource.body, RECORD_KEYS),
            ),
        )
        ctx.set_updated()

    def delete_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: DnsZoneRecordResource,
    ) -> None:
        process_netbird_response(
            self.session.delete(
                url=f"dns/zones/{resource.zone}/records/{ctx.get('ID')}"
            ),
        )
        ctx.set_purged()
