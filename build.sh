#!/usr/bin/env bash
# Deployment build entry: prepares the deployment-local venv (robonix_api deps
# + grpcio-tools for package codegen, with --system-site-packages so rclpy from
# the system ROS 2 install is visible), then builds the selected manifest.
# The system python3 is left untouched: JetPack pins an older grpcio than the
# generated bindings need.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DEPLOY_DIR/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -q --disable-pip-version-check \
    grpcio==1.80.0 protobuf==6.33.6 "mcp>=1.27,<2" "fastmcp>=3,<5" \
    "pyyaml>=6" "requests>=2.25" grpcio-tools==1.76.0

# rbnx codegen inside fetched packages honours this interpreter override.
export RBNX_CODEGEN_PYTHON="$VENV/bin/python"
export PATH="$VENV/bin:$PATH"

# Local-first: prefer the gitignored path-based manifest, which builds the
# sibling checkouts (../primitive-* / ../service-*) without a GitHub clone.
# Fall back to the committed url-based manifest on machines without it.
MANIFEST="$DEPLOY_DIR/robonix_manifest.local.yaml"
[[ -f "$MANIFEST" ]] || MANIFEST="$DEPLOY_DIR/robonix_manifest.yaml"

rbnx build --no-update-check -f "$MANIFEST"
