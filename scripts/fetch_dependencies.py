#!/usr/bin/env python3
"""Restore the third-party binaries listed in DEPENDENCIES.json.

The BSL Language Server JAR is always restored. The portable Temurin JDK for
Windows x64 is optional (--jdk): any Java 21+ on PATH works as well.
Every download is checked against the SHA-256 pinned in DEPENDENCIES.json;
a mismatch leaves nothing behind and exits with code 1.

Usage:
  python scripts/fetch_dependencies.py            # BSL LS JAR
  python scripts/fetch_dependencies.py --jdk      # JAR and portable JDK
  python scripts/fetch_dependencies.py --check    # verify what is present, download nothing
"""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "DEPENDENCIES.json"
CHUNK = 1 << 20


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, target):
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as response, open(target, "wb") as stream:
        shutil.copyfileobj(response, stream, CHUNK)


def restore_jar(spec, check_only):
    target = ROOT / spec["path"]
    if target.is_file():
        actual = sha256(target)
        if actual == spec["sha256"]:
            print(f"ok      {spec['path']}")
            return True
        print(f"MISMATCH {spec['path']}: sha256 {actual}, expected {spec['sha256']}")
        return False
    if check_only:
        print(f"missing {spec['path']}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        partial = Path(tmp) / target.name
        download(spec["url"], partial)
        actual = sha256(partial)
        if actual != spec["sha256"]:
            print(f"MISMATCH {spec['url']}: sha256 {actual}, expected {spec['sha256']}")
            return False
        partial.replace(target)
    print(f"ok      {spec['path']}")
    return True


def restore_jdk(spec, check_only):
    target = ROOT / spec["path"]
    release = target / "release"
    if release.is_file():
        actual = sha256(release)
        if actual == spec["release_file_sha256"]:
            print(f"ok      {spec['path']} ({spec['version']})")
            return True
        print(f"MISMATCH {spec['path']}/release: sha256 {actual}, expected {spec['release_file_sha256']}")
        return False
    if check_only:
        print(f"missing {spec['path']} (optional)")
        return True
    if target.exists():
        print(f"{spec['path']} exists but has no 'release' file; remove it and run again")
        return False
    downloads = ROOT / ".downloads"
    downloads.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=downloads) as tmp:
        archive = Path(tmp) / "jdk.zip"
        download(spec["url"], archive)
        actual = sha256(archive)
        if actual != spec["archive_sha256"]:
            print(f"MISMATCH {spec['url']}: sha256 {actual}, expected {spec['archive_sha256']}")
            return False
        unpacked = Path(tmp) / "unpacked"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpacked)
        roots = [p for p in unpacked.iterdir() if p.is_dir()]
        if len(roots) != 1 or not (roots[0] / "release").is_file():
            print("unexpected JDK archive layout")
            return False
        if sha256(roots[0] / "release") != spec["release_file_sha256"]:
            print("MISMATCH JDK 'release' file")
            return False
        shutil.move(str(roots[0]), str(target))
    print(f"ok      {spec['path']} ({spec['version']})")
    return True


def main():
    parser = argparse.ArgumentParser(description="Restore third-party binaries from DEPENDENCIES.json")
    parser.add_argument("--jdk", action="store_true", help="also restore the portable Temurin JDK (Windows x64)")
    parser.add_argument("--check", action="store_true", help="only verify files that are present")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok = restore_jar(manifest["bsl_ls"], args.check)
    if args.jdk or args.check:
        ok = restore_jdk(manifest["jdk"], args.check) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
