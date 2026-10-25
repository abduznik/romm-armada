#!/usr/bin/env bash
# Builds ../bin/savebridge-x86_64 and ../bin/savebridge-aarch64: static executables, so they need no
# particular glibc and run the same on SteamOS, on Armada and inside FEX's x86-64 rootfs.
#
# Needs podman (or CTR=docker), git and network on the first run. The compilers live in a container
# based on Debian 12, whose glibc is older than anything the plugin runs on.
#
#   ./build.sh              -> ../bin/savebridge-{x86_64,aarch64}
#   WORK=/tmp/w ./build.sh  keep the checkout and build trees somewhere else
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WORK=${WORK:-$ROOT/build/native}
CTR=${CTR:-podman}
IMAGE=localhost/romm-armada-native:1

repo= sigil= libchdr= tiny_aes=
while IFS='=' read -r key value; do
    case "$key" in
        repo) repo=$value ;;
        sigil) sigil=$value ;;
        libchdr) libchdr=$value ;;
        tiny-AES-c) tiny_aes=$value ;;
    esac
done < <(grep -E '^[a-zA-Z0-9_-]+=' "$HERE/SIGIL_PIN")
mkdir -p "$WORK" "$ROOT/bin"

echo "== argosy-sigil $sigil"
if [ ! -d "$WORK/sigil/.git" ]; then
    git clone --quiet "$repo" "$WORK/sigil"
fi
git -C "$WORK/sigil" fetch --quiet origin "$sigil" 2>/dev/null || true
git -C "$WORK/sigil" checkout --quiet --force "$sigil"
git -C "$WORK/sigil" submodule update --quiet --init
check() { # check <path> <expected commit>
    got=$(git -C "$WORK/sigil/$1" rev-parse HEAD)
    [ "$got" = "$2" ] || { echo "submodule $1 is at $got, SIGIL_PIN says $2" >&2; exit 1; }
}
check third_party/libchdr "$libchdr"
check third_party/tiny-AES-c "$tiny_aes"

echo "== builder image"
if ! $CTR image exists "$IMAGE" 2>/dev/null; then
    $CTR build -q -t "$IMAGE" -f - "$WORK" > /dev/null <<'EOT'
FROM docker.io/library/debian:bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build binutils \
        gcc-aarch64-linux-gnu libc6-dev-arm64-cross binutils-aarch64-linux-gnu \
 && rm -rf /var/lib/apt/lists/*
EOT
fi

echo "== build"
$CTR run --rm --userns=keep-id -e HOME=/tmp \
    -v "$HERE:/src:ro,Z" -v "$WORK:/work:Z" -v "$ROOT/bin:/out:Z" \
    -e SIGIL_PIN="${sigil:0:12}" "$IMAGE" bash -euc '
        common="-DSIGIL_SRC=/work/sigil -DSIGIL_PIN=$SIGIL_PIN -DCMAKE_BUILD_TYPE=Release -DSB_STATIC=ON \
                -DCMAKE_C_FLAGS=-ffile-prefix-map=/work=. -DCMAKE_EXE_LINKER_FLAGS=-Wl,--build-id=none"
        cmake -S /src/savebridge -B /work/b-x86_64 -G Ninja $common > /dev/null
        cmake --build /work/b-x86_64 --target savebridge > /dev/null
        strip -s -o /out/savebridge-x86_64 /work/b-x86_64/savebridge
        cmake -S /src/savebridge -B /work/b-aarch64 -G Ninja $common \
              -DCMAKE_TOOLCHAIN_FILE=/src/toolchain-aarch64.cmake > /dev/null
        cmake --build /work/b-aarch64 --target savebridge > /dev/null
        aarch64-linux-gnu-strip -s -o /out/savebridge-aarch64 /work/b-aarch64/savebridge
    '

echo "== done"
file "$ROOT"/bin/savebridge-* 2>/dev/null || true
sha256sum "$ROOT"/bin/savebridge-*
