#!/bin/bash
# Download (or link) the three checkpoints of the VGGT height service into $VGGT_HOME/weights and verify them against
# weights.sha256. Re-running is safe: verified files are skipped, partial downloads resume.
#
#   VGGT_HOME=/data/vggt HF_TOKEN_FILE=~/.cache/huggingface/token bash download_weights.sh
#
# Sources (all pinned to one revision; every file is checked against the sha256 of the official Hugging Face file):
#   VGGT_SOURCE  = hf (default) | hf-mirror | modelscope        VGGT-Omega 1B-512   (HF gated: token needed for hf, hf-mirror)
#   SAM2_SOURCE  = hf (default) | hf-mirror                     SAM 2.1 hiera-large (not gated)
#   SAM31_SOURCE = hf (default) | hf-mirror | modelscope        SAM 3.1 multiplex   (HF gated: token needed for hf, hf-mirror)
#   ONLY=vggt,sam2,sam31       subset (default all three; WITH_SAM31=0 drops sam31)
#   LOCAL_VGGT_PT=/path/vggt_omega_1b_512.pt  LOCAL_SAM2_DIR=/path/dir  LOCAL_SAM31_PT=/path/sam3.1_multiplex.pt
#                              hard-link (or copy) files you already have instead of downloading; sha256 still checked
#   CHECK_URLS=1               only send a 1 KB range request to every selected URL and print the HTTP code
# Token for gated repos: $HF_TOKEN, else the file $HF_TOKEN_FILE (default ~/.cache/huggingface/token). It is passed to
# curl on stdin (never on the command line) and only to the Hugging Face host or hf-mirror.com.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
V=${VGGT_HOME:?set VGGT_HOME (install root, same as for setup.sh)}
WD=$V/weights
VGGT_REV=1041e80fc0e911235d3426b0a3d9a81075111a53     # facebook/VGGT-Omega
SAM2_REV=665f8e2ad61cf5f53d65644ff27c8ee525124610     # facebook/sam2.1-hiera-large
SAM31_REV=daa63191845a41281374e725f4c9e51c7a824460    # facebook/sam3.1
SAM2_FILES="config.json model.safetensors preprocessor_config.json processor_config.json video_preprocessor_config.json"
ONLY=${ONLY:-vggt,sam2$([ "${WITH_SAM31:-1}" = 1 ] && echo ,sam31)}
mkdir -p "$WD/sam2.1-hiera-large" "$WD/sam3.1"

base(){ case "$1" in hf) echo https://huggingface.co;; hf-mirror) echo https://hf-mirror.com;;
                     modelscope) echo https://www.modelscope.cn/models;; *) echo "unknown source $1" >&2; exit 1;; esac; }
url(){  # source repo revision file
  if [ "$1" = modelscope ]; then echo "$(base modelscope)/$2/resolve/master/$4"; else echo "$(base "$1")/$2/resolve/$3/$4"; fi; }
token(){
  if [ -n "${HF_TOKEN:-}" ]; then printf '%s' "$HF_TOKEN"; return; fi
  local f=${HF_TOKEN_FILE:-$HOME/.cache/huggingface/token}
  [ -s "$f" ] && tr -d '\r\n ' < "$f"
}
want_sha(){ awk -v f="$1" '$2 == f {print $1}' "$HERE/weights.sha256"; }
verify(){ # relative path -> 0 if the file matches weights.sha256
  [ -s "$WD/$1" ] && [ "$(sha256sum "$WD/$1" | cut -d' ' -f1)" = "$(want_sha "$1")" ]; }
curl_get(){ # url dest auth(0/1) [range]
  local cfg="" ; [ "$3" = 1 ] && { local t; t=$(token); [ -n "$t" ] && cfg="header = \"Authorization: Bearer $t\""; }
  if [ -n "${4:-}" ]; then
    printf '%s\n' "$cfg" | curl -K - -s -L -r "$4" -o /dev/null -w '%{http_code}' -m 60 "$1"
  else
    local q=""; [ -t 1 ] || q=-sS        # progress meter only on a terminal
    printf '%s\n' "$cfg" | curl -K - $q -fL --retry 5 --retry-delay 5 -C - --connect-timeout 30 -o "$2" "$1"
  fi
}
fetch(){ # relative-dest url auth
  local rel=$1 u=$2 auth=$3
  if [ "${CHECK_URLS:-0}" = 1 ]; then echo "$(curl_get "$u" - "$auth" 0-1023) $u"; return 0; fi
  if verify "$rel"; then echo "ok (already verified) $rel"; return 0; fi
  [ "$auth" = 1 ] && [ -z "$(token)" ] && { echo "FAIL $rel: gated repo, set HF_TOKEN or HF_TOKEN_FILE (see README)"; return 1; }
  echo "downloading $rel <- $u"
  curl_get "$u" "$WD/$rel.part" "$auth" || { echo "FAIL download $rel (partial file kept, re-run to resume)"; return 1; }
  mv -f "$WD/$rel.part" "$WD/$rel"
  verify "$rel" && echo "ok $rel" || { echo "FAIL sha256 $rel (expected $(want_sha "$rel"))"; return 1; }
}
link(){ # src relative-dest
  verify "$2" && { echo "ok (already verified) $2"; return 0; }
  [ -e "$1" ] || { echo "FAIL $1 not found"; return 1; }
  rm -f "$WD/$2"; ln "$(readlink -f "$1")" "$WD/$2" 2>/dev/null || cp "$(readlink -f "$1")" "$WD/$2" || return 1
  verify "$2" && echo "ok (linked) $2" || { echo "FAIL sha256 $2 (expected $(want_sha "$2"))"; return 1; }
}

rc=0
case ",$ONLY," in *,vggt,*)
  if [ -n "${LOCAL_VGGT_PT:-}" ]; then link "$LOCAL_VGGT_PT" vggt_omega_1b_512.pt || rc=1
  else s=${VGGT_SOURCE:-hf}; a=1; [ "$s" = modelscope ] && a=0
    fetch vggt_omega_1b_512.pt "$(url "$s" facebook/VGGT-Omega $VGGT_REV vggt_omega_1b_512.pt)" $a || rc=1; fi;; esac
case ",$ONLY," in *,sam2,*)
  for f in $SAM2_FILES; do
    if [ -n "${LOCAL_SAM2_DIR:-}" ]; then link "$LOCAL_SAM2_DIR/$f" "sam2.1-hiera-large/$f" || rc=1
    else fetch "sam2.1-hiera-large/$f" "$(url "${SAM2_SOURCE:-hf}" facebook/sam2.1-hiera-large $SAM2_REV "$f")" 0 || rc=1; fi
  done;; esac
case ",$ONLY," in *,sam31,*)
  if [ -n "${LOCAL_SAM31_PT:-}" ]; then link "$LOCAL_SAM31_PT" sam3.1/sam3.1_multiplex.pt || rc=1
  else s=${SAM31_SOURCE:-hf}; a=1; [ "$s" = modelscope ] && a=0
    fetch sam3.1/sam3.1_multiplex.pt "$(url "$s" facebook/sam3.1 $SAM31_REV sam3.1_multiplex.pt)" $a || rc=1; fi;; esac
[ "${CHECK_URLS:-0}" = 1 ] && exit 0
echo "weights in $WD: $([ $rc = 0 ] && echo all verified || echo SOME FAILED)"
exit $rc
