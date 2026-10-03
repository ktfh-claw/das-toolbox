# Omega read-only retrieval experiment

This lane runs MongoDB, Redis, the attention broker, the query engine, and a deliberately narrow HTTP-to-DAS read proxy for an Omega retrieval proof of concept. It does not use the toolbox's host network default and publishes no host ports.

## Safety boundary

- `backend` is an internal network for databases and DAS services.
- `client` is a separate internal network attached only to `query-engine` and `read-proxy`. An Omega client must run as a container explicitly attached to `omega-das-integration-client` and connect to `read-proxy:8080`; it cannot reach MongoDB or Redis through that network.
- The proxy publishes no port. Private Docker-network membership is the accepted trust boundary: there is no application authentication or TLS inside it. Do not attach untrusted containers to `client`.
- The HTTP surface exposes only `POST /v1/query`, which always creates a DAS `PatternMatchingQueryProxy`. Callers cannot select another bus command, set a context, enable count mode, update attention, populate mappings, use the link-template cache, or pass arbitrary DAS parameters. Requests, tokens, answer count, answer fields, and query duration are bounded.
- DAS queries are bidirectional. `read-proxy:42999` is the advertised client identity and per-query gRPC callback listeners use `read-proxy:43000-43031`. The query engine must be able to call the dynamic callback addresses, which is why both services share `client`. None of these ports is published or reachable from the host.
- All images require `repository@sha256:<digest>` references. Placeholder, absent, malformed, or tag-only values fail preflight/Compose interpolation.
- MongoDB and Redis use explicitly named durable volumes. The normal rollback retains them.
- Redis listens on the private `backend` network with protected mode disabled so its intended non-loopback peer, `query-engine`, can issue commands. Redis has no published host port, is not attached to `client`, and `backend` is internal; network membership is therefore its access boundary. Do not attach untrusted containers to `backend` or publish Redis port 6379. Append-only persistence remains enabled on its durable volume.
- No loader, mutation agent, host network, privileged mode, Docker socket, or host bind port is included. The proxy runs as UID/GID 65532 with a read-only root filesystem, dropped capabilities, and a small temporary filesystem.
- The proxy container builds the DAS Python bus client from pinned DAS source commit `e12573cc3a588db699c75b58b1b9e45ebf6d8d4d`. The base Python image is digest-pinned through `PYTHON_IMAGE_DIGEST`; the build requires outbound access to GitHub and Python package indexes.
- The generated configuration is mode `0644` because the `trueagi/das` image runs as `nonroot:nonroot` and Docker bind mounts preserve the host file's numeric ownership and mode. Without host UID mapping, owner/group-only read access cannot reliably grant that container identity access; `0644` is the minimum mode that retains owner-only writes while permitting the image to read the file. On the deployment host, keep `/home/ubuntu` non-traversable by other users (mode `0700`, as provisioned): the parent-directory boundary prevents other host users from reaching a checkout below it, so the file's other-read bit enables the bind-mounted container without broadening host access. Preflight does not alter that boundary.

## Deploy

Copy `.env.example` to `.env`, replace every placeholder with a trusted digest/value, then run:

```sh
./scripts/preflight.sh
./scripts/up.sh
```

The Docker-free regression tests are:

```sh
./tests/test-render-config-permissions.sh
python3 ./tests/test-compose-policy.py
python3 ./tests/test-read-proxy.py
```

The exact DAS service commands come from `das-cli/src/common/container_manager/agents/attention_broker_container_manager.py` and `das-cli/src/common/bus_node/busnode_command_registry.py`. MongoDB and Redis commands follow their corresponding container managers. The proxy callback behavior and parameter names follow the pinned client's `ServiceBus`, `BaseQueryProxy`, and `PatternMatchingQueryProxy` implementations.

The `trueagi/das` image is distroless and has no `/bin/sh`, and the repository source defines no DAS health RPC or image-provided probe command. Consequently, the deployment does not invent an in-container health check for the attention broker or query engine. MongoDB and Redis remain health-gated, while the query engine uses Compose's strongest source-supported condition for the broker, `service_started`. A successful `up.sh` therefore proves only that Compose started the DAS containers; it does not prove DAS readiness or retrieval correctness.

Before treating the POC as operational, perform all of these post-start checks:

1. Inspect `docker compose --env-file .env -f compose.yaml ps` and `docker compose --env-file .env -f compose.yaml logs attention-broker query-engine`; reject crash loops, exited containers, or startup errors.
2. From an already-approved diagnostic or Omega client container attached to `omega-das-integration-client`, verify that `query-engine:40002` accepts a connection. Because the attention broker is backend-only, verify its `attention-broker:40001` listener from an already-approved container attached to `omega-das-integration-backend`. Do not add host port publications for these checks.
3. Run a representative read-only Omega query through the proxy and confirm the expected result. Listener checks alone are not application-level readiness checks.

These checks are deliberately post-start operator validation: the deployment cannot honestly encode them as container health without a source-supported probe or a trusted external health-monitor component.

Attach an already-created Omega client container without exposing a port:

```sh
docker network connect omega-das-integration-client OMEGA_CONTAINER_NAME
```

Do not attach untrusted containers. Network membership is the access boundary.

From that container, issue a tokenized DAS pattern query:

```sh
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"tokens":["LINK_TEMPLATE","Expression","2","NODE","Symbol","Concept","VARIABLE","X"],"max_answers":10}' \
  http://read-proxy:8080/v1/query
```

A successful response has the stable shape:

```json
{"answers":[{"assignments":{"X":"..."},"handles":["..."],"importance":0.0,"strength":1.0}],"count":1,"truncated":false}
```

Invalid input returns HTTP 400, oversized bodies return 413, unknown paths return 404, and a timeout or DAS transport failure returns 502 with a short JSON error. The proxy intentionally serializes queries because the pinned client uses process-wide singleton and port-pool state. This is a functional integration lane, not a high-throughput public API.

## Rollback

```sh
./scripts/rollback.sh
```

Rollback selects only resources bearing `io.ktfh-claw.task=omega-das-integration`. It removes task containers and task networks while retaining MongoDB and Redis volumes. After separately backing up or confirming the data is disposable, erase only the task-labeled volumes with:

```sh
./scripts/rollback.sh --purge-data
```

The scripts never use global prune operations.
