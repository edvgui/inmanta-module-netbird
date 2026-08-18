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
import json

import inmanta_plugins.std
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP, facts, get

import inmanta.plugins
from inmanta import const

SETUP_KEY_NAME = "peers-of-the-lab"
SETUP_KEY_EXPIRY = 86400

Compile = collections.abc.Callable[[str], None]


def find_setup_key(netbird: requests.Session, name: str) -> dict | None:
    return next((k for k in get(netbird, "setup-keys") if k["name"] == name), None)


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def create_group(netbird: requests.Session, name: str) -> str:
    """
    Create a group on the account and return its id.  The auto groups of a setup key
    can not be the group every account comes with: the api rejects it, so the tests
    make groups of their own to point at.
    """
    response = netbird.post(f"{netbird.base_url}/groups", json={"name": name})
    response.raise_for_status()
    return response.json()["id"]


def setup_key_model(**attributes: object) -> str:
    """
    Build a setup key resource, with only the attributes the test has an opinion
    about: everything else is left null, and is therefore not managed.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    return "\n".join(
        [
            "setup_key = netbird::SetupKey(",
            "    api=api,",
            f"    name={json.dumps(SETUP_KEY_NAME)},",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_setup_key_created_revoked_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A setup key is created on the account, the revocation the api lets us change
    afterwards is kept in line with the model, and the key is removed when the
    resource is purged.
    """
    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, usage_limit=3)
    )
    project.deploy_resource("netbird::SetupKey")

    setup_key = find_setup_key(netbird, SETUP_KEY_NAME)
    assert setup_key is not None
    # The id the api gave the key and the key it generated are both published, already
    # on the deploy that created them.  The listing reports the key masked, so the fact
    # is the only place the value a peer can register with is kept.
    published = facts(project)
    assert set(published) == {"id", "key"}
    assert published["id"] == setup_key["id"]
    assert "*" not in published["key"]
    assert published["key"].startswith(setup_key["key"].rstrip("*"))
    assert setup_key["type"] == "reusable"
    assert setup_key["usage_limit"] == 3
    assert setup_key["revoked"] is False
    assert setup_key["valid"] is True

    # A second deploy of the same desired state changes nothing.  It goes through the
    # read, which sees the key masked: republishing it there would replace a usable
    # key with `ABCDE****`, so the read publishes the id alone.
    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, usage_limit=3)
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)
    assert facts(project) == {"id": setup_key["id"]}

    # Revoking is the one change the api takes on an existing key.
    compile_model(setup_key_model(revoked=True))
    project.deploy_resource("netbird::SetupKey")
    revoked = find_setup_key(netbird, SETUP_KEY_NAME)
    assert revoked["revoked"] is True
    assert revoked["valid"] is False

    # And it only goes that way: the api refuses to un-revoke a key, so a model asking
    # for it is a failed deploy rather than something silently ignored.
    compile_model(setup_key_model(revoked=False))
    project.deploy_resource("netbird::SetupKey", status=const.ResourceState.failed)
    assert find_setup_key(netbird, SETUP_KEY_NAME)["revoked"] is True

    compile_model(setup_key_model(purged=True))
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME) is None


def test_setup_key_auto_groups(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api identifies the groups the peers registered with a key are added to by id,
    and so does the model: no name is translated on the way in or out, so the same
    desired state is a no-op on the second deploy.  The api keeps them in the order it
    was given them, which carries no meaning: the same set in another order is not a
    change either.
    """
    groups = [create_group(netbird, "lab"), create_group(netbird, "staging")]

    compile_model(
        setup_key_model(type="one-off", expires_in=SETUP_KEY_EXPIRY, auto_groups=groups)
    )
    project.deploy_resource("netbird::SetupKey")
    assert sorted(find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"]) == sorted(
        groups
    )

    compile_model(
        setup_key_model(
            type="one-off",
            expires_in=SETUP_KEY_EXPIRY,
            auto_groups=list(reversed(groups)),
        )
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)

    # The auto groups are the other thing the api takes on an update.
    compile_model(setup_key_model(auto_groups=[groups[0]]))
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == [groups[0]]

    # An empty collection comes back as one: the api never answers a json null here.
    compile_model(setup_key_model(auto_groups=[]))
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == []


def test_create_only_attributes_are_not_enforced(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api only takes the revocation and the auto groups when an existing key is
    updated.  Everything else is applied when the key is created, and a later change
    to it is not something the handler could enforce: it must not show up as a change
    on every deploy.

    The expiry is the worst of them: the api takes a duration and reports back the
    moment the key expires, so a diff on it could never converge either.
    """
    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, usage_limit=3)
    )
    project.deploy_resource("netbird::SetupKey")
    expires = find_setup_key(netbird, SETUP_KEY_NAME)["expires"]

    compile_model(
        setup_key_model(type="one-off", expires_in=3600, usage_limit=10, ephemeral=True)
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)

    unchanged = find_setup_key(netbird, SETUP_KEY_NAME)
    assert unchanged["type"] == "reusable"
    assert unchanged["usage_limit"] == 3
    assert unchanged["expires"] == expires


def test_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    Every attribute the model leaves null keeps whatever value the account holds for
    it, even when it was changed in the dashboard behind our back.  And a value the
    model does set is enforced, dashboard or not.
    """
    group = create_group(netbird, "lab")

    compile_model(setup_key_model(type="one-off", expires_in=SETUP_KEY_EXPIRY))
    project.deploy_resource("netbird::SetupKey")

    setup_key = find_setup_key(netbird, SETUP_KEY_NAME)
    netbird.put(
        f"{netbird.base_url}/setup-keys/{setup_key['id']}",
        json={"revoked": False, "auto_groups": [group]},
    ).raise_for_status()

    # The auto groups aren't managed: the deploy leaves them as they are.
    compile_model(setup_key_model(type="one-off", expires_in=SETUP_KEY_EXPIRY))
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == [group]

    compile_model(setup_key_model(auto_groups=[]))
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == []


def test_setup_key_revoked_from_the_start(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api ignores the revocation when a key is created, so the handler revokes the
    key it just made: a model asking for a revoked key converges on the first deploy
    rather than on the next repair.
    """
    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, revoked=True)
    )
    project.deploy_resource("netbird::SetupKey")

    setup_key = find_setup_key(netbird, SETUP_KEY_NAME)
    assert setup_key["revoked"] is True
    assert setup_key["valid"] is False

    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, revoked=True)
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)


def test_the_default_group_is_not_an_auto_group(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api refuses the group every account comes with as an auto group of a setup key.
    That refusal is reported as a failed deploy rather than silently ignored: the model
    asked for something the account can not do.
    """
    compile_model(
        setup_key_model(
            type="reusable",
            expires_in=SETUP_KEY_EXPIRY,
            auto_groups=[group_id(netbird, DEFAULT_GROUP)],
        )
    )
    project.deploy_resource("netbird::SetupKey", status=const.ResourceState.failed)
    assert find_setup_key(netbird, SETUP_KEY_NAME) is None


def test_key_is_a_fact_reference(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The generated key is exposed the way the object id is: the model never holds the
    value, it holds a reference that reads back the fact the handler published, and
    whoever registers a peer with it resolves that on the agent.
    """
    compile_model(setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY))

    (setup_key,) = project.get_instances("netbird::SetupKey")
    key = inmanta.plugins.allow_reference_values(setup_key)._key
    assert isinstance(key, inmanta_plugins.std.FactReference)
    assert key.fact_name == "key"


def test_the_key_is_not_sent_back_to_the_api(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
) -> None:
    """
    The key is the api's to generate, so it must not end up in the body the handler
    writes.  Its leading underscore is what keeps it out of the serialized desired
    state — without it the deploy would also have to resolve the reference before the
    create that produces the fact has run.
    """
    compile_model(setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY))

    resource = project.get_resource("netbird::SetupKey")
    assert resource is not None
    serialized = {
        key
        for state in resource.desired_state
        for key in (state["value"] if isinstance(state["value"], dict) else {})
    }
    assert "_key" not in serialized
    assert "key" not in serialized
