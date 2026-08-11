# inmanta-module-netbird

[![pypi version](https://img.shields.io/pypi/v/inmanta-module-netbird.svg)](https://pypi.python.org/pypi/inmanta-module-netbird/)
[![build status](https://img.shields.io/github/actions/workflow/status/edvgui/inmanta-module-netbird/continuous-integration.yml)](https://github.com/edvgui/inmanta-module-netbird/actions)

This package is an adapter that is meant to be used with the inmanta orchestrator: https://docs.inmanta.com

## Features

This module allows to manage [netbird](https://netbird.io) resources, through the netbird management api.

The module is a work in progress, it currently only contains the base entities every
resource of this module builds upon:
1. `netbird::Api`: the endpoint and credentials used to reach the netbird management api.
2. `netbird::ResourceABC`: the base entity for all the resources managed by this module.

## Example

```
import netbird

api = netbird::Api(
    agent_name="netbird",
    server_url="https://api.netbird.io",
    token=std::get_env("NETBIRD_TOKEN"),
)
```

Find more examples in the `tests` folder of this module!

## Development

```sh
python3 -m venv .venv
source .venv/bin/activate
make install
pytest tests
```
