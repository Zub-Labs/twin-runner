#!/bin/bash
# run-explore.sh <n twins> <report name> <minutes per app> <apk...>   (root on the runner; key script in env KEYS)
# Like run-apptest.sh, but runs harness.explore (one app at a time per twin) and has ci/explore-collect.py turn every
# finished app into a tarball + contact sheets in twinci-data. Harness output goes to private files only.
set -uo pipefail
N=$1; NAME=$2; MIN=$3; shift 3
W=/opt/twinci; X=$W/explore; mkdir -p $X/log
names=(twina twinb twinc twind)
i=0; declare -A share
for a in "$@"; do T=${names[$((i % N))]}; share[$T]+="$a "; i=$((i+1)); done
for t in $(seq 1 60); do [ -f /tmp/apt.done ] && break; sleep 5; done
python3 -c "import PIL" && command -v tesseract >/dev/null || { echo "::error::post-processing tools missing"; exit 1; }
# collector: counts on stdout (public log), details in a private file
python3 "$GITHUB_WORKSPACE/ci/explore-collect.py" "$NAME" "$#" ${!share[@]} 2> $X/log/collect.log &
CP=$!
PIDS=()
for i in $(seq 0 $((N-1))); do T=${names[$i]}
  [ -n "${share[$T]:-}" ] || continue
  (
    docker exec $T-harness sh -c "rm -rf /data/apks-$NAME /data/explore/$NAME && mkdir -p /data/apks-$NAME /data/explore/$NAME"
    for a in ${share[$T]}; do docker cp "$W/apks/$a" "$T-harness:/data/apks-$NAME/"; done
    docker exec $T-tv /media/developer/webosatl/bin/init.sh restart atld.py >/dev/null 2>&1 || true
    sleep 3
    echo "$T: $(echo ${share[$T]} | wc -w) apps"
    docker exec $T-harness python -m harness.explore --apks /data/apks-$NAME --out /data/explore/$NAME --minutes "$MIN" \
      --keys "${KEYS:-}" $([ "${POINTER:-0}" = 1 ] && echo --pointer) > $X/log/$T.log 2>&1
    echo "$T: harness exit $?"
  ) &
  PIDS+=($!)
done
wait "${PIDS[@]}"
touch $X/ALL-DONE
wait $CP
