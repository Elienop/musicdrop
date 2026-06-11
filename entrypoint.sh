#!/bin/sh
# PUID/PGID remap (the *arr convention): MusicDrop WRITES into the music
# share (tag writes, artwork, reorganize moves), so the runtime UID/GID must
# match the share's owner. Defaults to 911:911. Only /data is chown'd — the
# music mount's ownership belongs to the NAS, never to this container.
set -e

PUID=${PUID:-911}
PGID=${PGID:-911}

case "$PUID" in '' | *[!0-9]*)
    echo "ERROR: PUID must be numeric (got '$PUID')" >&2
    exit 1
    ;;
esac
case "$PGID" in '' | *[!0-9]*)
    echo "ERROR: PGID must be numeric (got '$PGID')" >&2
    exit 1
    ;;
esac
if [ "$PUID" -lt 100 ] || [ "$PUID" -gt 65534 ]; then
    echo "ERROR: PUID must be between 100 and 65534 (got $PUID)" >&2
    exit 1
fi
if [ "$PGID" -lt 100 ] || [ "$PGID" -gt 65534 ]; then
    echo "ERROR: PGID must be between 100 and 65534 (got $PGID)" >&2
    exit 1
fi

if ! getent group musicdrop >/dev/null 2>&1; then
    groupadd -g "$PGID" musicdrop
else
    groupmod -o -g "$PGID" musicdrop
fi
if ! id musicdrop >/dev/null 2>&1; then
    useradd -u "$PUID" -g musicdrop -d /app -M -s /usr/sbin/nologin musicdrop
else
    usermod -o -u "$PUID" musicdrop
fi

mkdir -p /data
chown -R musicdrop:musicdrop /data

exec gosu musicdrop "$@"
