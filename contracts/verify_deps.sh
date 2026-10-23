#!/bin/bash
# Compare the vendored dependencies with their official releases (see DEPENDENCIES.md). Needs curl and python3.
# Downloads into a fresh temporary directory; nothing downloaded is executed.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
OZ_URL=https://registry.npmjs.org/@openzeppelin/contracts/-/contracts-5.1.0.tgz
OZ_SRI=sha512-p1ULhl7BXzjjbha5aqst+QMLY+4/LCWADXOCsmLHRM77AqiPjnd9vvUN9sosUfhL9JGKpZ0TjEGxgvnizmWGSA==
FS_URL=https://codeload.github.com/foundry-rs/forge-std/tar.gz/refs/tags/v1.16.2
curl -fsS --max-time 120 -o "$T/oz.tgz" "$OZ_URL"
curl -fsSL --max-time 120 -o "$T/fs.tgz" "$FS_URL"
python3 -I - "$T" "$OZ_SRI" <<'PY'
import base64, hashlib, os, sys, tarfile
t, sri = sys.argv[1], sys.argv[2]
raw = open(os.path.join(t, "oz.tgz"), "rb").read()
got = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode()
if got != sri:
    sys.exit("OpenZeppelin tarball integrity MISMATCH")
for name, sub in (("oz.tgz", "oz"), ("fs.tgz", "fs")):
    os.makedirs(os.path.join(t, sub))
    with tarfile.open(os.path.join(t, name)) as tf:
        tf.extractall(os.path.join(t, sub), filter="data")
print("npm integrity OK")
PY
diff -rq "$HERE/node_modules/@openzeppelin/contracts" "$T/oz/package" && echo "OpenZeppelin 5.1.0: identical"
diff -rq "$HERE/lib/forge-std" "$T/fs/forge-std-1.16.2" && echo "forge-std v1.16.2: identical"
