#!/usr/bin/env bash
# Install picinplace

# figure out where this file is located even if it is being run from another location
# or as a symlink
# shellcheck disable=SC2296
SOURCE="${BASH_SOURCE[0]:-${(%):-%x}}"
while [ -h "$SOURCE" ]; do # resolve $SOURCE until the file is no longer a symlink
  DIR="$( cd -P "$( dirname "$SOURCE" )" >/dev/null && pwd )"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE" # if $SOURCE was a relative symlink, we need to resolve it relative to the path where the symlink file was located
done
REPO_ROOT="$( cd -P "$( dirname "$SOURCE" )" >/dev/null && pwd )"

SERVICE_NAME='picinplace'

mkdir -p "${HOME}/.config/systemd/user"
cp "${REPO_ROOT}/sys/${SERVICE_NAME}.service" "${HOME}/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable picinplace.service
systemctl --user start picinplace.service

