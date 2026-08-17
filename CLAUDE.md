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
- The default `_operation` is `files::merge`, under which `serialize` drops null
  attributes.  That is the mechanism behind "null is not managed" — don't set
  `operation` on these entities.
- `implement JsonObjectABC using parents, json_object` must stay: `json_object` sets
  `self.resource = self`, without which `entity.root` is never set and the exporter's
  `serialize_for_resource` returns nothing.
- A relation **between two json objects must be one-way** (`A.b [1] -- B`, not
  `-- B.as [0:]`).  A two-way one makes `get_child_instances` treat each end as a child
  of the other, and the serializer recurses forever.

## Ids are ids, never names

The api addresses everything by opaque id.  So does the model: an attribute that points
at another object carries **the id**, and the handler does no name↔id translation.
`netbird::ResourceABC.id` exposes an object's own id as a `std::create_fact_reference`
reading the `id` fact its resource publishes, so one resource feeds another
(`auto_groups=[some_group.id]`) without the model ever knowing the value.

Every handler publishes that fact on **both** the read and the create path — a create
that didn't publish would leave the reference unresolvable until the next repair.

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
  group: a key left out of it is emptied.
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
  masked (`ABCDE****`) on every read.
- A setup key's `auto_groups` come back in the order they were sent, and as `[]` when
  empty (no json `null` here).  The account's `All` group is refused (422 "can't add
  'all' group to the setup key"), and so is an unknown group id — unlike a group's
  `peers`, which drop silently.
- The server binds a hardcoded `:33073` for the management grpc, which no config key
  moves.  Two servers sharing a network namespace fight over it and the second one
  exits — hence the namespace per container, see below.

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
