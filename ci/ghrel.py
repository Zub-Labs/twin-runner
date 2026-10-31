#!/usr/bin/env python3
"""ghrel.py: release-asset transfers with as few GitHub API calls as possible (the user's hourly REST quota is shared
by many jobs and agents). Token: env GH_TOKEN.
  ghrel.py download <owner/repo> <release tag> <dest dir> <file...>   1 tag lookup + listing pages + 1 call per file
  ghrel.py upload   <owner/repo> <release tag> <file>                 1 tag lookup + 1 call (+ delete on a name clash)
Retries 403/429 with long back-off instead of failing at once."""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def api(method, url, data=None, ctype=None, accept="application/vnd.github+json", raw=False):
    h = {"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": accept}
    if ctype:
        h["Content-Type"] = ctype
    for wait in (0, 60, 180, 300, 600):
        if wait:
            print("HTTP %s, retry in %d s" % (code, wait), file=sys.stderr)
            time.sleep(wait)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=900) as r:
                b = r.read()
                return r.status, (b if raw else (json.loads(b) if b else None))
        except urllib.error.HTTPError as e:
            code = e.code
            if e.code not in (403, 429, 502, 503):
                return e.code, None
    return code, None


def rel_id(repo, tag):
    st, j = api("GET", "https://api.github.com/repos/%s/releases/tags/%s" % (repo, tag))
    if st != 200:
        sys.exit("release %s of %s: HTTP %s" % (tag, repo, st))
    return j["id"]


def listing(repo, rid):
    out, page = {}, 1
    while True:
        st, a = api("GET", "https://api.github.com/repos/%s/releases/%d/assets?per_page=100&page=%d" % (repo, rid, page))
        if not a:
            return out
        out.update({x["name"]: x for x in a})
        if len(a) < 100:
            return out
        page += 1


def main():
    cmd, repo, tag = sys.argv[1:4]
    rid = rel_id(repo, tag)
    if cmd == "download":
        dest, files = sys.argv[4], sys.argv[5:]
        os.makedirs(dest, exist_ok=True)
        idx = listing(repo, rid)
        for f in files:
            a = idx.get(f)
            if not a:
                sys.exit("not in %s: %s" % (tag, f))
            st, b = api("GET", a["url"], accept="application/octet-stream", raw=True)
            if st != 200 or len(b) != a["size"]:
                sys.exit("download %s: HTTP %s, %s bytes" % (f, st, len(b or b"")))
            open(os.path.join(dest, f), "wb").write(b)
        print("downloaded %d files" % len(files))
    elif cmd == "upload":
        f = sys.argv[4]
        name = os.path.basename(f)
        data = open(f, "rb").read()
        url = "https://uploads.github.com/repos/%s/releases/%d/assets?name=%s" % (repo, rid, urllib.parse.quote(name))
        st, _ = api("POST", url, data, "application/gzip")
        if st == 422:  # name exists: delete the old asset, upload again
            old = listing(repo, rid).get(name)
            if old:
                api("DELETE", "https://api.github.com/repos/%s/releases/assets/%d" % (repo, old["id"]))
            st, _ = api("POST", url, data, "application/gzip")
        if st != 201:
            sys.exit("upload %s: HTTP %s" % (name, st))
        print("uploaded %s" % name)


if __name__ == "__main__":
    main()
