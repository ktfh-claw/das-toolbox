#!/bin/sh
set -eu

deployment_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM

mkdir -p "$test_dir/config" "$test_dir/scripts"
cp "$deployment_dir/config/config.json.template" "$test_dir/config/config.json.template"
cp "$deployment_dir/scripts/render-config.sh" "$test_dir/scripts/render-config.sh"

cat >"$test_dir/test.env" <<'EOF'
MONGODB_USERNAME=test-user
MONGODB_PASSWORD=test-password
EOF

"$test_dir/scripts/render-config.sh" "$test_dir/test.env"

python3 - "$test_dir/generated/config.json" <<'PY'
import json
import pathlib
import stat
import sys

config = pathlib.Path(sys.argv[1])
mode = stat.S_IMODE(config.stat().st_mode)
assert mode == 0o644, f"expected generated config mode 0644, got {mode:04o}"

document = json.loads(config.read_text(encoding="utf-8"))
mongodb = document["atomdb"]["mongodb"]
assert mongodb["username"] == "test-user"
assert mongodb["password"] == "test-password"
PY

echo "render-config permission test passed"
