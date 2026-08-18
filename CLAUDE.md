# CLAUDE.md — netbird module map

Orientation for working in this repo.  Keep it accurate and short.

## Layout

```
model/_init.cf                      Api, ResourceABC, JsonObjectABC, and one entity per api object
inmanta_plugins/netbird/__init__.py Exporter base, handler base, and one resource+handler per object
inmanta_plugins/netbird/helpers.py  Session (base url, secret redaction, GET cache, retries)
tests/conftest.py                   The netbird server fixture and the test helpers
tests/test_<object>.py              One file per api object
```

## Every netbird object is a co-managed json object

The resource **is** its own desired state.  There is no separate "config" entity: one
DSL entity per api object, extending `netbird::JsonObjectABC`, whose attributes are
the keys the api reports about itself.

- An attribute left **`null` is not managed** — the object keeps whatever value the
  account holds for it.  That is what lets the module co-manage an object with
  whoever else edits it in the dashboard.  Default every optional attribute to `null`,
  never to the api's own default: a default would silently enforce a value.
- `ResourceABC` is deliberately **not** a `files::json::SerializableEntity`.
  `get_instance_attributes` returns `{}` for a parent entity type that isn't one, which
  is what keeps `purged`, `send_event`, `serialize` and `id` out of the body sent to the
  api.  **Anything you declare on `ResourceABC` is invisible to the api; anything on
  `JsonObjectABC` or below is sent.**  A model-only value on an object entity must
  therefore be named `_private` (`get_instance_attributes` skips leading underscores).
  That also applies to a value the api *generates*: `netbird::SetupKey._key` is a fact
  reference, and an unprefixed one would be serialized into the desired state, where
  the agent would have to resolve it before the create that publishes the fact ran.
- The default `_operation` is `files::merge`, under which `serialize` drops null
  attributes.  That is the mechanism behind "null is not managed" — don't set
  `operation` on these entities.
- `implement JsonObjectABC using parents, json_object` must stay: `json_object` sets
  `self.resource = self`, without which `entity.root` is never set and the exporter's
  `serialize_for_resource` returns nothing.
- A relation **between two json objects must be one-way** (`A.b [1] -- B`, not
  `-- B.as [0:]`).  A two-way one makes `get_child_instances` treat each end as a child
  of the other, and the serializer recurses forever.
- An **embedded** entity (a list of objects inside one api object, which the dsl has no
  attribute type for) is the exception: it must be two-way and the relation back to its
  parent must be named `parent`, which is the one name `get_child_instances` skips —
  that is what stops the recursion, and `get_relative_path` refuses a one-way one.
  Give it an `index (parent, <key>)`: the desired state then addresses each entry by
  that key, so the entries the model doesn't name are kept, and the order of the list
  can not be expressed by the model at all (the paths are applied sorted).

## Ids are ids, never names

The api addresses everything by opaque id.  So does the model: an attribute that points
at another object carries **the id**, and the handler does no name↔id translation.
`netbird::ResourceABC.id` exposes an object's own id as a `std::create_fact_reference`
reading the `id` fact its resource publishes, so one resource feeds another
(`auto_groups=[some_group.id]`) without the model ever knowing the value.

Every handler publishes that fact on **both** the read and the create path — a create
that didn't publish would leave the reference unresolvable until the next repair.

**pytest-inmanta does resolve a fact reference at deploy time**: `project.deploy` calls
`resource.resolve_all_references(ctx)` before the handler runs, so `resource.zone` is a
plain string by the time a crud method sees it.  It resolves from mocked facts, which it
does *not* fill with what a handler published: a test deploying an object addressed under
another one's id seeds them itself, with
`project.add_fact(parent_resource.id.resource_str(), "id", <id read off the api>)` — see
`deploy_zone` in `tests/test_dns_zone.py` and `deploy_network` in `tests/test_network.py`.

`std::create_fact_reference` **snapshots** those mocked facts into the reference at
compile time, and only when the store is non-empty already: seed the fact **before** the
compile that builds the reference.  A reference compiled with nothing seeded falls back to
a real orchestrator client and fails the deploy with a bare `KeyError: 'data'`.

## Adding a resource for another api object

1. **Probe the real api first.**  The docs at `https://docs.netbird.io/api/resources/*`
   and `shared/management/http/api/openapi.yml` in `netbirdio/netbird` are both lossy
   about what is actually required.  Start a server the way `tests/conftest.py` does and
   send the calls by hand.  Every surprise below was found that way, none of it was in
   the docs.
2. **Entity** in `model/_init.cf` (or its own `model/<object>.cf` once there are
   several): extend `JsonObjectABC`, one attribute per api key, `null` default on all
   but the identifying one, an `index (api, <identity>)`, `implement ... using parents`,
   and a link to the api documentation in the docstring.
3. **Exporter**: `@resource("netbird::X", "<identity>", "api.agent_name")` on a subclass
   of `ResourceABC`.  Keep `fields` down to the identity attribute — everything else the
   handler needs is already in `desired_state`.
4. **Handler**: subclass `HandlerABC`, implement the four crud methods.
5. `CREATE_KEYS` / `UPDATE_KEYS` constants + `select(body, KEYS)` per endpoint when the
   api doesn't take the same keys on both (it usually doesn't).

### The read/diff/write dance

```
read_resource   -> store the WHOLE object the api returned in resource.body
                   (not a narrowed one: the update needs the values the model
                    has no opinion about)
calculate_diff  -> desired.body = merged_body(current.body, desired)
                   compares diff_body(current.body) vs diff_body(desired.body)
update_resource -> PUT select(resource.body, UPDATE_KEYS)    # already merged
create_resource -> POST select(merged_body({}, resource), CREATE_KEYS)
```

Two hooks to override:

- `normalize(body)` — canonical shape, so a collection the api returns in another order
  isn't a spurious diff.  Sort every list the api doesn't order itself.
- `diff_body(body)` — the part that takes part in the diff.  **Override it with
  `select(body, UPDATE_KEYS)` whenever the api has create-only keys.**  Comparing full
  bodies makes those non-convergent: change one in the model and the diff reports a
  change on every deploy that the `PUT` can never enforce.

## Netbird api gotchas (all verified against a live server)

- `POST /api/users` requires `role`, and `email` unless `is_service_user` is true.
  `PUT /api/users/{id}` requires `role` **and** `auto_groups` on every call, even when
  neither is what you are changing — hence the merged body.  `is_blocked` is only taken
  on the `PUT`, so a user the model wants blocked from the start needs a create
  followed by an update.
- `PUT /api/peers/{id}` **replaces** the keys it takes: one left out of the body is read
  as its go zero value and written, so `{"name": "x"}` silently resets `ssh_enabled` to
  false.  It also requires `name` on every call (`500` without).  There is no
  `POST /api/peers` — a peer registers itself, so `create_resource` raises
  `SkipResource`.  `approval_required` and `extra_dns_labels` are accepted and silently
  ignored; don't model a key the api drops, it can never converge.
- A **service user has no email address**: the api stores `""` whatever you send.  Its
  `name` is the only thing left to identify it, which is why `netbird::User` is indexed
  on the name for regular and service users alike.
- `GET /api/users` unfiltered returns regular **and** service users.  `service_user` is
  a boolean query parameter; a non-boolean value is not a "give me everything" wildcard.
- A listing endpoint with nothing to list can answer json `null` instead of `[]` —
  `process_netbird_response` turns that into `[]` for a `list[...]` expected type.
- A fresh account already has an `All` group and a `Default` policy.  The api refuses to
  rename it (422) or to delete it (400).
- `GET /api/groups` reports `peers` as a list of `{id, name}` objects, but `POST`/`PUT`
  only take a list of peer ids and answer 400 on the shape the api returned itself.  An
  empty `peers`/`resources` comes back as json `null`, not `[]`, and a peer id the
  account doesn't know is dropped silently.  `PUT /api/groups/{id}` replaces the whole
  group: a key left out of it is emptied.  Its `resources` go the other way round than
  its `peers`: reported as `{id, type}` objects and only taken that way, 400 on a list of
  ids.
- `POST /api/setup-keys` requires `name` and `type`, takes everything else optionally,
  and **silently ignores `revoked`** (and any key it doesn't know) — a key the model
  wants revoked has to be created and then updated.  `type: one-off` forces
  `usage_limit` to 1 whatever you send.  Duplicate names are allowed.
- `PUT /api/setup-keys/{id}` requires `auto_groups` on every call (422 "setup key
  autogroups field is invalid" without it) and takes `revoked` optionally; every other
  key of the body, `name` included, is ignored, so echoing back the whole read body is
  harmless.  Un-revoking is a 422: revocation only goes one way.
- The api never echoes `expires_in` back, it reports the `expires` timestamp — hence
  create-only and out of `diff_body`.  `key` is in clear in the create response and
  masked (`ABCDE****`) on every read, so its fact is published on the create path
  **only**: the read would clobber it with the mask.  That is the one exception to
  "publish the fact on both paths", and it means a key this module adopted rather than
  created has no `key` fact at all.
- A setup key's `auto_groups` come back in the order they were sent, and as `[]` when
  empty (no json `null` here).  The account's `All` group is refused (422 "can't add
  'all' group to the setup key"), and so is an unknown group id — unlike a group's
  `peers`, which drop silently.
- `POST /api/networks` requires nothing at all — an empty body makes a nameless network —
  and the api takes several networks with the same name.  `PUT` replaces both keys.
- Deleting a network takes the resources and the routers it holds with it, no ordering
  needed.  The other way round matters: creating either in a network that is gone answers
  404, while listing them answers `null` / `[]`, so a child of a network that no longer
  exists reads as purged rather than failing.
- `POST`/`PUT /api/networks/{id}/resources` **require `address`** and answer `500` without
  one, or on an address they can't parse.  `type` is derived from the address and a `type`
  in the body is ignored.  `PUT` replaces: `name`, `description`, `enabled` and `groups`
  left out are emptied.  A resource name is unique within a network (422 on a duplicate).
  `groups` are reported as objects and only taken as ids (400 on the object shape), and
  the empty resource listing is json `null` while the empty router listing is `[]`.
- `POST`/`PUT /api/networks/{id}/routers` take exactly one of `peer` and `peer_groups`:
  400 `either peer or peer_groups must be provided` with neither, 400 `peer and
  peer_groups cannot be set at the same time` with both, on the update as much as on the
  create.  An *empty* one next to a filled one is fine, which is what lets the merged body
  through: the api reports `peer: ""` for a group router and `peer_groups: null` for a
  peer router.  `PUT` replaces, so an omitted `metric` becomes `0` and an omitted
  `masquerade`/`enabled` becomes `false`.  Several routers may route for the same target,
  and a peer or group id the account doesn't know is accepted silently, so nothing but the
  target identifies a router.
- `POST`/`PUT /api/dns/nameservers` take the same keys and validate the whole group on
  every call: **one to three** nameservers, at least one distribution group, and either
  `primary` or a non-empty `domains`, never both and never neither, and it refuses a
  primary group with `search_domains_enabled` rather than dropping it.  The count error
  reads "the list of nameservers should be 1 or 3" but two are accepted — only 0 and
  more than 3 are refused.  Every nameserver needs `ip`, `ns_type` (`udp` is the only
  value) **and** `port` — one missing either is "invalid ns servers format", so the
  handler completes an entry the model only named with `udp`/`53`.
- `PUT /api/dns/nameservers/{id}` **replaces**: a key left out is written as its zero
  value, and an emptied `domains` comes back as json `null`.  `enabled` is not defaulted
  to true on a create either: a group the model says nothing about is created disabled.
- The api keeps `nameservers`, `groups`, `domains` and `disabled_management_groups` in
  the order it was given them.  The handler still sorts everything but the nameservers:
  those are sets of ids and domains, while the nameserver order is simply not managed.
- `PUT /api/dns/settings` answers with the settings, and requires
  `disabled_management_groups`: without the key it answers `404 account not found`.
  There is no `POST` and no `DELETE` on it (`404 page not found`) — the settings are
  part of the account, so purging `netbird::DnsSettings` fails the deploy.
- Two primary nameserver groups on one account are accepted, the api enforces nothing
  there.
- `POST /api/dns/zones` requires a `name`, a well formed `domain` and at least one
  distribution group; a second zone on a domain the account serves is a 409, and
  changing the domain of an existing zone a 422 (`zone domain cannot be updated`).  A
  zone `PUT` ignores the `records` key and leaves the records of the zone alone, so a
  zone update can not clobber the `netbird::DnsZoneRecord`s under it.
- The dns zones live under `/api/dns/zones`, not `/api/dns-zones` (404), records under
  `/api/dns/zones/{zone}/records`.  `GET` on the records of a zone the account does not
  hold answers `200 []`, not 404, while `POST` into it answers 404: a record pointed at
  a zone that is gone reads as still to create, and only the create says why.
- A dns record takes only the types `A`, `AAAA` and `CNAME`, upper case (`a` is refused
  too), everything else is a 422 `invalid record type, must be a, aaaa, or cname`.
  `POST` and `PUT` both require `name`, `type` and `content` — the `PUT` even when
  none of them is what changes — and both reset a `ttl` left out of the body to 0.  The
  name must be fully qualified inside the zone's domain and carry no trailing dot; the
  api validates `content` against the type (ipv4/ipv6/target name) and stores name and
  content verbatim, so no normalization is needed.
- Two records may share a name and a type as long as their content differs; the api only
  refuses a record identical on all three (409).  `find_dns_zone_record` raises rather
  than guessing which of two the model meant.
- The server binds a hardcoded `:33073` for the management grpc, which no config key
  moves.  Two servers sharing a network namespace fight over it and the second one
  exits — hence the namespace per container, see below.

## The readme example is compiled and deployed by a test

`tests/test_example.py` builds the model the readme shows, compiles it, deploys the part
that is safe to deploy, and writes the result back between the `<x-example-...>` markers
in `README.md` — same mechanism as `inmanta-module-podman` and `inmanta-module-files`.
Edit the model in the test, never the readme.  What that exercise turned up:

- `podman::Container` is **not a resource**.  It is rendered into a quadlet unit file by
  `podman::services::SystemdContainer`, and that file is what deploys, next to the
  `exec::Run` resources doing `systemctl daemon-reload`/`enable`/`start`.  A model that
  only declares the container exports nothing, and a `requires` on it silently drops
  ("had requirements before flattening, but not after").
- **A reference can not be handed to a plugin declaring `object`.**  `files::jinja` takes
  `**kwargs: object`, and the dsl refuses a reference there.  Pass the *entity* and build
  the reference inside the template with the filter form,
  `{{ setup_key | std.create_fact_reference("key") }}` — the plugins are registered as
  jinja filters, not as globals, so `std.create_fact_reference(...)` as a call is
  `'std' is undefined`.
- **`podman::Container.env` can not carry a reference at all**: the quadlet template
  concatenates the values at compile time and dies with `can only concatenate str (not
  "FactReference") to str`.  An environment file whose content is a reference is the way
  through.
- Jinja drops the template's trailing newline, so the rendered file has none.
- `project.deploy_resource("<type>")` takes the **first** resource of that type; pass a
  filter (`path=...`) when the model holds several.
- A container that needs `--hostname` also needs **`--uts private`**.  The ci job's
  podman defaults to the host uts namespace, where `--hostname` is refused outright:
  `cannot set hostname when running in the host UTS namespace`.  It works without it
  locally, so this only ever shows up in ci.
- Never start a container with `check=True` and `capture_output=True` alone: the
  `CalledProcessError` carries the exit code and throws podman's message away, which is
  the only thing that explains a failure happening somewhere other than this machine.

## Testing

```sh
source .venv/bin/activate
pytest tests
```

- `tests/conftest.py` boots a real `netbirdio/netbird-server` (the combined single-node
  image: management + signal + relay + stun) in podman, **once per test**, so every test
  sees an empty account — that restart is the whole cleanup story.  Tests are skipped
  when podman is missing.
- The account is set up through `POST /api/setup` with `NB_SETUP_PAT_ENABLED=true`,
  which mints the PAT the tests drive the api with.  No IdP, no dashboard (netbird ≥
  0.62 has built-in local users).
- **Several copies of the suite can run next to each other on one host**, which is what
  every container getting a network namespace of its own buys.  Never `--network host`:
  the servers would collide on `:33073` and the peer clients on their wireguard
  interface.  Give a new container its `--network` from `pasta_network()`.
- Forwarding is `pasta:--tcp-ports,<host>:<container>`, **not `--publish`**: podman's own
  port forwarder silently does not forward when rootless podman itself runs inside a
  container, which is exactly the CI setup.  Verified both ways in
  `quay.io/podman/stable`.
- The api port is the same number inside the container and on the host, because
  `exposedAddress` (the url peers are told to come back on) has to be valid in both.
  Every other port the server binds keeps its default: they are namespace-private now.
- The api port comes from `free_port()` (bind `:0`, read it back, close), so another
  process can take it in the gap.  `start_server` retries `SERVER_START_ATTEMPTS` times
  on a fresh port.  The container runs **without `--rm`** on purpose: a server that dies
  on startup would otherwise take its logs with it, and those logs are all `wait_until`
  can report.
- Helpers are plain functions, not fixtures: `get(netbird, path)`, `facts(project)`.
  `compile_model` is a fixture because it closes over both `project` and `netbird`.
- Build test models with `json.dumps` — json literals are valid dsl for the primitive
  values a netbird object is made of, so tests pass plain python values.

## Lint, types, CI

- `make format` (isort, black, flake8, pyupgrade) and `make pep8`.  **Black uses its
  default line length of 88** — `setup.cfg`'s `[black] line-length=128` is not read by
  black, only flake8's `max-line-length` is.
- `make mypy-plugins` must pass, it is a CI step.  `setup.cfg` sets
  `follow_untyped_imports` for `inmanta_plugins.files.*` (no `py.typed` marker yet) —
  don't replace it with `ignore_missing_imports`, that makes the whole module `Any`.
- `fields` needs `# type: ignore[assignment]` on `ResourceABC`: inmanta infers the
  parent's type from its own literal, so any tuple of another length looks like a bad
  override.  Declaring it `tuple[str, ...]` there lets the subclasses assign freely.
- The CI `tests` job runs inside `quay.io/podman/stable` as the unprivileged `podman`
  user — rootless podman refuses to run as root.

## Conventions

- Never rebase or force push a branch that has been pushed; merge master in instead.
- The changelog is for released user-facing behaviour.  No tests, no CI, no tooling, and
  no entry describing churn on something not yet released — amend the existing
  unreleased entry.

## Maintaining this file

When a change teaches you something non-obvious that would speed up the next
exploration (a convention, a gotcha, where a mechanism lives), add a short line here.
Keep it terse, factual, and path-portable.  Delete anything that becomes wrong.
