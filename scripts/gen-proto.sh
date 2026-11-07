#!/usr/bin/env bash
# Regenerates all code from proto/. CI runs this and fails on any diff.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

out=platform/proto/src/playground_proto
rm -rf "$out/platform"
uv run --group dev python -m grpc_tools.protoc -I proto \
  --python_out="$out" --grpc_python_out="$out" \
  --mypy_out="$out" --mypy_grpc_out="$out" \
  $(find proto/platform -name '*.proto' | sort)

# protoc writes absolute imports from the proto path ("from platform.v1 ...").
# A top-level "platform" package would shadow the stdlib module, so the code
# lives under playground_proto and the imports are rewritten to match.
find "$out/platform" -name '*.py*' -print0 | xargs -0 sed -i -E \
  's/^from platform\.v1 import/from playground_proto.platform.v1 import/'
touch "$out/platform/__init__.py" "$out/platform/v1/__init__.py"

if [[ "${SKIP_TS:-}" != 1 ]]; then
  (cd proto && ../web/node_modules/.bin/buf generate)
fi
