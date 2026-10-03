# Omega read-only retrieval experiment

This lane runs only MongoDB, Redis, the attention broker, and the query engine needed by an Omega retrieval proof of concept. It deliberately does not use the toolbox's host network default and publishes no host ports.

## Safety boundary

- `backend` is an internal network for databases and DAS services.
- `client` is a separate internal network attached only to `query-engine`. An Omega client must run as a container explicitly attached to `omega-das-integration-client` and connect to `query-engine:40002`; it cannot reach MongoDB or Redis through that network.
- All images require `repository@sha256:<digest>` references. Placeholder, absent, malformed, or tag-only values fail preflight/Compose interpolation.
- MongoDB and Redis use explicitly named durable volumes. The normal rollback retains them.
- No loader, mutation agent, host network, privileged mode, Docker socket, or host bind port is included. Application-level authorization is not provided by the current query-engine entrypoint; use only a trusted Omega client on the private client network.
- The generated configuration is mode `0644` because the `trueagi/das` image runs as `nonroot:nonroot` and Docker bind mounts preserve the host file's numeric ownership and mode. Without host UID mapping, owner/group-only read access cannot reliably grant that container identity access; `0644` is the minimum mode that retains owner-only writes while permitting the image to read the file. On the deployment host, keep `/home/ubuntu` non-traversable by other users (mode `0700`, as provisioned): the parent-directory boundary prevents other host users from reaching a checkout below it, so the file's other-read bit enables the bind-mounted container without broadening host access. Preflight does not alter that boundary.

## Deploy

Copy `.env.example` to `.env`, replace every placeholder with a trusted digest/value, then run:

```sh
./scripts/preflight.sh
./scripts/up.sh
```

The Docker-free permission regression test is `./tests/test-render-config-permissions.sh`.

The exact DAS commands come from `das-cli/src/common/container_manager/agents/attention_broker_container_manager.py` and `das-cli/src/common/bus_node/busnode_command_registry.py`. MongoDB and Redis commands follow their corresponding container managers.

The `trueagi/das` image is distroless and has no `/bin/sh`, and the repository source defines no DAS health RPC or image-provided probe command. Consequently, the deployment does not invent an in-container health check for the attention broker or query engine. MongoDB and Redis remain health-gated, while the query engine uses Compose's strongest source-supported condition for the broker, `service_started`. A successful `up.sh` therefore proves only that Compose started the DAS containers; it does not prove DAS readiness or retrieval correctness.

Before treating the POC as operational, perform all of these post-start checks:

1. Inspect `docker compose --env-file .env -f compose.yaml ps` and `docker compose --env-file .env -f compose.yaml logs attention-broker query-engine`; reject crash loops, exited containers, or startup errors.
2. From an already-approved diagnostic or Omega client container attached to `omega-das-integration-client`, verify that `query-engine:40002` accepts a connection. Because the attention broker is backend-only, verify its `attention-broker:40001` listener from an already-approved container attached to `omega-das-integration-backend`. Do not add host port publications for these checks.
3. Run a representative read-only Omega query through `query-engine:40002` and confirm the expected result. Listener checks alone are not application-level readiness checks.

These checks are deliberately post-start operator validation: the deployment cannot honestly encode them as container health without a source-supported probe or a trusted external health-monitor component.

Attach an already-created Omega client container without exposing a port:

```sh
docker network connect omega-das-integration-client OMEGA_CONTAINER_NAME
```

Do not attach untrusted containers. Network membership is the access boundary.

## Rollback

```sh
./scripts/rollback.sh
```

Rollback selects only resources bearing `io.ktfh-claw.task=omega-das-integration`. It removes task containers and task networks while retaining MongoDB and Redis volumes. After separately backing up or confirming the data is disposable, erase only the task-labeled volumes with:

```sh
./scripts/rollback.sh --purge-data
```

The scripts never use global prune operations.
