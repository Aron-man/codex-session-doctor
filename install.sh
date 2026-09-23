#!/bin/sh
set -eu

usage() {
  printf 'Usage: sh install.sh --repo OWNER/REPO [--version TAG] [--install-dir DIR]\n' >&2
  exit 2
}

repo=
version=latest
install_dir=${HOME}/.local/bin
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo|--version|--install-dir)
      [ "$#" -ge 2 ] || usage
      case "$1" in
        --repo) repo=$2 ;;
        --version) version=$2 ;;
        --install-dir) install_dir=$2 ;;
      esac
      shift 2 ;;
    *) usage ;;
  esac
done

case "$repo" in
  */*) ;;
  *) usage ;;
esac
case "$repo" in
  *[!A-Za-z0-9._/-]*|/*|*/|*//*|*/*/*|.*/*|*/.* ) usage ;;
esac
case "$version" in
  ''|*[!A-Za-z0-9._-]* ) usage ;;
esac
[ -n "$install_dir" ] || usage

case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  *) printf 'Unsupported operating system\n' >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64|aarch64) arch=arm64 ;;
  x86_64|amd64) arch=amd64 ;;
  *) printf 'Unsupported architecture\n' >&2; exit 1 ;;
esac

asset="codex-doctor-${os}-${arch}"
if [ "$version" = latest ]; then
  base="https://github.com/${repo}/releases/latest/download"
else
  base="https://github.com/${repo}/releases/download/${version}"
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
curl -fL --retry 2 -o "$tmp/$asset.tar.gz" "$base/$asset.tar.gz"
curl -fL --retry 2 -o "$tmp/$asset.sha256" "$base/$asset.sha256"
expected=$(awk 'NR == 1 { print $1 }' "$tmp/$asset.sha256")
checksum_name=$(awk 'NR == 1 { print $2 }' "$tmp/$asset.sha256")
[ "$checksum_name" = "$asset.tar.gz" ] || { printf 'Checksum filename mismatch\n' >&2; exit 1; }
case "$expected" in
  *[!0-9a-fA-F]*|'') printf 'Invalid checksum file\n' >&2; exit 1 ;;
esac
[ "${#expected}" -eq 64 ] || { printf 'Invalid checksum length\n' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$tmp/$asset.tar.gz" | awk '{ print $1 }')
else
  actual=$(shasum -a 256 "$tmp/$asset.tar.gz" | awk '{ print $1 }')
fi
[ "$expected" = "$actual" ] || { printf 'Checksum mismatch\n' >&2; exit 1; }
tar -xOf "$tmp/$asset.tar.gz" codex-doctor > "$tmp/codex-doctor"
[ -s "$tmp/codex-doctor" ] || { printf 'Missing codex-doctor in archive\n' >&2; exit 1; }
chmod 755 "$tmp/codex-doctor"
mkdir -p "$install_dir"
staged=$(mktemp "$install_dir/.codex-doctor.XXXXXXXX")
trap 'rm -f "$staged"; rm -rf "$tmp"' EXIT HUP INT TERM
cat "$tmp/codex-doctor" > "$staged"
chmod 755 "$staged"
mv -f "$staged" "$install_dir/codex-doctor"
printf 'Installed %s\n' "$install_dir/codex-doctor"
