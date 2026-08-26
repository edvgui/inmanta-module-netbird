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
import contextlib
import getpass
import json
import pathlib
import subprocess

import inmanta_plugins.files
import pytest
import pytest_inmanta.plugin
import requests
from conftest import (
    CONTAINER_PREFIX,
    facts,
    get,
    peer_network,
    ping,
    run_container,
    underlay_address,
    update_example,
    wait_until,
)
from test_peer import (
    NETBIRD_CLIENT_IMAGE,
    PEER_CAPABILITIES,
    PEER_REGISTRATION_TIMEOUT,
    find_peer,
)

import inmanta.plugins
from inmanta import const

# The two gateways the example runs, in the order of the bridge networks they run in.
CLIENT_HOSTNAMES = ["lab-gateway-a", "lab-gateway-b"]
SETUP_KEY_NAME = "lab-gateways"
GROUP_NAME = "lab-gateways"
NAMESERVER_GROUP_NAME = "lab-dns"
DNS_DOMAIN = "lab.example.com"

# How long to give the two peers to reach each other over the overlay.  The client sets
# a connection up lazily, when there is traffic for the other end, and it has to go
# through the relay here.
PEER_CONNECTION_TIMEOUT = 120.0

# The template the environment file is rendered from, which the project running this
# example provides.  The setup key is created inside the template, by the same
# std::create_fact_reference that netbird::SetupKey._key is built from: files::jinja
# takes its arguments as ``object``, and the dsl refuses to pass a reference there, so
# the reference can not be handed in from the model.
ENV_TEMPLATE = """NB_SETUP_KEY={{ setup_key | std.create_fact_reference("key") }}
NB_MANAGEMENT_URL={{ management_url }}
"""


@contextlib.contextmanager
def netbird_client(
    netbird: requests.Session,
    env_file: pathlib.Path,
    hostname: str,
    network: str,
) -> collections.abc.Iterator[str]:
    """
    Run one of the netbird clients the example describes, stop it again afterwards, and
    yield the name of the container running it.

    The environment file this reads is the one the deploy just wrote, key included: what
    the container consumes is the artifact the model produced, not a copy of it.  Only
    the management url is overridden, because the address the api is reached on from the
    container's own bridge network is not the one the handler uses from the host, and an
    explicit ``-e`` wins over ``--env-file``.

    :param env_file: The environment file the deploy wrote, holding the setup key.
    :param hostname: The hostname the client registers itself under.
    :param network: The bridge network to run the client in.
    """
    container = f"{CONTAINER_PREFIX}-client-{hostname}"
    run_container(
        container,
        [
            # A bridge of its own, which the server joined too: the client reaches the
            # api by name, and nothing else.
            "--network",
            network,
            *PEER_CAPABILITIES,
            # A uts namespace of its own, which is what makes the hostname settable:
            # podman refuses --hostname in the host uts namespace, and that is the
            # default in the container the ci job runs in.
            "--uts",
            "private",
            "--hostname",
            hostname,
            "--env-file",
            str(env_file),
            "-e",
            f"NB_MANAGEMENT_URL={netbird.peer_management_url}",
            NETBIRD_CLIENT_IMAGE,
        ],
    )
    try:
        wait_until(
            lambda: find_peer(netbird, hostname) is not None,
            container,
            f"the netbird client did not register itself as {hostname}",
            timeout=PEER_REGISTRATION_TIMEOUT,
        )
        yield container
    finally:
        subprocess.run(["podman", "rm", "-f", container], capture_output=True)


def client_model(
    management_url: str,
    home: pathlib.Path,
    user: str,
) -> str:
    """
    The model the readme shows, with the values the test needs to deploy it for real
    substituted in.

    Everything runs rootless: the containers are owned by an unprivileged user, their
    quadlet units are user units, and they are driven with ``systemctl --user``.
    """
    return f"""
        import files
        import mitogen
        import netbird
        import podman
        import podman::services
        import std

        api = netbird::Api(
            agent_name="netbird",
            management_url="{management_url}",
            # A reference, not std::get_env: the token stays out of the desired state
            # and is resolved on the agent, at deploy time.
            api_token=std::create_environment_reference("NETBIRD_TOKEN"),
        )

        host = std::Host(
            name="localhost",
            os=std::linux,
            via=mitogen::Local(),
        )

        # Each client reports its own hostname when it registers, and the hostname is
        # what identifies a peer here: the api offers no way to change it, while the
        # name of a peer is one of the values this model rewrites.  Two gateways, so
        # that there is an overlay to speak of: once both have joined the account they
        # reach each other over it, wherever they sit on the underlay.
        hostnames = {json.dumps(CLIENT_HOSTNAMES)}

        # Everything below runs rootless, as this unprivileged user.  It needs access to
        # /dev/net/tun, and `loginctl enable-linger` on it, so that its units keep
        # running while it is not logged in.
        user = "{user}"

        # The group the two gateways end up in.  Its `peers` are left null, so the
        # model does not manage them: the clients join by registering with the key
        # below, which is what the key's auto groups do.
        gateways = netbird::Group(
            api=api,
            name="{GROUP_NAME}",
        )

        # The token both clients register with.  The api generates it and the model
        # never sees the value: it is published as a fact when the key is created.  A
        # reusable key, since more than one client joins with it.
        setup_key = netbird::SetupKey(
            api=api,
            name="{SETUP_KEY_NAME}",
            type="reusable",
            expires_in=86400,
            # The id of a group the model never reads either: it is a reference on the
            # fact the group's own resource publishes.  Every peer registering with
            # this key lands in that group.
            auto_groups=[gateways.id],
            # The api refuses an auto group it doesn't know, so the group has to be
            # there first.  A reference is not a dependency of its own.
            requires=gateways,
        )

        # The dns the gateways resolve the lab's own domain with.  The api wants one to
        # three servers, at least one group to distribute them to, and either the
        # primary flag or a domain — never both, never neither.
        netbird::NameserverGroup(
            api=api,
            name="{NAMESERVER_GROUP_NAME}",
            enabled=true,
            groups=[gateways.id],
            domains=["{DNS_DOMAIN}"],
            nameservers=[
                netbird::Nameserver(ip="9.9.9.9", ns_type="udp", port=53),
                netbird::Nameserver(ip="1.1.1.1", ns_type="udp", port=53),
            ],
            requires=gateways,
        )

        # And netbird resolves for every peer of the account: no group opts out of it.
        # These settings are a singleton the api creates with the account, so this
        # resource only ever updates them — purging it is an error.
        netbird::DnsSettings(
            api=api,
            disabled_management_groups=[],
        )

        for hostname in hostnames:
            # One configuration directory per client: they share the account and the
            # key, nothing else.
            config_dir = files::Directory(
                host=host,
                path=f"{home}/.config/netbird/{{hostname}}",
                owner=user,
                create_parents=true,
            )

            # The key can not go through podman::Container.env: the quadlet file is
            # rendered at compile time, where the key is still a reference and not a
            # string.  It goes through an environment file instead, whose content stays
            # a reference until the agent writes it on the host.
            env_file = files::TextFile(
                host=host,
                # No need to require the directory, the files exporter wires that up.
                path=f"{{config_dir.path}}/client.env",
                content=files::jinja(
                    "template:///netbird-client.env.j2",
                    setup_key=setup_key,
                    management_url=api.management_url,
                ),
                owner=user,
                # The key is a secret: only its owner gets to read it.
                permissions=600,
                # A fact reference is not a dependency: the key has to exist, and its
                # fact to be published, before the agent can resolve it here.
                requires=setup_key,
            )

            # The netbird client itself.  NET_ADMIN, NET_RAW and /dev/net/tun are what
            # it takes to bring the wireguard interface up.
            client = podman::Container(
                host=host,
                owner=user,
                name=hostname,
                hostname=hostname,
                image="docker.io/netbirdio/netbird:latest",
                env_file=env_file.path,
                add_capability=["NET_ADMIN", "NET_RAW"],
                add_device=["/dev/net/tun"],
                requires=env_file,
            )

            # podman::Container is not a resource of its own: it is rendered into a
            # quadlet unit, and that unit file is what gets deployed.
            service = podman::services::SystemdContainer(
                container=client,
                state="running",
                enabled=true,
                quadlet=true,
                systemd_unit_dir="{home}/.config/systemd/user",
                systemd_container_dir="{home}/.config/containers/systemd",
                systemctl_command=["systemctl", "--user"],
            )

            # And the peer the client registered.  This resource adopts a peer rather
            # than creating one, so it only deploys once the client has joined the
            # account: before that it skips, saying so.
            netbird::Peer(
                api=api,
                hostname=hostname,
                ssh_enabled=false,
                requires=service.resources,
            )
        end
    """


def test_netbird_client(
    project: pytest_inmanta.plugin.Project,
    netbird: requests.Session,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Run two netbird clients in containers, adopt the peers they register, and check
    that they reach each other over the overlay.

    A peer can not be created through the api, so the only way to get one is to run a
    client that registers itself.  What this test drives is the hand-off that makes that
    possible: the key the module creates is published as a fact, the reference to it is
    resolved on the agent, and the value the clients register with lands in the
    environment files on the host.

    The two clients run on bridge networks the api container joined and that are
    isolated from each other, so there is no path between them on the underlay: the
    ping that goes through is the overlay the account gives them, relayed by the server.

    The systemd resources of the services are deliberately left undeployed: the podman
    module runs ``systemctl --user daemon-reload`` and enables and starts the units on a
    unit file change, which is not this test's business to do on the machine it runs on.
    So are the dns resources of the example: they need neither a client nor an overlay,
    and ``tests/test_dns.py`` deploys them against the same api.  They are part of the
    model because the readme shows a whole account, not because this test drives them.
    """
    monkeypatch.setenv("NETBIRD_TOKEN", netbird.token)

    # The model runs rootless, as the user running the test: it is the only one whose
    # files this test may chown to, and whose home it may write in.
    user = getpass.getuser()

    template_dir = pathlib.Path(project._test_project_dir, "templates")
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "netbird-client.env.j2").write_text(ENV_TEMPLATE)

    config_dirs = {
        hostname: tmp_path / ".config" / "netbird" / hostname
        for hostname in CLIENT_HOSTNAMES
    }
    env_files = {
        hostname: config_dir / "client.env"
        for hostname, config_dir in config_dirs.items()
    }

    model = client_model(netbird.management_url, tmp_path, user)
    project.compile(model, no_dedent=False)

    # The key reaches both environment files as a reference: the desired state carries
    # the reference, never the secret itself.
    assert len(project.get_instances("files::TextFile")) == len(CLIENT_HOSTNAMES)
    for env_file in project.get_instances("files::TextFile"):
        content = inmanta.plugins.allow_reference_values(env_file).content
        assert isinstance(content, inmanta_plugins.files.JinjaReference)

    # The containers are not resources, the quadlet unit files they render into are, and
    # those are what point the clients at their environment files.
    assert project.get_resource("podman::Container") is None
    for hostname in CLIENT_HOSTNAMES:
        quadlet = next(
            r
            for r in project.resources.values()
            if str(getattr(r, "path", "")).endswith(f"{hostname}.container")
        )
        assert f"EnvironmentFile={env_files[hostname]}" in quadlet.content
        assert "Image=docker.io/netbirdio/netbird:latest" in quadlet.content
        assert f"HostName={hostname}" in quadlet.content
        assert "AddCapability=NET_ADMIN" in quadlet.content
        assert "AddCapability=NET_RAW" in quadlet.content

    # The group comes first: the setup key's auto groups and the nameserver group both
    # point at its id, and the api refuses a group id it doesn't know.
    project.deploy_resource("netbird::Group")
    group_resource = project.get_resource("netbird::Group")
    group_id = facts(project)["id"]

    # std::create_fact_reference snapshots the fact store at compile time, so a fact has
    # to be seeded before the compile that builds the reference the deploy resolves.
    project.add_fact(group_resource.id.resource_str(), "id", group_id)
    project.compile(model, no_dedent=False)

    # Deploying the key creates it on the account and publishes its value as a fact.
    # Its auto groups resolved to the id the group's own resource published — the model
    # fed one resource from another without ever knowing the value.
    project.deploy_resource("netbird::SetupKey")
    key = facts(project)["key"]
    assert "*" not in key
    setup_key = next(
        k for k in get(netbird, "setup-keys") if k["name"] == SETUP_KEY_NAME
    )
    assert setup_key["auto_groups"] == [group_id]

    setup_key_resource = project.get_resource("netbird::SetupKey")
    project.add_fact(setup_key_resource.id.resource_str(), "key", key)
    project.compile(model, no_dedent=False)

    # The environment files are written with the key the api generated, resolved on the
    # agent.  This hand-off is what the whole example exists for.
    for hostname in CLIENT_HOSTNAMES:
        project.deploy_resource("files::Directory", path=str(config_dirs[hostname]))
        project.deploy_resource("files::TextFile", path=str(env_files[hostname]))
        # Jinja does not keep the trailing newline of the template.
        assert env_files[hostname].read_text() == (
            f"NB_SETUP_KEY={key}\nNB_MANAGEMENT_URL={netbird.management_url}"
        )

        # No client has registered yet, so there is no peer to adopt: the resource skips
        # rather than reporting a desired state it did not reach.
        project.deploy_resource(
            "netbird::Peer", hostname=hostname, status=const.ResourceState.skipped
        )

    # Run the clients on the environment files that were just written, each on a bridge
    # network of its own.  Once they have joined the account there are peers to adopt,
    # and the resources converge.
    with contextlib.ExitStack() as clients:
        containers = {
            hostname: clients.enter_context(
                netbird_client(
                    netbird,
                    env_files[hostname],
                    hostname,
                    peer_network(index),
                )
            )
            for index, hostname in enumerate(CLIENT_HOSTNAMES)
        }

        for hostname in CLIENT_HOSTNAMES:
            project.deploy_resource("netbird::Peer", hostname=hostname)

            peer = find_peer(netbird, hostname)
            assert peer is not None
            # The peer the model asked for, on the peer the key registered.
            assert peer["ssh_enabled"] is False
            assert facts(project)["id"] == peer["id"]

            # And a second deploy of the same desired state changes nothing.
            project.deploy_resource(
                "netbird::Peer", hostname=hostname, change=const.Change.nochange
            )

        peers = {peer["hostname"]: peer for peer in get(netbird, "peers")}

        # Both clients registered with the key, so both peers are in the group its auto
        # groups named — the model never listed them there, and it could not have: it
        # does not know the ids the api handed out.
        group = next(g for g in get(netbird, "groups") if g["name"] == GROUP_NAME)
        assert sorted(member["id"] for member in group["peers"]) == sorted(
            peer["id"] for peer in peers.values()
        )

        # The two gateways reach each other over the overlay the account gives them.
        # The client sets that connection up on the first packet, and it has to go
        # through the server's relay, so give it a moment.
        source, destination = CLIENT_HOSTNAMES
        wait_until(
            lambda: ping(containers[source], peers[destination]["ip"]),
            containers[source],
            f"{source} could not reach {destination} over the overlay",
            timeout=PEER_CONNECTION_TIMEOUT,
        )

        # And there is no other way for them to reach each other: their bridges are
        # isolated, so the underlay address of one is unreachable from the other.  The
        # container itself answers on that address, which is what makes the failure
        # below say something about the isolation rather than about the address.
        underlay = underlay_address(containers[destination])
        assert ping(containers[destination], underlay)
        assert not ping(
            containers[source], underlay
        ), "the two bridge networks are not isolated, the ping above proves nothing"

    tested_model = pathlib.Path(project._test_project_dir, "main.cf").read_text()
    # The readme shows the home of a dedicated unprivileged user rather than the
    # throwaway one this test deployed into.
    tested_model = tested_model.replace(str(tmp_path), "/home/netbird")
    tested_model = tested_model.replace(f'user = "{user}"', 'user = "netbird"')
    tested_model = tested_model.replace(
        netbird.management_url, "https://api.netbird.io"
    )
    update_example("netbird-client", tested_model)
    update_example("netbird-client-template", ENV_TEMPLATE.rstrip("\n"))
