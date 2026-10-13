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

Compile = collections.abc.Callable[[str], None]

# The settings this module models.  Everything else the account holds has to come out
# of a deploy as it went in.
MANAGED = (
    "dns_domain",
    "network_range",
    "network_range_v6",
    "ipv6_enabled_groups",
    "routing_peer_dns_resolution_enabled",
)


def account(netbird: requests.Session) -> dict:
    (only,) = get(netbird, "accounts")
    return only


def unmanaged(settings: dict) -> dict:
    return {k: v for k, v in settings.items() if k not in MANAGED}


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def account_settings_model(**attributes: object) -> str:
    """
    Build the settings resource of the account, with only the attributes the test has
    an opinion about: everything else is left null, and is therefore not managed.
    """
    return "\n".join(
        [
            "settings = netbird::AccountSettings(",
            "    api=api,",
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_account_settings_updated(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The settings of the account always exist, so the resource only ever updates them,
    and the settings this module doesn't model come out of it untouched.
    """
    before = account(netbird)
    all_group = group_id(netbird, DEFAULT_GROUP)

    model = account_settings_model(
        dns_domain="ni.example.com",
        # The host bits are cleared by the api, the range it stores is 10.128.0.0/24,
        # which must not show up as a change on every deploy.
        network_range="10.128.0.5/24",
        network_range_v6="fd79:3cbf:152c:a3e0::/64",
        ipv6_enabled_groups=[],
        routing_peer_dns_resolution_enabled=False,
    )
    compile_model(model)
    project.deploy_resource("netbird::AccountSettings")

    after = account(netbird)
    assert facts(project)["id"] == before["id"]
    assert after["settings"]["dns_domain"] == "ni.example.com"
    assert after["settings"]["network_range"] == "10.128.0.0/24"
    assert after["settings"]["network_range_v6"] == "fd79:3cbf:152c:a3e0::/64"
    assert after["settings"]["ipv6_enabled_groups"] == []
    assert after["settings"]["routing_peer_dns_resolution_enabled"] is False
    # The update writes every setting of the account back, the ones it doesn't model
    # included: they are left as they were.
    assert unmanaged(after["settings"]) == unmanaged(before["settings"])

    compile_model(model)
    project.deploy_resource("netbird::AccountSettings", change=const.Change.nochange)

    compile_model(account_settings_model(ipv6_enabled_groups=[all_group]))
    project.deploy_resource("netbird::AccountSettings")
    assert account(netbird)["settings"]["ipv6_enabled_groups"] == [all_group]


def test_account_settings_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A setting the model leaves null keeps whatever value the account holds for it, even
    when it was changed in the dashboard behind our back.  The api replaces every
    setting on an update, so the handler has to carry that value along.
    """
    current = account(netbird)
    changed = netbird.put(
        f"{netbird.base_url}/accounts/{current['id']}",
        json={"settings": {**current["settings"], "dns_domain": "dashboard.example"}},
    )
    changed.raise_for_status()

    compile_model(account_settings_model(network_range="10.129.0.0/24"))
    project.deploy_resource("netbird::AccountSettings")

    settings = account(netbird)["settings"]
    assert settings["network_range"] == "10.129.0.0/24"
    assert settings["dns_domain"] == "dashboard.example"


def test_account_settings_can_not_be_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The api has no endpoint to delete the settings of an account, they are part of the
    account itself.  Purging the resource is therefore reported as a failed deploy
    rather than as a deploy that did nothing.
    """
    before = account(netbird)["settings"]

    compile_model(account_settings_model(dns_domain="ni.example.com", purged=True))
    project.deploy_resource(
        "netbird::AccountSettings", status=const.ResourceState.failed
    )
    assert account(netbird)["settings"] == before


def test_account_settings_published_as_facts(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    Every setting the account holds is published as a fact, the ones the model leaves
    null included, so that another resource can point at a setting the model does not
    manage.
    """
    before = account(netbird)

    compile_model(account_settings_model(dns_domain="ni.example.com"))
    resource = project.deploy_resource("netbird::AccountSettings")

    published = facts(project)
    assert published["id"] == before["id"]
    # The value the deploy wrote, not the one the account held before it.
    assert published["dns_domain"] == "ni.example.com"
    # A string is published as it is, anything else json encoded.
    assert published["network_range"] == before["settings"]["network_range"]
    assert published["peer_login_expiration"] == json.dumps(
        before["settings"]["peer_login_expiration"]
    )

    # The test runner only knows about a fact once it put it there itself, before the
    # compile that builds the reference.
    project.add_fact(
        resource.id.resource_str(), "network_range", published["network_range"]
    )
    compile_model(
        account_settings_model(dns_domain="ni.example.com")
        + "\nnetbird::Network(\n"
        + "    api=api,\n"
        + '    name="overlay",\n'
        + '    description=std::create_fact_reference(settings, "network_range"),\n'
        + ")\n"
    )
    project.deploy_resource("netbird::Network")
    (network,) = get(netbird, "networks")
    assert network["description"] == before["settings"]["network_range"]
