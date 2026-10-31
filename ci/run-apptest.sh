#!/bin/bash
# run-apptest.sh <n twins> <report name> <apk...|smoke>   (root on the runner)
# Splits the APKs round-robin over the twins, copies each twin's share into its harness volume (apptest --cleanup
# deletes the copies) and runs harness.apptest --deep on all twins at the same time.
set -uo pipefail
N=$1; NAME=$2; shift 2
W=/opt/twinci
names=(twina twinb twinc twind)
[ "${1:-smoke}" = smoke ] && set -- all/g061-mpv.apk all/g066-pipetv.apk
[ "$1" = ALL ] && set -- $(cd $W/apks && ls all/*.apk arm64/*.apk)
i=0; declare -A share
for a in "$@"; do T=${names[$((i % N))]}; share[$T]+="$a "; i=$((i+1)); done
for i in $(seq 0 $((N-1))); do T=${names[$i]}
  [ -n "${share[$T]:-}" ] || continue
  (
    docker exec $T-harness sh -c "rm -rf /data/apks-$NAME && mkdir -p /data/apks-$NAME"
    for a in ${share[$T]}; do docker cp "$W/apks/$a" "$T-harness:/data/apks-$NAME/"; done
    docker exec $T-tv /media/developer/webosatl/bin/init.sh restart atld.py >/dev/null 2>&1 || true
    sleep 3
    echo "== $T: ${share[$T]}"
    docker exec $T-harness python -m harness.apptest --apks /data/apks-$NAME --out /data/reports/$NAME \
      --wait 240 --settle 40 --dpad auto --cleanup --deep 2>&1 | sed "s/^/[$T] /"
  ) &
done
wait
