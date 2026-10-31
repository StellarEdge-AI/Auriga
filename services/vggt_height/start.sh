#!/bin/bash
# Start the VGGT height service (and its SAM 3.1 text backend) detached: setsid nohup, PID/STATUS/log files in
# $VGGT_RUN_DIR (default $VGGT_HOME/run). Binds 127.0.0.1 only unless VGGT_HOST / SAM31_HOST say otherwise.
#
#   VGGT_HOME=/data/vggt CUDA_VISIBLE_DEVICES=0 bash start.sh          # both processes: :8331 (VGGT) and :8333 (SAM 3.1)
#   bash start.sh vggt | sam31                                          # only one of them
#   VGGT_PORT=18331 SAM31_PORT=18333 CUDA_VISIBLE_DEVICES=3 bash start.sh   # second instance next to a running one
# Environment (defaults in brackets):
#   CUDA_VISIBLE_DEVICES [0]  GPU of both processes;  SAM31_CUDA_VISIBLE_DEVICES [same]  GPU of the SAM 3.1 process
#   VGGT_HOST [127.0.0.1]  VGGT_PORT [8331]  SAM31_HOST [127.0.0.1]  SAM31_PORT [8333]
#   SAM31_ENDPOINT [http://127.0.0.1:$SAM31_PORT]  where the VGGT process looks for SAM 3.1 (falls back to SAM 2.1 if down)
#   VGGT_CKPT [$VGGT_HOME/weights/vggt_omega_1b_512.pt]  SAM2_DIR [$VGGT_HOME/weights/sam2.1-hiera-large]
#   SAM31_CKPT [$VGGT_HOME/weights/sam3.1/sam3.1_multiplex.pt]  VGGT_CACHE [48 images]  SAM31_CACHE [16 images]
#   WITH_SAM31 [1]  0 = never start the SAM 3.1 process ("all" then means VGGT only)
#   START_TIMEOUT_S [300]  how long to wait for /health
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
V=${VGGT_HOME:?set VGGT_HOME (install root)}
R=${VGGT_RUN_DIR:-$V/run}
mkdir -p "$R"
GPU=${CUDA_VISIBLE_DEVICES:-0}
GPU31=${SAM31_CUDA_VISIBLE_DEVICES:-$GPU}
VGGT_HOST=${VGGT_HOST:-127.0.0.1}; VGGT_PORT=${VGGT_PORT:-8331}
SAM31_HOST=${SAM31_HOST:-127.0.0.1}; SAM31_PORT=${SAM31_PORT:-8333}
SAM31_ENDPOINT=${SAM31_ENDPOINT:-http://127.0.0.1:$SAM31_PORT}
VGGT_CKPT=${VGGT_CKPT:-$V/weights/vggt_omega_1b_512.pt}
SAM2_DIR=${SAM2_DIR:-$V/weights/sam2.1-hiera-large}
SAM31_CKPT=${SAM31_CKPT:-$V/weights/sam3.1/sam3.1_multiplex.pt}
WAIT=${START_TIMEOUT_S:-300}
WHAT=${1:-all}
[ "${CUDA_VISIBLE_DEVICES:-}" = "" ] && echo "note: CUDA_VISIBLE_DEVICES not set, using GPU 0"

alive(){ # pidfile pattern
  local p; p=$(cat "$1" 2>/dev/null) && [ -n "$p" ] && kill -0 "$p" 2>/dev/null && grep -q "$2" "/proc/$p/cmdline" 2>/dev/null; }
port_busy(){ ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$1\$"; }
wait_health(){ # name port pidfile statusfile log
  local i
  for i in $(seq 1 $((WAIT / 2))); do
    sleep 2
    if curl -s -m 2 "http://127.0.0.1:$2/health" | grep -q '"status": "ok"'; then echo "$1 up after $((2 * i)) s: $(cat "$4")"; return 0; fi
    kill -0 "$(cat "$3")" 2>/dev/null || { echo "$1 FAILED (see $5)"; echo "FAILED $(date '+%F %T %Z')" > "$4"; tail -20 "$5"; return 1; }
  done
  echo "$1 not healthy after $WAIT s (still loading?)"; tail -5 "$5"; return 1
}
launch(){ # name port gpu python script args...
  local name=$1 port=$2 gpu=$3; shift 3
  local pidf=$R/$name.$port.PID stf=$R/$name.$port.STATUS logf=$R/$name.$port.log
  if alive "$pidf" "$(basename "$2")"; then echo "$name already running pid=$(cat "$pidf"): $(cat "$stf")"; return 0; fi
  port_busy "$port" && { echo "$name: port $port busy (another process)"; return 1; }
  [ -x "$1" ] || { echo "$name: $1 missing (run setup.sh)"; return 1; }
  echo "STARTING port=$port gpu=$gpu $(date '+%F %T %Z')" > "$stf"
  ( cd "$R" || exit 1
    export HF_HOME=$V/cache/hf HF_HUB_OFFLINE=1 CUDA_VISIBLE_DEVICES=$gpu PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
    setsid nohup "$@" --status "$stf" >> "$logf" 2>&1 < /dev/null &
    echo $! > "$pidf" )
  wait_health "$name" "$port" "$pidf" "$stf" "$logf"
}

rc=0
if [ "$WHAT" != vggt ] && [ "${WITH_SAM31:-1}" = 1 ]; then
  [ -s "$SAM31_CKPT" ] || { echo "sam31: $SAM31_CKPT missing (download_weights.sh)"; exit 1; }
  launch sam31 "$SAM31_PORT" "$GPU31" "$V/venv_sam31/bin/python" "$HERE/sam31/sam31_service.py" \
    --host "$SAM31_HOST" --port "$SAM31_PORT" --ckpt "$SAM31_CKPT" --cache "${SAM31_CACHE:-64}" \
    || { rc=1; echo "SAM 3.1 not started"; }
fi
if [ "$WHAT" != sam31 ]; then
  [ -s "$VGGT_CKPT" ] || { echo "vggt: $VGGT_CKPT missing (download_weights.sh)"; exit 1; }
  launch vggt "$VGGT_PORT" "$GPU" "$V/venv/bin/python" "$HERE/vggt_height_service.py" \
    --host "$VGGT_HOST" --port "$VGGT_PORT" --ckpt "$VGGT_CKPT" --sam31 "$SAM31_ENDPOINT" \
    --cache "${VGGT_CACHE:-128}" --max_queue "${VGGT_MAX_QUEUE:-256}" || rc=1
fi
exit $rc
