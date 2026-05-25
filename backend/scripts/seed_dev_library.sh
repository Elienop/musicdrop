#!/usr/bin/env bash
# Seed a real dev beets library from a handful of album folders, for the manual
# demo only (NOT used by tests). Idempotent: re-running re-copies missing albums
# and re-imports in place. Everything it writes lives under data/ (gitignored).
set -euo pipefail

# Resolve repo root from this script's location (backend/scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# SOURCE_ROOT is overridable; defaults to the old repo's sample music.
SOURCE_ROOT="${SOURCE_ROOT:-/mnt/data/projects/MusicDrop-old/Volumetest/fresh}"
MUSIC_DIR="${REPO_ROOT}/data/music"
BEETS_DIR="${REPO_ROOT}/data/beets"
CONFIG_PATH="${BEETS_DIR}/config.yaml"
LIBRARY_PATH="${BEETS_DIR}/library.db"

if [[ ! -d "${SOURCE_ROOT}" ]]; then
  echo "SOURCE_ROOT does not exist: ${SOURCE_ROOT}" >&2
  echo "Set SOURCE_ROOT to a directory of <artist>/<album>/ folders and re-run." >&2
  exit 0
fi

ALBUMS=(
  "ABBA/Arrival"
  "a-ha/Hunting High and Low"
  "2 Unlimited/No Limits"
  "4 Non Blondes/Bigger, Better, Faster, More!"
  "Aerosmith/Toys in the Attic"
)

mkdir -p "${MUSIC_DIR}" "${BEETS_DIR}"

has_audio() {
  find "$1" -maxdepth 1 -type f \
    \( -iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.ogg' \) \
    -print -quit | grep -q .
}

copied=0
for album in "${ALBUMS[@]}"; do
  src="${SOURCE_ROOT}/${album}"
  dst="${MUSIC_DIR}/${album}"
  if [[ ! -d "${src}" ]]; then
    echo "skip (missing source): ${album}"
    continue
  fi
  if ! has_audio "${src}"; then
    echo "skip (no audio): ${album}"
    continue
  fi
  mkdir -p "$(dirname "${dst}")"
  # cp -n is idempotent: never clobbers already-copied files.
  cp -rn "${src}" "$(dirname "${dst}")/"
  echo "copied: ${album}"
  copied=$((copied + 1))
done
echo "albums copied/present: ${copied}"

cat > "${CONFIG_PATH}" <<EOF
directory: ${MUSIC_DIR}
library: ${LIBRARY_PATH}
import:
  copy: no
  move: no
  autotag: no
EOF
echo "wrote beets config: ${CONFIG_PATH}"

# Index in place from existing tags: -A (no autotag), -q (quiet, no prompts).
uv run beet -c "${CONFIG_PATH}" import -A -q "${MUSIC_DIR}"
echo "imported into: ${LIBRARY_PATH}"
