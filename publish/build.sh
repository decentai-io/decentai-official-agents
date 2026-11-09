#!/bin/sh
# The catalog, built in Docker from a clean export of this working tree: only what git tracks or would track, so
# nothing left lying in a folder reaches a program. Agents may be named; with none, every one is built.
#
#   publish/build.sh [agent ...]
#
# In Git Bash on Windows, set MSYS_NO_PATHCONV=1 first.
set -e
cd "$(dirname "$0")/.."
# python:3.12-slim, the sandbox's base, by its digest.
X86_64=python:3.12-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177
export_dir=$(mktemp -d)
trap 'rm -rf "$export_dir"' EXIT
git ls-files -co --exclude-standard -z -- . ':!catalog' | tar --null --ignore-failed-read -T - -cf - 2>/dev/null | tar -xf - -C "$export_dir"
here=$(pwd -W 2>/dev/null || pwd)
there=$(cd "$export_dir" && (pwd -W 2>/dev/null || pwd))
docker run --rm -v "$there:/src:ro" -v "$here/catalog:/out" "$X86_64" sh -c \
    "pip install -q --root-user-action=ignore pyyaml && cd /src && python -m publish.build --out /out $*"
