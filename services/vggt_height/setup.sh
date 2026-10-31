#!/bin/bash
# Build the two Python environments of the VGGT height service from scratch under $VGGT_HOME.
#
#   export VGGT_HOME=/data/vggt            # install root (about 6 GB with the weights; venvs + uv cache ~10 GB more)
#   bash setup.sh                          # foreground, ~10-20 min depending on the network
#   setsid nohup bash setup.sh > "$VGGT_HOME/setup.log" 2>&1 < /dev/null &   # detached; progress in $VGGT_HOME/run/setup.STATUS
#
# Result:
#   $VGGT_HOME/venv         Python 3.11, torch 2.8.0+cu128, requirements-vggt.txt, vggt-omega (editable)
#   $VGGT_HOME/vggt-omega   https://github.com/facebookresearch/vggt-omega @ 48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6
#   $VGGT_HOME/venv_sam31   Python 3.12, torch 2.8.0+cu128, sam31/requirements-sam31.txt, sam3 (editable)   [WITH_SAM31=1]
#   $VGGT_HOME/sam3         https://github.com/facebookresearch/sam3 @ 7f473cfd983d5eb43040ff608f66bff0d7bdcb53
# Options (environment):
#   WITH_SAM31=0            skip the SAM 3.1 venv (the service then uses the SAM 2.1 grid for /object_table)
#   TORCH_SOURCE=pytorch    (default) uv installs torch/torchvision from https://download.pytorch.org/whl/cu128
#   TORCH_SOURCE=aliyun     curl the same wheel files from https://mirrors.aliyun.com/pytorch-wheels/cu128 (mainland China)
#   PYPI_INDEX=URL          PyPI index for everything else (e.g. https://mirrors.aliyun.com/pypi/simple/)
#   UV=/path/to/uv          use this uv; otherwise uv on PATH, otherwise uv 0.13.2 is pip-installed into $VGGT_HOME/tools/uv
# Weights are a separate step: download_weights.sh.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
V=${VGGT_HOME:?set VGGT_HOME (install root)}
VGGT_REPO=https://github.com/facebookresearch/vggt-omega.git
VGGT_COMMIT=48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6
SAM3_REPO=https://github.com/facebookresearch/sam3.git
SAM3_COMMIT=7f473cfd983d5eb43040ff608f66bff0d7bdcb53
TORCH_V=2.8.0; TV_V=0.23.0; CU=cu128
mkdir -p "$V/run" "$V/src/wheels" "$V/cache"
export UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-600} UV_CACHE_DIR=${UV_CACHE_DIR:-$V/cache/uv} UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$V/uv_python}
[ -n "${PYPI_INDEX:-}" ] && export UV_DEFAULT_INDEX=$PYPI_INDEX
ST=$V/run/setup.STATUS
T0=$(date +%s)
step(){ echo "== $* $(date '+%F %T')"; echo "RUNNING $* $(date '+%F %T')" > "$ST"; }
fail(){ echo "FAIL $*"; echo "FAIL $* $(date '+%F %T')" > "$ST"; exit 1; }

step uv
if [ -z "${UV:-}" ]; then
  if command -v uv >/dev/null; then UV=$(command -v uv)
  else UV=$V/tools/uv/bin/uv
    [ -x "$UV" ] || python3 -m pip install --quiet ${PYPI_INDEX:+--index-url "$PYPI_INDEX"} --target "$V/tools/uv" uv==0.13.2 || fail "uv (pip install uv)"
  fi
fi
"$UV" --version || fail uv

clone(){ # repo dir commit
  if [ ! -d "$2/.git" ]; then
    [ -e "$2" ] && fail "$2 exists but is not a git checkout (move it away)"
    git clone -q "$1" "$2" || fail "git clone $1"
  fi
  ( cd "$2" && { git cat-file -e "$3^{commit}" 2>/dev/null || git fetch -q origin; } && git checkout -q "$3" \
    && echo "$(basename "$2") at $(git log -1 --format='%H %cI')" ) || fail "checkout $3 in $2"
}
torch_install(){ # python-tag venv
  local py=$2/bin/python
  if [ "${TORCH_SOURCE:-pytorch}" = aliyun ]; then
    local w f files=""
    for w in "torch-$TORCH_V%2B$CU-$1-$1-manylinux_2_28_x86_64.whl" "torchvision-$TV_V%2B$CU-$1-$1-manylinux_2_28_x86_64.whl"; do
      f=$V/src/wheels/$(echo "$w" | sed 's/%2B/+/')
      curl -fsSL --retry 5 -C - -o "$f" "https://mirrors.aliyun.com/pytorch-wheels/$CU/$w" || fail "download $w"
      files="$files $f"
    done
    "$UV" pip install --python "$py" $files || fail "torch install ($1)"
  else
    "$UV" pip install --python "$py" --index-url "https://download.pytorch.org/whl/$CU" \
      "torch==$TORCH_V+$CU" "torchvision==$TV_V+$CU" || fail "torch install ($1)"
  fi
}

step vggt_venv
[ -x "$V/venv/bin/python" ] || "$UV" venv --python 3.11 "$V/venv" || fail "uv venv 3.11"
"$V/venv/bin/python" --version
step vggt_src
clone "$VGGT_REPO" "$V/vggt-omega" "$VGGT_COMMIT"
step vggt_torch
torch_install cp311 "$V/venv"
step vggt_deps
"$UV" pip install --python "$V/venv/bin/python" -r "$HERE/requirements-vggt.txt" || fail "requirements-vggt.txt"
"$UV" pip install --python "$V/venv/bin/python" --no-deps -e "$V/vggt-omega" || fail "vggt-omega editable"
"$UV" pip freeze --python "$V/venv/bin/python" > "$V/run/freeze_vggt.txt"
"$V/venv/bin/python" -c "
import torch, transformers, numpy, sklearn, cv2
from vggt_omega.models import VGGTOmega
from transformers import Sam2Model, Sam2Processor
print('vggt venv ok: torch', torch.__version__, 'cuda', torch.version.cuda, 'gpu', torch.cuda.is_available(),
      'transformers', transformers.__version__, 'numpy', numpy.__version__)" || fail "vggt venv import check"

if [ "${WITH_SAM31:-1}" = 1 ]; then
  step sam31_venv
  [ -x "$V/venv_sam31/bin/python" ] || "$UV" venv --python 3.12 "$V/venv_sam31" || fail "uv venv 3.12"
  "$V/venv_sam31/bin/python" --version
  step sam31_src
  clone "$SAM3_REPO" "$V/sam3" "$SAM3_COMMIT"
  step sam31_torch
  torch_install cp312 "$V/venv_sam31"
  step sam31_deps
  "$UV" pip install --python "$V/venv_sam31/bin/python" -r "$HERE/sam31/requirements-sam31.txt" || fail "requirements-sam31.txt"
  "$UV" pip install --python "$V/venv_sam31/bin/python" --no-deps -e "$V/sam3" || fail "sam3 editable"
  "$UV" pip freeze --python "$V/venv_sam31/bin/python" > "$V/run/freeze_sam31.txt"
  "$V/venv_sam31/bin/python" -c "
import torch, numpy, sam3
from sam3.model_builder import build_sam3_image_model
from sam3.model.sam3_image_processor import Sam3Processor
print('sam31 venv ok: torch', torch.__version__, 'gpu', torch.cuda.is_available(), 'numpy', numpy.__version__)" || fail "sam31 venv import check"
fi

step compare_freeze
# the installed versions must equal the reference freeze (local wheel / editable lines aside)
norm(){ grep -v -e '^#' -e '^-e ' -e ' @ file:' -e '^torch' "$1" | tr 'A-Z_' 'a-z-' | sort; }
d=$(diff <(norm "$HERE/requirements-freeze.txt") <(norm "$V/run/freeze_vggt.txt")) && echo "vggt venv == requirements-freeze.txt" \
  || { echo "vggt venv differs from requirements-freeze.txt:"; echo "$d"; }
if [ "${WITH_SAM31:-1}" = 1 ]; then
  d=$(diff <(norm "$HERE/sam31/requirements-freeze.txt") <(norm "$V/run/freeze_sam31.txt")) && echo "sam31 venv == sam31/requirements-freeze.txt" \
    || { echo "sam31 venv differs from sam31/requirements-freeze.txt:"; echo "$d"; }
fi
echo "DONE setup $(date '+%F %T') ($(( $(date +%s) - T0 )) s)" | tee "$ST"
