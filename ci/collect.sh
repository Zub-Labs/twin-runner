#!/bin/bash
# collect.sh <n twins> <report name>: reports (without big screenshots), atld log tails and a one-line-per-app summary
N=$1; NAME=$2; W=/opt/twinci; O=$W/out
names=(twina twinb twinc twind)
rm -rf $O; mkdir -p $O
for i in $(seq 0 $((N-1))); do T=${names[$i]}
  mkdir -p $O/$T
  docker cp $T-harness:/data/reports/$NAME $O/$T/ 2>/dev/null || true
  docker cp $T-harness:/data/reports/boot $O/$T/ 2>/dev/null || true
  find $O/$T -name '*.png' -size +300k -delete
  docker exec $T-tv tail -n 300 /media/developer/webosatl/logs/atld.log > $O/$T/atld.log 2>/dev/null || true
  docker inspect -f '{{.Name}} cpus={{.HostConfig.NanoCpus}} mem={{.HostConfig.Memory}}' $T-tv $T-harness >> $O/caps.txt 2>/dev/null
done
python3 - $O <<'EOF' | tee $O/SUMMARY.txt
import glob, json, sys
for f in sorted(glob.glob(sys.argv[1] + "/*/*/apps.json")):
    for a in json.load(open(f)):
        print(f.split("/")[-3], a.get("apk"), a.get("category"), a.get("signature") or "", (a.get("evidence") or "")[:120].replace("\n", " "))
EOF
