#!/usr/bin/env bash
# Checks that the two copies of the agent <-> daemon contract are identical. Run it before every
# release: a difference means a wire change reached only one side, and heartbeats would fail.
#
#   ./check_wire.sh
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
agent_copy="$root/agent/agent/wire.py"
daemon_copy="$root/cluster-manager/cluster_manager/daemon/wire.py"

if cmp -s "$agent_copy" "$daemon_copy"; then
    echo "wire.py: the agent's and the daemon's copies are identical"
    exit 0
fi
echo "wire.py: the copies differ (agent first, daemon second):" >&2
diff -u "$agent_copy" "$daemon_copy" >&2 || true
exit 1
