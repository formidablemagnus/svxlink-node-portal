#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly WORK_DIR="/var/lib/svxlink-node-updater"
readonly SOURCE_DIR="$WORK_DIR/svxlink"
readonly BUILD_DIR="$SOURCE_DIR/src/build"
readonly REPOSITORY="https://github.com/sm0svx/svxlink.git"
readonly RELEASE_API="https://api.github.com/repos/sm0svx/svxlink/releases/latest"
readonly RELEASE_TAG_API="https://api.github.com/repos/sm0svx/svxlink/releases/tags"
export HOME="$WORK_DIR"

fail() {
  printf 'Fel: %s\n' "$*" >&2
  exit 1
}

if [[ $# -ne 1 || ! "$1" =~ ^(prerelease|release|[0-9]+(\.[0-9]+){1,3})$ ]]; then
  fail "Ange prerelease, release eller ett versionsnummer."
fi
channel="$1"

if ! command -v flock >/dev/null 2>&1; then
  fail "flock saknas. Installera util-linux."
fi
exec 9>"$WORK_DIR/update.lock"
if ! flock -n 9; then
  fail "En SvxLink-uppdatering körs redan."
fi

for executable in cmake curl dpkg-deb doxygen g++ git groff make python3; do
  if ! command -v "$executable" >/dev/null 2>&1; then
    fail "Byggverktyget saknas: $executable."
  fi
done

if [[ -e "$SOURCE_DIR" && ! -d "$SOURCE_DIR/.git" ]]; then
  fail "Uppdateringskatalogen finns men innehåller ingen Git-arbetskopia: $SOURCE_DIR"
fi

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  printf 'Hämtar SvxLink-källkod...\n' >&2
  git clone --branch master "$REPOSITORY" "$SOURCE_DIR" >&2
fi

origin="$(git -C "$SOURCE_DIR" remote get-url origin)"
if [[ "$origin" != "$REPOSITORY" ]]; then
  fail "SvxLink-arbetskopians origin måste vara $REPOSITORY."
fi
if [[ -n "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=no)" ]]; then
  fail "SvxLink-arbetskopian har ändrade spårade filer. Återställ dem innan uppdatering."
fi

if [[ "$channel" == "prerelease" ]]; then
  printf 'Hämtar senaste pre-release från master...\n' >&2
  git -C "$SOURCE_DIR" fetch origin master >&2
  git -C "$SOURCE_DIR" checkout --detach origin/master >&2
else
  if [[ "$channel" == "release" ]]; then
    printf 'Hämtar senaste stabila SvxLink-release...\n' >&2
    release_url="$RELEASE_API"
  else
    printf 'Hämtar SvxLink-release %s...\n' "$channel" >&2
    release_url="$RELEASE_TAG_API/$channel"
  fi
  release_tag="$(
    curl --fail --silent --show-error --location --max-time 30 \
      --proto '=https' --proto-redir '=https' --tlsv1.2 "$release_url" |
      python3 -c '
import json
import re
import sys

release = json.load(sys.stdin)
tag = release.get("tag_name")
if release.get("prerelease") is not False or release.get("draft") is not False or not isinstance(tag, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", tag):
    raise SystemExit("GitHub returnerade ingen giltig stabil release-tagg.")
if sys.argv[1] != "release" and tag != sys.argv[1]:
    raise SystemExit("GitHub returnerade en annan release än den begärda.")
print(tag)
' "$channel"
  )" || fail "Kunde inte hitta den begärda stabila releasen från GitHub."
  git -C "$SOURCE_DIR" fetch origin "refs/tags/$release_tag:refs/tags/$release_tag" >&2
  git -C "$SOURCE_DIR" checkout --detach "$release_tag" >&2
fi

if [[ ! -f "$SOURCE_DIR/src/CMakeLists.txt" ]]; then
  fail "SvxLink-källkoden saknar CMakeLists.txt i $SOURCE_DIR/src."
fi

mkdir -p "$BUILD_DIR"
log_file="$(mktemp "$WORK_DIR/update.XXXXXX")"
trap 'rm -f -- "$log_file"' EXIT

run_logged() {
  local description="$1"
  shift
  printf '%s\n' "$description" >&2
  if "$@" >"$log_file" 2>&1; then
    return
  fi
  tail -n 40 "$log_file" >&2 || true
  return 1
}

run_logged "Konfigurerar SvxLink-bygget..." \
  cmake \
  -S "$SOURCE_DIR/src" \
  -B "$BUILD_DIR" \
  -DUSE_QT=OFF \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DSYSCONF_INSTALL_DIR=/etc \
  -DLOCAL_STATE_DIR=/var \
  -DWITH_SYSTEMD=ON \
  -DCPACK_GENERATOR=DEB

find "$BUILD_DIR" -maxdepth 1 -type f -name 'svxlink-*-Linux.deb' -delete
run_logged "Bygger paketet (make -j4 all doc package)..." \
  make -C "$BUILD_DIR" -j4 all doc package

mapfile -t packages < <(
  find "$BUILD_DIR" -maxdepth 1 -type f -name 'svxlink-*-Linux.deb' -printf '%f\n'
)
if (( ${#packages[@]} != 1 )); then
  fail "Förväntade ett nybyggt svxlink-*-Linux.deb-paket, hittade ${#packages[@]}."
fi

package_path="$BUILD_DIR/${packages[0]}"
package_name="$(dpkg-deb --field "$package_path" Package)"
version="$(dpkg-deb --field "$package_path" Version)"
if [[ "$package_name" != "svxlink" || ! "$version" =~ ^[A-Za-z0-9.+:~_-]{1,128}$ ]]; then
  fail "Det byggda Debian-paketet har ett ogiltigt paketnamn eller versionsnummer."
fi

python3 -c '
import json
import sys

print(json.dumps({
    "channel": sys.argv[1],
    "package": sys.argv[2],
    "version": sys.argv[3],
}))
' "$channel" "${packages[0]}" "$version"
