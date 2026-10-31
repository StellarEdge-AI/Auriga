#!/bin/bash
# Show STATUS files, PID liveness and a /health summary of the processes started by start.sh.
#   bash status.sh            exit 0 if every started process answers /health
set -u
V=${VGGT_HOME:?set VGGT_HOME (install root)}
R=${VGGT_RUN_DIR:-$V/run}
rc=0
for name in vggt sam31; do
  port=$([ $name = vggt ] && echo "${VGGT_PORT:-8331}" || echo "${SAM31_PORT:-8333}")
  st=$(cat "$R/$name.$port.STATUS" 2>/dev/null || echo "never started")
  p=$(cat "$R/$name.$port.PID" 2>/dev/null)
  live=no; [ -n "$p" ] && kill -0 "$p" 2>/dev/null && live="yes pid=$p"
  h=$(curl -s -m 3 "http://127.0.0.1:$port/health")
  if [ -n "$h" ]; then
    sum=$(printf '%s' "$h" | python3 -c "import json,sys; d=json.load(sys.stdin); print(' '.join(f'{k}={d.get(k)}' for k in
      ('status','version','object_table','object_table_backend','sam31','cuda_visible_devices','allocated_mb','requests','errors') if k in d))")
  else sum="no answer"; fi
  echo "$name :$port  process: $live  status: $st"
  echo "    health: $sum"
  case "$st" in UP*|STARTING*) [ -n "$h" ] || rc=1;; esac
done
exit $rc
