#!/bin/bash
# boot-twins.sh <n twins> <tv cpus> [runtime.sqfs [slot [runtime2.sqfs [slot2]]]]   (run as root on the runner; images and $W/{tree,state,apks} ready)
# slot: runtime (64-bit, default) or runtime32: the installed webosatl/<slot>.sqfs that the test image replaces
# Each twin = copy of Oracle's twin3 tree renamed twinA / twinB, fresh volumes restored from the twin3 snapshot,
# one shared sqfs cache volume. Fixed caps: TV <tv cpus>, harness 0.25 CPU (Oracle: TV 1.0 on N1, harness 0.25).
# v4 per-twin runtimes: when runtime2 is given, twin index 1 (twinb) boots runtime2/slot2; twin index 0 (twina)
# and twins 2-3 boot runtime/slot. With no runtime2 every twin boots runtime, exactly as v3.
set -euo pipefail
N=${1:-2}; CPUS=${2:-0.65}; RT=${3:-}; SLOT=${4:-runtime}; RT2=${5:-}; SLOT2=${6:-$SLOT}
W=/opt/twinci
PART=$(readlink -f "$(findmnt -n -o SOURCE /)"); PK=$(lsblk -no PKNAME "$PART" 2>/dev/null | head -1)
DEV=${PK:+/dev/$PK}; [ -b "${DEV:-}" ] || DEV=none   # io.max needs the whole disk, not a partition
echo "io cap device: $DEV (root on $PART)"
names=(twina twinb twinc twind)
docker volume create twinci-sqfs >/dev/null
for i in $(seq 0 $((N-1))); do
  T=${names[$i]}; P=$((47881+i)); D=$W/$T
  # per-twin runtime (v4): twinb uses runtime2 when given, all others use runtime
  TRT=$RT; TSLOT=$SLOT
  if [ $i -eq 1 ] && [ -n "$RT2" ]; then TRT=$RT2; TSLOT=$SLOT2; fi
  case $TSLOT in runtime) E=fix64-runtime;; runtime32) E=test-runtime32;; *) echo "bad slot $TSLOT" >&2; exit 2;; esac
  rm -rf $D; cp -a $W/tree $D; cd $D
  # harness overlay from this repo (ci/harness/*.py replace the tree's harness files)
  [ -d "$GITHUB_WORKSPACE/ci/harness" ] && cp -f "$GITHUB_WORKSPACE"/ci/harness/*.py harness/ && echo "harness overlay: $(ls "$GITHUB_WORKSPACE"/ci/harness)"
  [ -f twin3-env.sh ] && mv twin3-env.sh $T-env.sh
  sed -i -e "s/twin3-sqfs/twinci-sqfs/g" -e "s/twin3/$T/g" -e "s/47879/$P/g" \
         -e "s#/home/tvtwin/apktest:/apks:ro#$W/apks:/apks:ro#" Makefile compose.yaml calibrate/*.py lock.sh $T-env.sh
  rm -f .work/compose.io.yaml
  # rootful docker on the runner applies AppArmor docker-default, which blocks mount(2) even with SYS_ADMIN (Oracle runs rootless docker without AppArmor)
  sed -i "s#      - seccomp=#      - apparmor=unconfined\n      - seccomp=#" compose.yaml
  if [ -n "$TRT" ]; then  # test image: 64-bit under the entry name the Oracle fix64 scripts use
    cp "$TRT" image/$E.sqfs
    python3 - "$(sha256sum image/$E.sqfs | cut -d' ' -f1)" "$E" <<'EOF'
import json, os, sys
p = "image/sqfs-cache.json"; d = json.load(open(p))
n = sys.argv[2]
d["images"] = [e for e in d["images"] if e.get("name") != n]
d["images"].append({"name": n, "url": "file:///image/%s.sqfs" % n, "sha256": sys.argv[1], "size": os.path.getsize("image/%s.sqfs" % n)})
json.dump(d, open(p, "w"), indent=1)
EOF
  else  # drop file entries whose image is not in the tree
    python3 - <<'EOF'
import json, os
p = "image/sqfs-cache.json"; d = json.load(open(p))
d["images"] = [e for e in d["images"] if not e["url"].startswith("file://") or os.path.exists("." + e["url"][7:])]
json.dump(d, open(p, "w"), indent=1)
EOF
  fi
  for v in media var home mntlg; do
    docker volume create $T-$v >/dev/null
    docker run --rm -v $T-$v:/v -v $W/state:/s:ro tvtwin-builder tar -C /v --numeric-owner -xpf /s/$v.tar
  done
  if [ -n "$TRT" ]; then  # installed runtime = the test image, in place before the twin boots (mounting after boot fails)
    M=$(docker volume inspect -f '{{.Mountpoint}}' $T-media)/webosatl
    [ -f $M/$TSLOT.sqfs ] || { echo "state ${T}: no webosatl/$TSLOT.sqfs to replace (use a state with that runtime installed)" >&2; exit 1; }
    o=$(stat -c %u:%g $M/$TSLOT.sqfs); cp -f image/$E.sqfs $M/$TSLOT.sqfs.new && chown $o $M/$TSLOT.sqfs.new && mv -f $M/$TSLOT.sqfs.new $M/$TSLOT.sqfs
    echo "$T $TSLOT.sqfs -> $(sha256sum $M/$TSLOT.sqfs | cut -c1-16)"
  fi
done
# every runtime image installed in the snapshot (64-bit runtime.sqfs, 32-bit runtime32.sqfs, ...) must be in the
# sqfs cache, or the twin's mount shim refuses it (atld then falls back to 64-bit): add the missing ones
MD=$(docker volume inspect -f '{{.Mountpoint}}' twina-media)/webosatl
for MA in "$MD"/runtime*.sqfs; do
  [ -f "$MA" ] || continue
  base=$(basename "$MA" .sqfs)                       # runtime | runtime32
  SSHA=$(sha256sum "$MA" | cut -d' ' -f1); echo "snapshot $base.sqfs sha256 $SSHA"
  for i in $(seq 0 $((N-1))); do cd $W/${names[$i]}
    # per-twin runtime (v4): skip the slot this twin's test image replaced
    TSLOT=$SLOT; TRT=$RT
    if [ $i -eq 1 ] && [ -n "$RT2" ]; then TSLOT=$SLOT2; TRT=$RT2; fi
    [ "$base" = "$TSLOT" ] && [ -n "$TRT" ] && continue  # replaced by the test image above
    grep -q "$SSHA" image/sqfs-cache.json && continue
    cp "$MA" image/state-$base.sqfs
    python3 - "$SSHA" "$base" <<'PY'
import json, os, sys
p = "image/sqfs-cache.json"; d = json.load(open(p)); n = "state-" + sys.argv[2]
d["images"] = [e for e in d["images"] if e.get("name") != n]
d["images"].append({"name": n, "url": "file:///image/%s.sqfs" % n, "sha256": sys.argv[1], "size": os.path.getsize("image/%s.sqfs" % n)})
json.dump(d, open(p, "w"), indent=1)
PY
  done
done
# v4: populate the shared sqfs cache from EVERY twin's cache list, not just twina's -- with per-twin runtimes
# twinb's image would otherwise never enter the shared volume. With a single runtime the extra runs are
# no-ops (entries already unpacked, skipped by make sqfs).
for i in $(seq 0 $((N-1))); do
  cd $W/${names[$i]} && make sqfs IO_DEV=$DEV 2>&1 | grep -v "already unpacked"
done
cd $W/twina
python3 -c "import json;[print('cache entry',e['name'],e['sha256'][:12],e['url']) for e in json.load(open('image/sqfs-cache.json'))['images']]"; ls -la image/*.sqfs
docker run --rm -v twinci-sqfs:/c tvtwin-builder ls /c
for i in $(seq 0 $((N-1))); do
  T=${names[$i]}; cd $W/$T
  make up IO_DEV=$DEV 2>&1 | tail -4
  docker exec $T-tv sh -c 'grep -q " /media/developer/webosatl/runtime " /proc/mounts || sh /media/developer/webosatl/bin/init.sh mount' 2>&1 | tail -8
  docker exec $T-tv sh -c 'ls /media/developer/webosatl/runtime/AppRun && grep " /media/developer/webosatl/runtime" /proc/mounts' 2>&1 | sed "s/^/$T runtime: /"
  docker update --cpus "$CPUS" $T-tv >/dev/null
  docker update --cpus 0.25 $T-harness >/dev/null
done
docker ps --format '{{.Names}} {{.Status}}'
for i in $(seq 0 $((N-1))); do T=${names[$i]}
  docker inspect -f "{{.Name}} cpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}" $T-tv $T-harness
  docker exec $T-harness python -m harness.run --gate t0.versions --out /data/reports/boot 2>&1 | tail -1
done
