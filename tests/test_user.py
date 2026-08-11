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

import inmanta_plugins.std
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP

import inmanta.plugins
from inmanta import const

USER_NAME = "Alice"
USER_EMAIL = "alice@example.com"

Compile = collections.abc.Callable[[str], None]
Get = collections.abc.Callable[[str], list[dict] | dict]
Facts = collections.abc.Callable[[], dict[str, str]]


def find_user(get: Get, name: str) -> dict | None:
    return next((u for u in get("users") if u["name"] == name), None)


def group_id(get: Get, name: str) -> str:
    return next(g["id"] for g in get("groups") if g["name"] == name)


def user_model(**attributes: object) -> str:
    """
    Build a user resource, with only the attributes the test has an opinion about:
    everything else is left null, and is therefore not managed.
    """
    return "\n".join(
        [
            "user = netbird::User(",
            "    api=api,",
            f"    name={USER_NAME!r},",
            *(f"    {key}={value}," for key, value in attributes.items()),
            ")",
        ]
    )


def dsl(value: object) -> str:
    """
    Render a python value as the inmanta literal it maps onto.
    """
    match value:
        case bool():
            return str(value).lower()
        case list():
            return "[" + ", ".join(dsl(item) for item in value) + "]"
        case _:
            return repr(value)


def test_user_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
    facts: Facts,
) -> None:
    """
    A user is invited on the account, the settings the api lets us change afterwards
    are kept in line with the model, and the user is removed when the resource is
    purged.
    """
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_NAME)
    assert user is not None
    # The id the api gave the user is published, already on the deploy that
    # created it.
    assert facts() == {"id": user["id"]}
    assert user["email"] == USER_EMAIL
    assert user["role"] == "user"
    assert user["is_blocked"] is False
    assert user["is_service_user"] is False

    # A second deploy of the same desired state changes nothing.
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))
    project.deploy_resource("netbird::User", change=const.Change.nochange)

    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("admin")))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME)["role"] == "admin"

    compile_model(user_model(email=dsl(USER_EMAIL), is_blocked=dsl(True)))
    project.deploy_resource("netbird::User")
    blocked = find_user(get, USER_NAME)
    assert blocked["is_blocked"] is True
    # The role isn't managed anymore, and the api requires one on every update: the
    # one the account holds is carried along instead of being reset.
    assert blocked["role"] == "admin"

    compile_model(user_model(purged=dsl(True)))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME) is None


def test_user_auto_groups(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    The api identifies the groups a user's peers are added to by id, and so does the
    model: no name is translated on the way in or out, so the same desired state is a
    no-op on the second deploy.
    """
    all_group = group_id(get, DEFAULT_GROUP)

    compile_model(
        user_model(
            email=dsl(USER_EMAIL), role=dsl("user"), auto_groups=dsl([all_group])
        )
    )
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME)["auto_groups"] == [all_group]

    compile_model(
        user_model(
            email=dsl(USER_EMAIL), role=dsl("user"), auto_groups=dsl([all_group])
        )
    )
    project.deploy_resource("netbird::User", change=const.Change.nochange)

    compile_model(user_model(email=dsl(USER_EMAIL), auto_groups=dsl([])))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME)["auto_groups"] == []


def test_service_user_created_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    A service user only exists to hold access tokens, it can not log in, and the api
    keeps no email address for it.  The name is what identifies every user here, so
    the same resource manages it, and a redeploy doesn't create a second one.
    """
    compile_model(user_model(role=dsl("admin"), is_service_user=dsl(True)))
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_NAME)
    assert user is not None
    assert user["is_service_user"] is True
    assert user["email"] == ""
    assert user["role"] == "admin"

    compile_model(user_model(role=dsl("admin"), is_service_user=dsl(True)))
    project.deploy_resource("netbird::User", change=const.Change.nochange)
    assert [u["name"] for u in get("users") if u["is_service_user"]] == [USER_NAME]

    compile_model(user_model(purged=dsl(True)))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME) is None


def test_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
    netbird: requests.Session,
) -> None:
    """
    Every attribute the model leaves null keeps whatever value the account holds for
    it, even when it was changed in the dashboard behind our back.  And a value the
    model does set is enforced, dashboard or not.
    """
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_NAME)
    netbird.put(
        f"{netbird.base_url}/users/{user['id']}",
        json={"role": "user", "auto_groups": [], "is_blocked": True},
    ).raise_for_status()

    # The blocked flag isn't managed: the deploy leaves it as it is.
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))
    project.deploy_resource("netbird::User", change=const.Change.nochange)
    assert find_user(get, USER_NAME)["is_blocked"] is True

    compile_model(user_model(email=dsl(USER_EMAIL), is_blocked=dsl(False)))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME)["is_blocked"] is False


def test_create_only_attributes_are_not_enforced(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    The api only takes the role, the auto groups and the blocked flag when a user is
    updated.  The email address of a user is therefore only set when it is created,
    and a later change to it is not something the handler could enforce: it must not
    show up as a change on every deploy.
    """
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_NAME)["email"] == USER_EMAIL

    compile_model(user_model(email=dsl("alice.cooper@example.com"), role=dsl("user")))
    project.deploy_resource("netbird::User", change=const.Change.nochange)
    assert find_user(get, USER_NAME)["email"] == USER_EMAIL


def test_resource_id_is_a_fact_reference(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The api addresses every object by an opaque id the model never knows.  It is
    exposed on the resource all the same, as a reference that reads it back from the
    facts the handler publishes, so another resource can point at this user without
    going digging through the api.
    """
    compile_model(user_model(email=dsl(USER_EMAIL), role=dsl("user")))

    (user,) = project.get_instances("netbird::User")
    resource_id = inmanta.plugins.allow_reference_values(user).resource_id
    assert isinstance(resource_id, inmanta_plugins.std.FactReference)
    assert resource_id.fact_name == "id"
