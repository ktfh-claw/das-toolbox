#!/usr/bin/env python3
"""Docker-free assertions for the Omega deployment's Redis safety boundary."""

from pathlib import Path

import yaml


deployment_dir = Path(__file__).resolve().parent.parent
document = yaml.safe_load((deployment_dir / "compose.yaml").read_text(encoding="utf-8"))
services = document["services"]
redis = services["redis"]

command = redis["command"]
options = dict(zip(command[1::2], command[2::2], strict=True))
assert command[0] == "redis-server"
assert options["--bind"] == "0.0.0.0"
assert options["--protected-mode"] == "no"
assert options["--appendonly"] == "yes"

assert "ports" not in redis
assert "expose" not in redis
assert "network_mode" not in redis
assert redis["networks"] == ["backend"]
assert document["networks"]["backend"]["internal"] is True
assert "backend" in services["query-engine"]["networks"]

redis_mounts = redis["volumes"]
assert redis_mounts == ["omega-das-integration-redis:/data"]
assert "omega-das-integration-redis" in document["volumes"]

print("compose Redis policy test passed")

proxy = services["read-proxy"]
assert proxy["networks"] == ["client"]
assert "ports" not in proxy
assert "network_mode" not in proxy
assert proxy["read_only"] is True
assert proxy["cap_drop"] == ["ALL"]
assert proxy["environment"]["DAS_QUERY_ENGINE"] == "query-engine:40002"
assert proxy["environment"]["DAS_CLIENT_ENDPOINT"] == "read-proxy:42999"
assert proxy["environment"]["DAS_CALLBACK_PEER_HOST"] == "query-engine"
assert proxy["environment"]["PROXY_QUERY_TIMEOUT_SECONDS"] == "60"
assert int(proxy["environment"]["DAS_CALLBACK_PORT_LOWER"]) > 0
assert int(proxy["environment"]["DAS_CALLBACK_PORT_UPPER"]) >= int(proxy["environment"]["DAS_CALLBACK_PORT_LOWER"])
assert services["query-engine"]["networks"] == ["backend", "client"]
assert "--endpoint=0.0.0.0:40002" in services["query-engine"]["command"]
assert document["networks"]["client"]["internal"] is True

dockerfile = (deployment_dir / "proxy" / "Dockerfile").read_text(encoding="utf-8")
assert "e12573cc3a588db699c75b58b1b9e45ebf6d8d4d" in dockerfile
assert "submodule update --init" in dockerfile
assert "grpc_tools.protoc" in dockerfile
assert "USER 65532:65532" in dockerfile

adapter = (deployment_dir / "proxy" / "das_client.py").read_text(encoding="utf-8")
assert 'PatternMatchingQueryProxy(tokens=tokens)' in adapter
for disabled_option in (
    "attention_update_flag",
    "positive_importance_flag",
    "count_flag",
    "use_link_template_cache",
    "populate_metta_mapping",
    "user_metta_as_query_tokens",
):
    assert f'proxy.set_parameter("{disabled_option}", False)' in adapter
print("compose read-proxy policy test passed")
