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
import typing
from dataclasses import asdict

import pydantic
import requests
from inmanta_plugins.files.json import Operation, serialize_for_resource, update

import inmanta.agent.handler
import inmanta.execute.proxy
import inmanta.export
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
# it takes when an existing key is updated.  Nearly everything about a key is fixed at
# creation: only its revocation and its auto groups can be changed afterwards, and the
# revocation is the other way around — the api ignores it on the create.
SETUP_KEY_CREATE_KEYS = (
    "name",
    "type",
    "expires_in",
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
        # The api keeps the auto groups in the order it was given them, which carries
        # no meaning: the same set written in another order is not a change.
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

        if desired.get("revoked"):
            # The api ignores the revocation when a key is created, so a key the model
            # wants revoked from the start needs a second call.  It is built from the
            # key the api just made, as an update has to carry the auto groups too.
            process_netbird_response(
                self.session.put(
                    url=f"setup-keys/{setup_key['id']}",
                    json=select(
                        self.merged_body(setup_key, resource), SETUP_KEY_UPDATE_KEYS
                    ),
                ),
            )

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: SetupKeyResource,
    ) -> None:
        # The body calculate_diff merged is the object as the api holds it, with the
        # desired state applied on top.  The api only takes part of it on an update,
        # and requires the auto groups on every call even when the revocation is what
        # is being changed.
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
