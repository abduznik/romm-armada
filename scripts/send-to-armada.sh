#!/usr/bin/env bash
# Sends the newest RomM-Armada-*.zip to the handheld's ~/Downloads over SSH, then reads its checksum back from the
# handheld to prove it arrived whole. It copies one file and touches nothing else there.
#
#   scripts/send-to-armada.sh              send the newest zip
#   scripts/send-to-armada.sh --check      only say whether the handheld answers (copies nothing)
#   scripts/send-to-armada.sh some.zip     send a particular file
#
# HOST (default armada@armada.local) and KEY (default ~/.ssh/id_ed25519, the key
# made for this, no passphrase) can be overridden in the environment.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HOST=${HOST:-armada@armada.local}
KEY=${KEY:-$HOME/.ssh/id_ed25519}
SSH_OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5)

mode=send
zip=""
for arg in "$@"; do
    case "$arg" in
        --check) mode=check ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) zip=$arg ;;
    esac
done

if [ "$mode" = send ]; then
    if [ -z "$zip" ]; then
        zip=$(ls -t "$ROOT"/RomM-Armada-*.zip 2> /dev/null | head -1 || true)
    fi
    [ -n "$zip" ] && [ -f "$zip" ] || { echo "No RomM-Armada-*.zip to send. Build one with scripts/package.sh." >&2; exit 2; }
fi

if ! ssh "${SSH_OPTS[@]}" "$HOST" true 2> /dev/null; then
    echo "The handheld does not answer at $HOST." >&2
    echo "Check that it is on and signed in to Tailscale (tailscale status), then run this again." >&2
    exit 1
fi
echo "The handheld answers: $(ssh "${SSH_OPTS[@]}" "$HOST" 'echo "$(uname -m), $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-linux}")"')"
[ "$mode" = check ] && exit 0

name=$(basename "$zip")
local_sum=$(sha256sum "$zip" | cut -d' ' -f1)
echo "Sending $name ($(du -h "$zip" | cut -f1)) to $HOST:~/Downloads/"
ssh "${SSH_OPTS[@]}" "$HOST" 'mkdir -p ~/Downloads'
scp -q "${SSH_OPTS[@]}" "$zip" "$HOST:Downloads/"
remote_sum=$(ssh "${SSH_OPTS[@]}" "$HOST" "sha256sum ~/Downloads/'$name'" | cut -d' ' -f1)

if [ "$local_sum" != "$remote_sum" ]; then
    echo "The copy on the handheld does not match (sent $local_sum, found ${remote_sum:-nothing}). Run this again." >&2
    exit 3
fi
echo "Arrived intact: sha256 ${local_sum:0:16}…"
cat << EOF

On the handheld:
  Decky menu → Settings → Developer → Install Plugin from Zip → pick $name in Downloads.
  Open RomM Armada. Settings → About shows which save helper runs there (aarch64 is expected).
EOF
