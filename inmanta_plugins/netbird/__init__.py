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
    fields = ("management_url", "api_token", "desired_state")  # type: ignore[assignment]
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


def groups(session: Session) -> list[dict]:
    """
    Get all the groups of the account.  The netbird api identifies groups by id
    everywhere, but the model refers to them by name, as the name is the only stable
    identifier across accounts.
    """
    return process_netbird_response(
        session.get(url="groups"),
        expected_type=list[dict],
    )


def group_ids(session: Session, names: typing.Sequence[str]) -> list[str]:
    """
    Resolve a list of group names into the group ids the api expects.  A group that
    doesn't exist is a configuration error: netbird creates groups from the setup
    keys and the dashboard, this module never creates them.
    """
    ids_by_name = {group["name"]: group["id"] for group in groups(session)}
    missing = [name for name in names if name not in ids_by_name]
    if missing:
        raise RuntimeError(
            f"The following groups don't exist on the netbird account: {missing}"
        )

    return [ids_by_name[name] for name in names]


def group_names(session: Session, ids: typing.Sequence[str]) -> list[str]:
    """
    Resolve a list of group ids into their names, so that the current state can be
    compared to the desired state expressed in the model.
    """
    names_by_id = {group["id"]: group["name"] for group in groups(session)}
    return [names_by_id.get(id, id) for id in ids]


def find_user(
    session: Session, *, email: str, name: str | None, is_service_user: bool
) -> dict | None:
    """
    Find a user of the account, and return None if it doesn't exist.  The listing is
    not filtered on the service_user query parameter, so it holds both the regular
    and the service users of the account.

    A regular user is found back by its email address.  The api doesn't keep the
    email address of a service user, so that one is found back by its name, which the
    model requires it to have.
    """
    users = process_netbird_response(
        session.get(url="users"),
        expected_type=list[dict],
    )
    if is_service_user:
        return next(
            (u for u in users if u["is_service_user"] and u["name"] == name),
            None,
        )

    return next(
        (u for u in users if not u["is_service_user"] and u["email"] == email),
        None,
    )


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

    def resolved_values(self, resource: NR) -> dict:
        """
        The values the api addresses by opaque id, and that the model therefore
        expresses as a relation towards another object rather than as an attribute
        it could serialize itself.  They are resolved against the api, so they take
        part in the diff like any other value, and they are sent on every write.
        """
        return {}

    def diff_keys(self, resource: NR) -> set[str]:
        """
        The keys that take part in the diff between the current and the desired
        state.  By default those are all the ones the model has an opinion about,
        plus the values the handler resolves against the api.

        An object whose api rejects some of its values on an update narrows this
        down: a difference the handler has no way to enforce would otherwise make
        the resource change on every single deploy.
        """
        return managed_keys(resource.desired_state) | set(
            self.resolved_values(resource)
        )

    def to_model(self, body: dict) -> dict:
        """
        Translate the object the api reports into the shape the model expresses, e.g.
        a group that the api identifies by id and that the model names.
        """
        return body

    def to_api(self, body: dict) -> dict:
        """
        Translate the object the model expresses into the shape the api expects, the
        other way around from to_model.
        """
        return body

    def normalize(self, body: dict) -> dict:
        """
        Put a body in a canonical shape, so that the current and the desired state
        can be compared even when the api returns a collection in another order.
        """
        return body

    def read_body(self, resource: NR, current: dict) -> dict:
        """
        Narrow the object the api returned down to the values that take part in the
        diff.  The api reports a lot more than that (what the agent detected, what
        the api computed) and rejects most of it on a write, so it is kept out of
        both the diff and the update body.

        :param resource: The resource being read, carrying the desired state.
        :param current: The object as the api returned it.
        """
        body = self.to_model(current)

        return self.normalize({key: body.get(key) for key in self.diff_keys(resource)})

    def write_body(self, resource: NR) -> dict:
        """
        The body to send to the api.  Applying the desired state is idempotent, so
        this works both on a create (nothing was read) and on an update (where
        calculate_diff already applied it onto the current state).
        """
        return self.to_api(
            apply_desired_state(resource.body, resource.desired_state)
            | self.resolved_values(resource)
        )

    def calculate_diff(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        current: NR,
        desired: NR,
    ) -> dict[str, dict[str, object]]:
        keys = self.diff_keys(desired)
        body = apply_desired_state(current.body, desired.desired_state) | (
            self.resolved_values(desired)
        )
        desired.body = self.normalize({key: body[key] for key in keys if key in body})

        changes = super().calculate_diff(ctx, current, desired)
        if desired.body != current.body:
            changes["body"] = {"current": current.body, "desired": desired.body}

        return changes


def managed_keys(desired_state: list[dict]) -> set[str]:
    """
    The top level keys the model has an opinion about, taken from the serialized
    object.  The api reports much more than that (what the agent detected, what the
    api computed), and it rejects those read-only values on an update, so both the
    diff and the update body are narrowed down to these keys.

    :param desired_state: The serialized object describing the desired state.
    """
    return {key for item in desired_state for key in item["value"] or {}}


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


# The keys of the user object the api takes when a user is created, and the ones it
# takes when an existing user is updated.  They don't overlap fully: the identity of
# the user is fixed at creation, and a user can only be blocked afterwards.
USER_CREATE_KEYS = ("email", "name", "role", "auto_groups", "is_service_user")
USER_UPDATE_KEYS = ("role", "auto_groups", "is_blocked")


@inmanta.resources.resource("netbird::User", "email", "api.agent_name")
class UserResource(ResourceABC):
    fields = ("email", "name", "is_service_user")
    email: str
    name: str | None
    is_service_user: bool


@inmanta.agent.handler.provider("netbird::User", "")
class UserHandler(HandlerABC[UserResource]):
    def diff_keys(self, resource: UserResource) -> set[str]:
        # The api only takes USER_UPDATE_KEYS on an update, so the values it only
        # accepts at creation are kept out of the diff: the handler could not
        # enforce a change to them anyway.
        return super().diff_keys(resource) & set(USER_UPDATE_KEYS)

    def to_model(self, body: dict) -> dict:
        # The api identifies the groups a user's peers are added to by id, the model
        # refers to them by name
        if "auto_groups" in body:
            body["auto_groups"] = group_names(self.session, body["auto_groups"] or [])

        return body

    def to_api(self, body: dict) -> dict:
        if "auto_groups" in body:
            body["auto_groups"] = group_ids(self.session, body["auto_groups"])

        return body

    def normalize(self, body: dict) -> dict:
        # The api doesn't preserve the order of the auto groups
        if "auto_groups" in body:
            body["auto_groups"] = sorted(body["auto_groups"])

        return body

    def create_body(self, resource: UserResource) -> dict:
        return {
            k: v for k, v in self.write_body(resource).items() if k in USER_CREATE_KEYS
        }

    def update_body(self, resource: UserResource) -> dict:
        return {
            k: v for k, v in self.write_body(resource).items() if k in USER_UPDATE_KEYS
        }

    def read_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: UserResource,
    ) -> None:
        user = find_user(
            self.session,
            email=resource.email,
            name=resource.name,
            is_service_user=resource.is_service_user,
        )
        if user is None:
            raise inmanta.agent.handler.ResourcePurged()

        ctx.set("ID", user["id"])
        self.publish_ids(ctx, user_id=user["id"])

        resource.body = self.read_body(resource, user)

    def create_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        resource: UserResource,
    ) -> None:
        user = process_netbird_response(
            self.session.post(url="users", json=self.create_body(resource)),
            expected_type=dict,
        )
        self.publish_ids(ctx, user_id=user["id"])
        ctx.set_created()

        update_body = self.update_body(resource)
        if update_body.get("is_blocked"):
            # The api doesn't take the blocked flag when the user is created, so a
            # user the model wants blocked from the start needs a second call.
            process_netbird_response(
                self.session.put(url=f"users/{user['id']}", json=update_body),
            )

    def update_resource(
        self,
        ctx: inmanta.agent.handler.HandlerContext,
        changes: dict,
        resource: UserResource,
    ) -> None:
        process_netbird_response(
            self.session.put(
                url=f"users/{ctx.get('ID')}",
                json=self.update_body(resource),
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
