#!/bin/bash
# Stop the processes started by start.sh with the same VGGT_HOME / VGGT_RUN_DIR / ports (only their own PIDs).
#   bash stop.sh [all|vggt|sam31]
set -u
V=${VGGT_HOME:?set VGGT_HOME (install root)}
R=${VGGT_RUN_DIR:-$V/run}
WHAT=${1:-all}
stop_one(){ # name port pattern
  local pidf=$R/$1.$2.PID p; p=$(cat "$pidf" 2>/dev/null)
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null && grep -q "$3" "/proc/$p/cmdline" 2>/dev/null; then
    kill "$p"; for i in $(seq 1 30); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$p" 2>/dev/null && kill -9 "$p"
    echo "$1 :$2 stopped pid=$p"
  else
    echo "$1 :$2 not running"
  fi
  rm -f "$pidf"; echo "STOPPED $(date '+%F %T %Z')" > "$R/$1.$2.STATUS"
}
[ "$WHAT" != sam31 ] && stop_one vggt "${VGGT_PORT:-8331}" vggt_height_service.py
[ "$WHAT" != vggt ] && stop_one sam31 "${SAM31_PORT:-8333}" sam31_service.py
exit 0
