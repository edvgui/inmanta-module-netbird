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

import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP, facts, get

from inmanta import const

SETUP_KEY_NAME = "peers-of-the-lab"
SETUP_KEY_EXPIRY = 86400

Compile = collections.abc.Callable[[str], None]


def find_setup_key(netbird: requests.Session, name: str) -> dict | None:
    return next((k for k in get(netbird, "setup-keys") if k["name"] == name), None)


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


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
    # The id the api gave the key is published, already on the deploy that created
    # it.  The key itself is a secret, and is not published.
    assert facts(project) == {"id": setup_key["id"]}
    assert setup_key["type"] == "reusable"
    assert setup_key["usage_limit"] == 3
    assert setup_key["revoked"] is False
    assert setup_key["valid"] is True

    # A second deploy of the same desired state changes nothing.
    compile_model(
        setup_key_model(type="reusable", expires_in=SETUP_KEY_EXPIRY, usage_limit=3)
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)

    # Revoking is the one change the api takes on an existing key.
    compile_model(setup_key_model(revoked=True))
    project.deploy_resource("netbird::SetupKey")
    revoked = find_setup_key(netbird, SETUP_KEY_NAME)
    assert revoked["revoked"] is True
    assert revoked["valid"] is False

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
    desired state is a no-op on the second deploy.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(
        setup_key_model(
            type="one-off", expires_in=SETUP_KEY_EXPIRY, auto_groups=[all_group]
        )
    )
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == [all_group]

    compile_model(
        setup_key_model(
            type="one-off", expires_in=SETUP_KEY_EXPIRY, auto_groups=[all_group]
        )
    )
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)

    # The auto groups are the other thing the api takes on an update.
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
    all_group = group_id(netbird, DEFAULT_GROUP)

    compile_model(setup_key_model(type="one-off", expires_in=SETUP_KEY_EXPIRY))
    project.deploy_resource("netbird::SetupKey")

    setup_key = find_setup_key(netbird, SETUP_KEY_NAME)
    netbird.put(
        f"{netbird.base_url}/setup-keys/{setup_key['id']}",
        json={"revoked": False, "auto_groups": [all_group]},
    ).raise_for_status()

    # The auto groups aren't managed: the deploy leaves them as they are.
    compile_model(setup_key_model(type="one-off", expires_in=SETUP_KEY_EXPIRY))
    project.deploy_resource("netbird::SetupKey", change=const.Change.nochange)
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == [all_group]

    compile_model(setup_key_model(auto_groups=[]))
    project.deploy_resource("netbird::SetupKey")
    assert find_setup_key(netbird, SETUP_KEY_NAME)["auto_groups"] == []
