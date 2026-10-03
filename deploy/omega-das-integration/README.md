# Omega read-only retrieval experiment

This lane runs only MongoDB, Redis, the attention broker, and the query engine needed by an Omega retrieval proof of concept. It deliberately does not use the toolbox's host network default and publishes no host ports.

## Safety boundary

- `backend` is an internal network for databases and DAS services.
- `client` is a separate internal network attached only to `query-engine`. An Omega client must run as a container explicitly attached to `omega-das-integration-client` and connect to `query-engine:40002`; it cannot reach MongoDB or Redis through that network.
- All images require `repository@sha256:<digest>` references. Placeholder, absent, malformed, or tag-only values fail preflight/Compose interpolation.
- MongoDB and Redis use explicitly named durable volumes. The normal rollback retains them.
- No loader, mutation agent, host network, privileged mode, Docker socket, or host bind port is included. Application-level authorization is not provided by the current query-engine entrypoint; use only a trusted Omega client on the private client network.

## Deploy

Copy `.env.example` to `.env`, replace every placeholder with a trusted digest/value, then run:

```sh
./scripts/preflight.sh
./scripts/up.sh
```

The exact DAS commands come from `das-cli/src/common/container_manager/agents/attention_broker_container_manager.py` and `das-cli/src/common/bus_node/busnode_command_registry.py`. MongoDB and Redis commands follow their corresponding container managers. The `kill -0 1` checks prove only that the DAS entrypoint is alive because the source defines no health RPC; verify an actual retrieval from the attached Omega client before treating the POC as healthy.

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
