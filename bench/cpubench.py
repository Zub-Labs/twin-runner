# Single-thread CPU benchmark run inside an armv7 (32-bit) container, like the twin's TV userland.
# Prints seconds per test (median of 3). Lower is faster.
import hashlib, json, statistics, time, zlib

def loop():
    s = 0
    for i in range(3_000_000):
        s += (i * 7) % 13
    return s

def dicts():
    d = {}
    for i in range(400_000):
        d["k%d" % i] = [i, str(i), {"x": i}]
    return len(json.dumps(d))

def sha():
    b = b"x" * (64 * 1024 * 1024)
    return hashlib.sha256(b).hexdigest()

def deflate():
    b = bytes(range(256)) * (64 * 1024)
    return len(zlib.compress(b, 6))

res = {}
for name, f in [("loop", loop), ("dicts", dicts), ("sha256", sha), ("zlib", deflate)]:
    t = []
    for _ in range(3):
        a = time.perf_counter(); f(); t.append(time.perf_counter() - a)
    res[name] = round(statistics.median(t), 3)
print("BENCH", json.dumps(res))
