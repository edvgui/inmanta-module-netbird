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

import pytest
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP

import inmanta.ast
from inmanta import const

USER_EMAIL = "alice@example.com"

Compile = collections.abc.Callable[[str], None]
Get = collections.abc.Callable[[str], list[dict] | dict]
Facts = collections.abc.Callable[[], dict[str, str]]


def find_user(get: Get, email: str) -> dict | None:
    return next((u for u in get("users") if u["email"] == email), None)


def find_service_user(get: Get, name: str) -> dict | None:
    return next(
        (u for u in get("users") if u["is_service_user"] and u["name"] == name), None
    )


def user_model(
    *,
    name: str | None = "Alice",
    role: str = "user",
    auto_groups: list[str] | None = None,
    is_blocked: bool = False,
    is_service_user: bool = False,
    purged: bool = False,
) -> str:
    named = "" if name is None else f"name={name!r},"
    return f"""
        user = netbird::User(
            api=api,
            email={USER_EMAIL!r},
            {named}
            role={role!r},
            auto_groups={auto_groups if auto_groups is not None else []},
            is_blocked={str(is_blocked).lower()},
            is_service_user={str(is_service_user).lower()},
            purged={str(purged).lower()},
        )
    """


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
    compile_model(user_model())
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_EMAIL)
    assert user is not None
    # The id the api gave the user is published, already on the deploy that
    # created it.
    assert facts() == {"user_id": user["id"]}
    assert user["name"] == "Alice"
    assert user["role"] == "user"
    assert user["auto_groups"] == []
    assert user["is_blocked"] is False
    assert user["is_service_user"] is False

    # A second deploy of the same desired state changes nothing.
    compile_model(user_model())
    project.deploy_resource("netbird::User", change=const.Change.nochange)

    compile_model(user_model(role="admin"))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_EMAIL)["role"] == "admin"

    compile_model(user_model(role="admin", is_blocked=True))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_EMAIL)["is_blocked"] is True

    compile_model(user_model(purged=True))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_EMAIL) is None


def test_user_auto_groups_are_named(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    The api identifies the groups a user's peers are added to by id, the model refers
    to them by name.  The names are resolved both ways, so the same desired state is
    a no-op on the second deploy.
    """
    compile_model(user_model(auto_groups=[DEFAULT_GROUP]))
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_EMAIL)
    group_names = {g["id"]: g["name"] for g in get("groups")}
    assert [group_names[i] for i in user["auto_groups"]] == [DEFAULT_GROUP]

    compile_model(user_model(auto_groups=[DEFAULT_GROUP]))
    project.deploy_resource("netbird::User", change=const.Change.nochange)

    compile_model(user_model(auto_groups=[]))
    project.deploy_resource("netbird::User")
    assert find_user(get, USER_EMAIL)["auto_groups"] == []


def test_user_with_unknown_group_fails(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    This module never creates a group, it only refers to the ones the account already
    has.  A group that doesn't exist is a configuration error, and the deploy says
    which one is missing rather than silently inviting a user into nothing.
    """
    compile_model(user_model(auto_groups=["Nope"]))
    project.deploy_resource("netbird::User", status=const.ResourceState.failed)

    assert any(
        "Nope" in log._data["msg"] or "Nope" in str(log._data.get("traceback", ""))
        for log in project.ctx.logs
    )
    assert find_user(get, USER_EMAIL) is None


def test_service_user_is_created(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
) -> None:
    """
    A service user only exists to hold access tokens, it can not log in.  It is
    created by the same resource, and shows up in the same listing.  The api drops
    its email address, so it is found back by its name instead: a redeploy must not
    create a second one.
    """
    compile_model(user_model(name="Alice", is_service_user=True, role="admin"))
    project.deploy_resource("netbird::User")

    user = find_service_user(get, "Alice")
    assert user is not None
    assert user["email"] == ""
    assert user["role"] == "admin"

    compile_model(user_model(name="Alice", is_service_user=True, role="admin"))
    project.deploy_resource("netbird::User", change=const.Change.nochange)
    assert [u["name"] for u in get("users") if u["is_service_user"]] == ["Alice"]

    compile_model(
        user_model(name="Alice", is_service_user=True, role="admin", purged=True)
    )
    project.deploy_resource("netbird::User")
    assert find_service_user(get, "Alice") is None


def test_service_user_requires_a_name(
    compile_model: Compile,
) -> None:
    """
    The api keeps nothing but the name of a service user, so a model that doesn't
    give one describes a user that could never be found back.  That is a compile
    error, not something to discover on the first redeploy.
    """
    with pytest.raises(inmanta.ast.ExternalException, match="requires a name"):
        compile_model(user_model(name=None, is_service_user=True))


def test_settings_left_out_of_the_api_update_are_not_enforced(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    get: Get,
    netbird: requests.Session,
) -> None:
    """
    The api only takes the role, the auto groups and the blocked flag when a user is
    updated.  The name of a user is therefore only set when it is created, and a
    later change to it is not something the handler could enforce: it must not show
    up as a change on every deploy.
    """
    compile_model(user_model(name="Alice"))
    project.deploy_resource("netbird::User")

    user = find_user(get, USER_EMAIL)
    assert user["name"] == "Alice"

    compile_model(user_model(name="Alice Cooper"))
    project.deploy_resource("netbird::User", change=const.Change.nochange)
    assert find_user(get, USER_EMAIL)["name"] == "Alice"
