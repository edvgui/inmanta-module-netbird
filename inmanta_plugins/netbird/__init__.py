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
