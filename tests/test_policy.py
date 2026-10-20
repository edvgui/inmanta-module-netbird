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

import pytest
import pytest_inmanta.plugin
import requests
from conftest import DEFAULT_GROUP, facts, get

import inmanta.ast
from inmanta import const

POLICY_NAME = "lab"

# The group the sources of the rules point at, next to the account's own All group.
SOURCE_GROUP = "lab-clients"

Compile = collections.abc.Callable[[str], None]


def find_policy(netbird: requests.Session, name: str) -> dict | None:
    return next((p for p in get(netbird, "policies") if p["name"] == name), None)


def group_id(netbird: requests.Session, name: str) -> str:
    return next(g["id"] for g in get(netbird, "groups") if g["name"] == name)


def create_group(netbird: requests.Session, name: str) -> str:
    created = netbird.post(f"{netbird.base_url}/groups", json={"name": name})
    created.raise_for_status()
    return created.json()["id"]


def rule_of(policy: dict) -> dict:
    """
    The one rule of a policy.  The api holds the rules in a list and keeps a single
    entry of it: a policy with two rules is accepted, echoed back, and read back with
    one.
    """
    (rule,) = policy["rules"]
    return rule


def group_ids(rule: dict, key: str) -> list[str]:
    """
    The ids of the groups a rule points at.  The api reports them as objects, and only
    takes the ids back.
    """
    return sorted(group["id"] for group in rule[key] or [])


# The ends of a rule that point at a resource, and the entity each of them takes.
RESOURCE_TARGETS = {
    "sourceResource": "netbird::PolicyRuleSourceResource",
    "destinationResource": "netbird::PolicyRuleDestinationResource",
}


def dsl_value(key: str, value: object) -> str:
    """
    The dsl for a value of a rule: json, but for a resource target, which is an
    embedded entity of its own.
    """
    if key in RESOURCE_TARGETS:
        assert isinstance(value, dict)
        attrs = ", ".join(f"{k}={json.dumps(v)}" for k, v in value.items())
        return f"{RESOURCE_TARGETS[key]}({attrs})"

    return json.dumps(value)


def policy_model(*, rule: dict | None = None, **attributes: object) -> str:
    """
    Build a policy resource, with only the attributes the test has an opinion about:
    everything else is left null, and is therefore not managed.

    The rule is an embedded entity rather than an attribute: the api holds it as an
    object inside the policy, which the dsl has no attribute type for.

    The values are serialized as json, which the dsl takes as is for the primitive
    values a netbird object is made of.
    """
    attrs = ", ".join(
        f"{key}={dsl_value(key, value)}" for key, value in (rule or {}).items()
    )
    return "\n".join(
        [
            "policy = netbird::Policy(",
            "    api=api,",
            f"    name={json.dumps(POLICY_NAME)},",
            *([f"    rule=netbird::PolicyRule({attrs}),"] if rule is not None else []),
            *(f"    {key}={json.dumps(value)}," for key, value in attributes.items()),
            ")",
        ]
    )


def test_policy_created_updated_and_purged(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A policy is created on the account, the values the model has an opinion about are
    kept in line with it, and it is removed when the resource is purged.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    clients = create_group(netbird, SOURCE_GROUP)

    compile_model(
        policy_model(
            enabled=True,
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "all",
                "action": "accept",
                "bidirectional": True,
                "enabled": True,
            },
        )
    )
    project.deploy_resource("netbird::Policy")

    policy = find_policy(netbird, POLICY_NAME)
    assert policy is not None
    # The id the api gave the policy is published, already on the deploy that created
    # it.
    assert facts(project)["id"] == policy["id"]
    assert policy["enabled"] is True

    rule = rule_of(policy)
    assert group_ids(rule, "sources") == [clients]
    assert group_ids(rule, "destinations") == [all_group]
    assert rule["protocol"] == "all"
    assert rule["action"] == "accept"
    assert rule["bidirectional"] is True

    # A second deploy of the same desired state changes nothing: the api reports the
    # groups of a rule as objects and takes them as ids, which is no difference, and
    # it holds the rule in a list the handler keeps a single object.
    compile_model(
        policy_model(
            enabled=True,
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "all",
                "action": "accept",
                "bidirectional": True,
                "enabled": True,
            },
        )
    )
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)

    # The rule is rewritten as a whole, ports and all: the api validates the policy on
    # every call and takes no partial one.
    compile_model(
        policy_model(
            enabled=True,
            description="Only ssh, one way",
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "tcp",
                "ports": ["22"],
                "action": "accept",
                "bidirectional": False,
                "enabled": True,
            },
        )
    )
    project.deploy_resource("netbird::Policy")

    updated = rule_of(find_policy(netbird, POLICY_NAME))
    assert find_policy(netbird, POLICY_NAME)["description"] == "Only ssh, one way"
    assert updated["protocol"] == "tcp"
    assert updated["ports"] == ["22"]
    assert updated["bidirectional"] is False

    compile_model(policy_model(purged=True))
    project.deploy_resource("netbird::Policy")
    assert find_policy(netbird, POLICY_NAME) is None


def test_attributes_left_null_are_not_managed(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    Every attribute the model leaves null keeps whatever value the account holds for
    it, on the rule as much as on the policy itself — even when it was changed in the
    dashboard behind our back.  And a value the model does set is enforced.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    clients = create_group(netbird, SOURCE_GROUP)

    # A policy the model only names, next to the rule it needs to be created at all.
    compile_model(
        policy_model(
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "tcp",
                "ports": ["443"],
                "action": "accept",
            }
        )
    )
    project.deploy_resource("netbird::Policy")

    # The api does not default `enabled` to true, on the policy or on its rule.
    policy = find_policy(netbird, POLICY_NAME)
    assert policy["enabled"] is False

    netbird.put(
        f"{netbird.base_url}/policies/{policy['id']}",
        json={
            "name": POLICY_NAME,
            "description": "Written in the dashboard",
            "enabled": True,
            "rules": [
                {
                    "name": "named in the dashboard",
                    "enabled": True,
                    "sources": [clients],
                    "destinations": [all_group],
                    "protocol": "tcp",
                    "ports": ["443"],
                    "bidirectional": True,
                    "action": "accept",
                }
            ],
        },
    ).raise_for_status()

    # None of that is managed, so the deploy leaves all of it alone.
    compile_model(
        policy_model(
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "tcp",
                "ports": ["443"],
                "action": "accept",
            }
        )
    )
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)

    kept = find_policy(netbird, POLICY_NAME)
    assert kept["description"] == "Written in the dashboard"
    assert kept["enabled"] is True
    assert rule_of(kept)["name"] == "named in the dashboard"
    assert rule_of(kept)["bidirectional"] is True

    # And what the model does set is enforced, the rest of the policy untouched.
    compile_model(
        policy_model(
            rule={
                "sources": [clients],
                "destinations": [all_group],
                "protocol": "udp",
                "ports": ["53"],
                "action": "accept",
            }
        )
    )
    project.deploy_resource("netbird::Policy")

    enforced = find_policy(netbird, POLICY_NAME)
    assert rule_of(enforced)["protocol"] == "udp"
    assert rule_of(enforced)["ports"] == ["53"]
    assert enforced["description"] == "Written in the dashboard"
    assert rule_of(enforced)["name"] == "named in the dashboard"


def create_network_resource(netbird: requests.Session, all_group: str) -> dict:
    network = netbird.post(f"{netbird.base_url}/networks", json={"name": "lab"})
    network.raise_for_status()
    resource = netbird.post(
        f"{netbird.base_url}/networks/{network.json()['id']}/resources",
        json={"name": "lab-lan", "address": "10.10.0.0/24", "groups": [all_group]},
    )
    resource.raise_for_status()
    return resource.json()


def test_a_rule_pointing_at_a_network_resource(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A rule may point at a network resource rather than at a group, and the api refuses
    to be given both: having the group key in the body is enough, an empty list next to
    a resource is rejected just the same.

    So the target the model names for an end replaces the one the account holds,
    whichever kind that is, and an end the model names nothing for is left alone.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    clients = create_group(netbird, SOURCE_GROUP)
    resource = create_network_resource(netbird, all_group)
    target = {"id": resource["id"], "type": resource["type"]}

    created = netbird.post(
        f"{netbird.base_url}/policies",
        json={
            "name": POLICY_NAME,
            "enabled": True,
            "rules": [
                {
                    "name": "lab",
                    "enabled": True,
                    "sources": [clients],
                    "destinationResource": target,
                    "protocol": "all",
                    "bidirectional": True,
                    "action": "accept",
                }
            ],
        },
    )
    created.raise_for_status()

    # The model names no destination, so the resource the rule points at is carried
    # along rather than dropped — and the api would refuse the body if it were sent
    # next to an empty destinations list.
    compile_model(
        policy_model(rule={"sources": [clients], "protocol": "all", "action": "accept"})
    )
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)

    kept = rule_of(find_policy(netbird, POLICY_NAME))
    assert kept["destinationResource"]["id"] == resource["id"]

    # Naming the resource the account already holds adopts the rule as it is.
    resource_rule = {
        "sources": [clients],
        "destinationResource": target,
        "protocol": "all",
        "action": "accept",
    }
    compile_model(policy_model(rule=resource_rule))
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)

    # Naming groups rewrites the rule: the groups win, and the resource target goes.
    group_rule = {
        "sources": [clients],
        "destinations": [all_group],
        "protocol": "all",
        "action": "accept",
    }
    compile_model(policy_model(rule=group_rule))
    project.deploy_resource("netbird::Policy")

    rewritten = rule_of(find_policy(netbird, POLICY_NAME))
    assert group_ids(rewritten, "destinations") == [all_group]
    assert rewritten.get("destinationResource") is None

    # And back: the resource the model names replaces the groups the account holds.
    compile_model(policy_model(rule=resource_rule))
    project.deploy_resource("netbird::Policy")

    back = rule_of(find_policy(netbird, POLICY_NAME))
    assert back["destinationResource"] == target
    assert back["destinations"] is None
    assert group_ids(back, "sources") == [clients]

    compile_model(policy_model(rule=resource_rule))
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)


def test_a_rule_created_towards_a_network_resource(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A rule is created pointing at a network resource of the model, whose id and type
    are read from the facts its resource publishes.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    clients = create_group(netbird, SOURCE_GROUP)
    resource = create_network_resource(netbird, all_group)

    model = f"""
        lan = netbird::NetworkResource(
            api=api,
            _network={json.dumps(resource["id"])},
            name={json.dumps(resource["name"])},
            address={json.dumps(resource["address"])},
        )
        policy = netbird::Policy(
            api=api,
            name={json.dumps(POLICY_NAME)},
            rule=netbird::PolicyRule(
                sources={json.dumps([clients])},
                destinationResource=netbird::PolicyRuleDestinationResource(
                    id=lan.id,
                    type=std::create_fact_reference(lan, "type"),
                ),
                protocol="all",
                action="accept",
            ),
        )
    """

    # The facts of the network resource are seeded before the compile that builds the
    # references to them, which is the only place the test runner reads them from.
    compile_model(model)
    lan = project.get_resource("netbird::NetworkResource")
    project.add_fact(lan.id.resource_str(), "id", resource["id"])
    project.add_fact(lan.id.resource_str(), "type", resource["type"])

    compile_model(model)
    project.deploy_resource("netbird::Policy")

    rule = rule_of(find_policy(netbird, POLICY_NAME))
    assert rule["destinationResource"] == {"id": resource["id"], "type": "subnet"}
    assert rule["destinations"] is None
    assert group_ids(rule, "sources") == [clients]

    compile_model(model)
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)


@pytest.mark.parametrize("end", ["source", "destination"])
def test_a_rule_naming_groups_and_a_resource_for_one_end(
    project: pytest_inmanta.plugin.Project, end: str
) -> None:
    """
    The api refuses a rule naming groups and a resource for the same end, so does the
    model.  The compiler refuses it on its own, no server needed.
    """
    model = policy_model(
        rule={
            f"{end}s": ["some-group"],
            f"{end}Resource": {"id": "some-resource", "type": "subnet"},
        }
    )
    with pytest.raises(inmanta.ast.CompilerException, match=f"either {end} groups"):
        project.compile(
            "import netbird\n"
            'api = netbird::Api(agent_name="netbird", '
            'management_url="http://localhost", api_token="token")\n' + model
        )


def test_the_default_policy_is_adopted_rather_than_duplicated(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    A fresh account already holds a policy named `Default`, allowing everything between
    the peers of its `All` group.  A model naming it manages that one rather than
    creating a second policy — the api would take one, it allows duplicate names.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    default = find_policy(netbird, "Default")
    assert default is not None

    compile_model(
        "policy = netbird::Policy(\n"
        "    api=api,\n"
        '    name="Default",\n'
        "    enabled=false,\n"
        f"    rule=netbird::PolicyRule(sources={json.dumps([all_group])}, "
        f'destinations={json.dumps([all_group])}, protocol="all", action="accept"),\n'
        ")\n"
    )
    project.deploy_resource("netbird::Policy")

    assert len([p for p in get(netbird, "policies") if p["name"] == "Default"]) == 1
    adopted = find_policy(netbird, "Default")
    assert adopted["id"] == default["id"]
    assert adopted["enabled"] is False


def test_a_policy_without_a_rule_in_the_model(
    project: pytest_inmanta.plugin.Project,
    compile_model: Compile,
    netbird: requests.Session,
) -> None:
    """
    The rule of a policy is optional in the model, like every other value: a model
    that doesn't declare one manages the policy itself and leaves its rule alone.
    """
    all_group = group_id(netbird, DEFAULT_GROUP)
    default = find_policy(netbird, "Default")

    compile_model(
        "policy = netbird::Policy(\n"
        "    api=api,\n"
        '    name="Default",\n'
        '    description="Managed by inmanta",\n'
        ")\n"
    )
    project.deploy_resource("netbird::Policy")

    kept = find_policy(netbird, "Default")
    assert kept["description"] == "Managed by inmanta"
    # The rule the account created with the policy is untouched, sources and all: the
    # api requires the rules on every write, and what it gets back is what it holds.
    assert rule_of(kept)["id"] == rule_of(default)["id"]
    assert group_ids(rule_of(kept), "sources") == [all_group]
    assert group_ids(rule_of(kept), "destinations") == [all_group]
    assert rule_of(kept)["protocol"] == "all"

    compile_model(
        "policy = netbird::Policy(\n"
        "    api=api,\n"
        '    name="Default",\n'
        '    description="Managed by inmanta",\n'
        ")\n"
    )
    project.deploy_resource("netbird::Policy", change=const.Change.nochange)
