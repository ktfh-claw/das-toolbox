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
