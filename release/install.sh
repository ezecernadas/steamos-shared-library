#!/usr/bin/env bash
# Install or update steamshare on Linux with one command:
#
#   curl -fsSL https://github.com/ezecernadas/steamshare/releases/download/v<version>/install.sh | sudo bash
#
# Install options go after `bash -s --`, for example:
#
#   curl -fsSL <url> | sudo bash -s -- --no-watch
#
# This file is the template: release/build_assets.py stamps the release's
# version into the copy each GitHub release attaches, so that copy downloads
# the source archive of its own release. Running the template refuses.
#
# It holds no decisions (CONTRIBUTING.md, rule 2). It downloads the archive,
# extracts it to a temporary folder, and runs src/steamshare/release_setup.py
# from there with the system's python3. That module chooses the lasting
# folder (/opt/steamshare, or ~/.local/share/steamshare on a read-only root
# such as SteamOS's), places the release there, and starts `steamshare
# install` from it. Running the command again updates.
#
# Two traps of `curl | bash`:
# - bash reads this script from its standard input as it runs, so everything
#   is inside `main`, called on the last line: bash has read the whole file
#   before anything runs.
# - A program started from here would read the rest of the script as its
#   input, and install asks questions. So programs read the terminal
#   (/dev/tty) instead. Without one, as in CI, they read nothing, and install
#   says which options answer its questions in advance (--yes,
#   --partition-uuid, --library-folder).
set -euo pipefail

version="@STEAMSHARE_VERSION@"
repository_url="https://github.com/ezecernadas/steamshare"

fail() {
    echo "Error: $*" >&2
    exit 1
}

download() {
    local url="$1" file="$2"
    if command -v curl >/dev/null; then
        curl --fail --silent --show-error --location --output "${file}" "${url}"
    elif command -v wget >/dev/null; then
        wget --quiet --output-document="${file}" "${url}"
    else
        fail "downloading steamshare needs curl or wget, and neither is on PATH. Install one" \
            "from your distribution, then run the command again."
    fi
}

main() {
    # The template's placeholder starts with "@"; a built copy has a version.
    if [[ "${version}" == @* ]]; then
        fail "this is the unbuilt template of the installer. Use the install.sh attached to" \
            "a release (see the README), or run ./steamshare install from a clone."
    fi
    if [[ "${EUID}" -ne 0 ]]; then
        fail "installing steamshare needs root. Run it through sudo:" \
            "curl -fsSL ${repository_url}/releases/download/v${version}/install.sh | sudo bash"
    fi
    if ! command -v python3 >/dev/null; then
        fail "steamshare needs python3, but none is on PATH. Install Python 3 from your" \
            "distribution, then run the command again."
    fi

    # Global, not local: the EXIT trap runs after main has returned.
    temporary_folder="$(mktemp -d)"
    trap 'rm -rf "${temporary_folder}"' EXIT

    local archive="${temporary_folder}/steamshare.tar.gz"
    echo "Downloading steamshare ${version}..."
    download "${repository_url}/archive/refs/tags/v${version}.tar.gz" "${archive}"
    # --no-same-owner: as root, tar would otherwise keep the archive's owners.
    tar --extract --gzip --no-same-owner --file "${archive}" --directory "${temporary_folder}"
    # GitHub names the archive's top folder <repository>-<version>.
    local release="${temporary_folder}/steamshare-${version}"
    if [[ ! -x "${release}/steamshare" ]]; then
        fail "the downloaded archive has no steamshare launcher in it. Run the command" \
            "again; if it keeps failing, report it at ${repository_url}/issues."
    fi

    local input=/dev/null
    if { true </dev/tty; } 2>/dev/null; then
        input=/dev/tty
    fi

    # Exits with an error message when python3 is older than steamshare needs.
    python3 "${release}/src/steamshare/python_requirement.py" <"${input}"
    # -P (Python 3.11+, checked above): the current folder is not searched for
    # modules, so a `steamshare` folder there cannot stand in for the release.
    PYTHONPATH="${release}/src" python3 -P -m steamshare.release_setup \
        "${release}" "${version}" "$@" <"${input}"
}

main "$@"
