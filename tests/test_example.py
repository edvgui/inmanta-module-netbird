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
import pathlib
import subprocess

import inmanta_plugins.files
import pytest
import pytest_inmanta.plugin
import requests
from conftest import CONTAINER_PREFIX, facts, pasta_network, run_container, wait_until
from test_peer import (
    NETBIRD_CLIENT_IMAGE,
    PEER_HOST,
    PEER_REGISTRATION_TIMEOUT,
    find_peer,
)

import inmanta.plugins
from inmanta import const

CLIENT_HOSTNAME = "lab-gateway"
SETUP_KEY_NAME = "lab-gateways"

# The template the environment file is rendered from, which the project running this
# example provides.  The setup key is created inside the template, by the same
# std::create_fact_reference that netbird::SetupKey._key is built from: files::jinja
# takes its arguments as ``object``, and the dsl refuses to pass a reference there, so
# the reference can not be handed in from the model.
ENV_TEMPLATE = """NB_SETUP_KEY={{ setup_key | std.create_fact_reference("key") }}
NB_MANAGEMENT_URL={{ management_url }}
"""


def update_example(name: str, block: str) -> None:
    """
    Find the example with the given name in the readme, and make sure the block it
    shows is the one this test used.  The readme can not drift away from something that
    works that way.
    """
    readme_file = pathlib.Path(__file__).parent.parent / "README.md"
    readme = readme_file.read_text()

    marker_start = f"<x-example-{name}>"
    start = readme.find(marker_start)
    if start == -1:
        raise RuntimeError(
            f"Can not find marker {marker_start} in readme {readme_file}"
        )

    marker_end = f"</x-example-{name}>"
    end = readme.find(marker_end, start)
    if end == -1:
        raise RuntimeError(f"Can not find marker {marker_end} in readme {readme_file}")

    current = readme[start : end + len(marker_end)]
    desired = marker_start + "\n\n```\n" + block + "\n```\n\n" + marker_end

    if current != desired:
        readme_file.write_text(
            readme[:start] + desired + readme[end + len(marker_end) :]
        )


@contextlib.contextmanager
def netbird_client(
    netbird: requests.Session,
    env_file: pathlib.Path,
    hostname: str,
) -> collections.abc.Iterator[None]:
    """
    Run the netbird client the example describes, and stop it again afterwards.

    The environment file this reads is the one the deploy just wrote, key included: what
    the container consumes is the artifact the model produced, not a copy of it.  Only
    the management url is overridden, because the address the api is reached on from the
    container's own network namespace is not the one the handler uses from the host, and
    an explicit ``-e`` wins over ``--env-file``.
    """
    container_id = run_container(
        f"{CONTAINER_PREFIX}-client-{hostname}",
        [
            # A namespace of its own, plus what it takes to set up the wireguard
            # interface: the same reasons as every other container in this suite.
            "--network",
            pasta_network({}),
            "--cap-add",
            "NET_ADMIN",
            "--device",
            "/dev/net/tun",
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
            "NB_MANAGEMENT_URL="
            + netbird.management_url.replace("127.0.0.1", PEER_HOST),
            NETBIRD_CLIENT_IMAGE,
        ],
    )
    try:
        wait_until(
            lambda: find_peer(netbird, hostname) is not None,
            container_id,
            f"the netbird client did not register itself as {hostname}",
            timeout=PEER_REGISTRATION_TIMEOUT,
        )
        yield
    finally:
        subprocess.run(["podman", "rm", "-f", container_id], capture_output=True)


def client_model(management_url: str, home: pathlib.Path, user: str) -> str:
    """
    The model the readme shows, with the values the test needs to deploy it for real
    substituted in.

    Everything runs rootless: the container is owned by an unprivileged user, its quadlet
    unit is a user unit, and it is driven with ``systemctl --user``.
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

        # The client reports this as its hostname when it registers, and the hostname
        # is what identifies a peer here: the api offers no way to change it, while the
        # name of a peer is one of the values this model rewrites.
        hostname = "{CLIENT_HOSTNAME}"

        # Everything below runs rootless, as this unprivileged user.  It needs access to
        # /dev/net/tun, and `loginctl enable-linger` on it, so that its units keep
        # running while it is not logged in.
        user = "{user}"

        # The token the client registers with.  The api generates it and the model
        # never sees the value: it is published as a fact when the key is created.
        setup_key = netbird::SetupKey(
            api=api,
            name="{SETUP_KEY_NAME}",
            type="reusable",
            expires_in=86400,
        )

        config_dir = files::Directory(
            host=host,
            path="{home}/.config/netbird",
            owner=user,
            create_parents=true,
        )

        # The key can not go through podman::Container.env: the quadlet file is rendered
        # at compile time, where the key is still a reference and not a string.  It goes
        # through an environment file instead, whose content stays a reference until the
        # agent writes it on the host.
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
            # A fact reference is not a dependency: the key has to exist, and its fact
            # to be published, before the agent can resolve it here.
            requires=setup_key,
        )

        # The netbird client itself.  NET_ADMIN and /dev/net/tun are what it takes to
        # set up the wireguard interface.
        client = podman::Container(
            host=host,
            owner=user,
            name="netbird",
            hostname=hostname,
            image="docker.io/netbirdio/netbird:latest",
            env_file=env_file.path,
            add_capability=["NET_ADMIN"],
            add_device=["/dev/net/tun"],
            requires=env_file,
        )

        # podman::Container is not a resource of its own: it is rendered into a quadlet
        # unit, and that unit file is what gets deployed.
        service = podman::services::SystemdContainer(
            container=client,
            state="running",
            enabled=true,
            quadlet=true,
            systemd_unit_dir="{home}/.config/systemd/user",
            systemd_container_dir="{home}/.config/containers/systemd",
            systemctl_command=["systemctl", "--user"],
        )

        # And the peer the client registered.  This resource adopts a peer rather than
        # creating one, so it only deploys once the client has joined the account:
        # before that it skips, saying so.
        netbird::Peer(
            api=api,
            hostname=hostname,
            ssh_enabled=false,
            requires=service.resources,
        )
    """


def test_netbird_client(
    project: pytest_inmanta.plugin.Project,
    netbird: requests.Session,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Run a netbird client in a container and adopt the peer it registers.

    A peer can not be created through the api, so the only way to get one is to run a
    client that registers itself.  What this test drives is the hand-off that makes that
    possible: the key the module creates is published as a fact, the reference to it is
    resolved on the agent, and the value the client registers with lands in the
    environment file on the host.

    The systemd resources of the service are deliberately left undeployed: the podman
    module runs ``systemctl --user daemon-reload`` and enables and starts the unit on a
    unit file change, which is not this test's business to do on the machine it runs on.
    """
    monkeypatch.setenv("NETBIRD_TOKEN", netbird.token)

    # The model runs rootless, as the user running the test: it is the only one whose
    # files this test may chown to, and whose home it may write in.
    user = getpass.getuser()

    template_dir = pathlib.Path(project._test_project_dir, "templates")
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "netbird-client.env.j2").write_text(ENV_TEMPLATE)

    model = client_model(netbird.management_url, tmp_path, user)
    project.compile(model, no_dedent=False)

    # The key reaches the environment file as a reference: the desired state carries the
    # reference, never the secret itself.
    env_file = project.get_instances("files::TextFile").pop()
    content = inmanta.plugins.allow_reference_values(env_file).content
    assert isinstance(content, inmanta_plugins.files.JinjaReference)

    # The container is not a resource, the quadlet unit file it renders into is, and
    # that is what points the client at the environment file.
    assert project.get_resource("podman::Container") is None
    quadlet = next(
        r
        for r in project.resources.values()
        if str(getattr(r, "path", "")).endswith("netbird.container")
    )
    assert f"EnvironmentFile={tmp_path}/.config/netbird/client.env" in quadlet.content
    assert "Image=docker.io/netbirdio/netbird:latest" in quadlet.content
    assert f"HostName={CLIENT_HOSTNAME}" in quadlet.content
    assert "AddCapability=NET_ADMIN" in quadlet.content

    # Deploying the key creates it on the account and publishes its value as a fact.
    project.deploy_resource("netbird::SetupKey")
    key = facts(project)["key"]
    assert "*" not in key

    # std::create_fact_reference snapshots the fact store at compile time, so the fact
    # has to be seeded before the compile that builds the reference the deploy resolves.
    setup_key_resource = project.get_resource("netbird::SetupKey")
    project.add_fact(setup_key_resource.id.resource_str(), "key", key)
    project.compile(model, no_dedent=False)

    # The environment file is written with the key the api generated, resolved on the
    # agent.  This hand-off is what the whole example exists for.
    project.deploy_resource(
        "files::Directory", path=str(tmp_path / ".config" / "netbird")
    )
    project.deploy_resource("files::TextFile")
    env_file_path = tmp_path / ".config" / "netbird" / "client.env"
    written = env_file_path.read_text()
    # Jinja does not keep the trailing newline of the template.
    assert written == (
        f"NB_SETUP_KEY={key}\nNB_MANAGEMENT_URL={netbird.management_url}"
    )

    # No client has registered yet, so there is no peer to adopt: the resource skips
    # rather than reporting a desired state it did not reach.
    project.deploy_resource("netbird::Peer", status=const.ResourceState.skipped)

    # Run the client on the environment file that was just written.  Once it has joined
    # the account there is a peer to adopt, and the resource converges.
    with netbird_client(netbird, env_file_path, CLIENT_HOSTNAME):
        project.deploy_resource("netbird::Peer")

        peer = find_peer(netbird, CLIENT_HOSTNAME)
        assert peer is not None
        # The peer the model asked for, on the peer the key registered.
        assert peer["ssh_enabled"] is False
        assert facts(project)["id"] == peer["id"]

        # And a second deploy of the same desired state changes nothing.
        project.deploy_resource("netbird::Peer", change=const.Change.nochange)

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
