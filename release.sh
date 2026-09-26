#!/bin/bash
# Release a new version: bump, tag, pin the PKGBUILD checksum, build the Arch
# package and publish a GitHub release with it attached.
#
#   ./release.sh 0.2.0            release v0.2.0
#   ./release.sh 0.2.0 --aur      ...and push the PKGBUILD to the AUR
#   ./release.sh 0.2.0 --dry-run  do everything locally; push and publish nothing
#   ./release.sh --aur-only       push the current aur/ files to the AUR
set -euo pipefail
cd "$(dirname "$0")"

REPO=bh808303/yt-pplayer
PKG=yt-pplayer
AUR_URL=ssh://aur@aur.archlinux.org/$PKG.git

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

version="" aur=0 dry=0 aur_only=0
for arg in "$@"; do
  case $arg in
    --aur) aur=1 ;;
    --dry-run) dry=1 ;;
    --aur-only) aur_only=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $arg" ;;
    *) version=$arg ;;
  esac
done

run() {  # run, or only show it in dry-run mode
  if ((dry)); then echo "   (dry-run) $*"; else "$@"; fi
}

publish_aur() {
  step "Pushing PKGBUILD to the AUR"
  local ver; ver=$(sed -n 's/^\tpkgver = //p' aur/.SRCINFO)-$(sed -n 's/^\tpkgrel = //p' aur/.SRCINFO)
  if ((dry)); then
    echo "   (dry-run) push $PKG $ver to $AUR_URL"
    return
  fi
  local tmp; tmp=$(mktemp -d)
  git clone -q "$AUR_URL" "$tmp" 2>/dev/null || die "cannot reach $AUR_URL (AUR account and SSH key set up?)"
  cp aur/PKGBUILD aur/.SRCINFO "$tmp/"
  if [[ -z $(git -C "$tmp" status --porcelain) ]]; then
    echo "AUR already has $ver"
  else
    git -C "$tmp" add PKGBUILD .SRCINFO
    git -C "$tmp" commit -q -m "Update to $ver"
    git -C "$tmp" push -q origin HEAD:master
    echo "https://aur.archlinux.org/packages/$PKG"
  fi
  rm -rf "$tmp"
}

if ((aur_only)); then
  publish_aur
  exit 0
fi

[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: ./release.sh X.Y.Z [--aur] [--dry-run]"
tag=v$version

step "Checking the repository"
for cmd in gh makepkg namcap curl; do command -v $cmd >/dev/null || die "$cmd is not installed"; done
[[ $(git branch --show-current) == main ]] || die "not on main"
[[ -z $(git status --porcelain) ]] || die "uncommitted changes; commit or stash them first"
git fetch -q origin
[[ $(git rev-parse HEAD) == $(git rev-parse origin/main) ]] || die "main is not in sync with origin/main"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
((dry)) || gh auth status >/dev/null 2>&1 || die "gh is not logged in (gh auth login)"
previous=$(git describe --tags --abbrev=0 2>/dev/null || true)
current=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
echo "current version $current, previous tag ${previous:-none}, releasing $tag"

if ((!dry)); then
  echo
  echo "This pushes $tag to GitHub and publishes a release."
  ((aur)) && echo "It also pushes the PKGBUILD to the AUR."
  read -r -p "Continue? [y/N] " answer
  [[ $answer == [yY] ]] || die "aborted"
fi

if ((dry)); then
  # Work on a throwaway clone so nothing here changes.
  work=$(mktemp -d)
  git clone -q . "$work"
  cd "$work"
  echo "dry-run: working in $work"
fi

step "Bumping version to $version"
sed -i "s/^version = \".*\"/version = \"$version\"/" pyproject.toml
sed -i -e "s/^pkgver=.*/pkgver=$version/" -e "s/^pkgrel=.*/pkgrel=1/" aur/PKGBUILD
git add pyproject.toml aur/PKGBUILD
git commit -q -m "Release $tag"
git tag -a "$tag" -m "$PKG $version"
run git push -q origin main "$tag"

step "Pinning the source checksum"
tarball=$(mktemp -d)/$PKG-$version.tar.gz
if ((dry)); then
  # GitHub's archive isn't there in a dry run; a local one tests the same steps.
  git archive --format=tar.gz --prefix="$PKG-$version/" -o "$tarball" "$tag"
else
  for _ in 1 2 3 4 5; do
    curl -fsSL -o "$tarball" "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz" && break
    sleep 3
  done
  [[ -s $tarball ]] || die "could not download the $tag source archive"
fi
sum=$(sha256sum "$tarball" | cut -d' ' -f1)
sed -i "s/^sha256sums=.*/sha256sums=('$sum')/" aur/PKGBUILD
(cd aur && makepkg --printsrcinfo > .SRCINFO)
git add aur/PKGBUILD aur/.SRCINFO
git commit -q -m "Pin $tag checksum in PKGBUILD"
run git push -q origin main
echo "sha256 $sum"

step "Building the package"
build=$(mktemp -d)
cp aur/PKGBUILD "$build/"
if ((dry)); then
  cp "$tarball" "$build/"
  sed -i "s|^source=.*|source=(\"$PKG-$version.tar.gz\")|" "$build/PKGBUILD"
fi
(cd "$build" && makepkg -f --noconfirm >/dev/null 2>"$build/makepkg.log") || { cat "$build/makepkg.log" >&2; die "makepkg failed"; }
pkgfile="$build/$PKG-$version-1-any.pkg.tar.zst"
[[ -f $pkgfile ]] || die "makepkg did not produce $pkgfile"
pkgsum=$(sha256sum "$pkgfile" | cut -d' ' -f1)
echo "$pkgfile"
namcap "$pkgfile" | grep -v -e "uninstalled dependency" -e "may not be needed" -e "Dependency bash detected" || true

step "Publishing the GitHub release"
notes=$(mktemp)
{
  if [[ -n $previous ]]; then
    echo "## Changes since $previous"
    echo
    git log --no-merges --format='- %s' "$previous..$tag" | grep -v -e "^- Release v" -e "^- Pin v" || echo "- Maintenance release"
    echo
  fi
  cat <<EOF
## Install (Arch / Omarchy)

\`\`\`sh
curl -LO https://github.com/$REPO/releases/download/$tag/$(basename "$pkgfile")
sudo pacman -U $(basename "$pkgfile")
\`\`\`

pacman installs the dependencies from the official repos. The package is unsigned, which is why it's downloaded first: pacman refuses unsigned packages straight from a URL.

SHA-256: \`$pkgsum\`

Or build it yourself: \`git clone https://github.com/$REPO && cd $PKG/aur && makepkg -si\`
EOF
} > "$notes"
if ((dry)); then
  echo "   (dry-run) gh release create $tag $(basename "$pkgfile") --title \"$PKG $version\""
  echo "---- release notes ----"; cat "$notes"; echo "-----------------------"
else
  gh release create "$tag" "$pkgfile" --repo "$REPO" --title "$PKG $version" --notes-file "$notes"
fi

((aur)) && publish_aur

step "Done"
((dry)) && echo "dry-run finished; nothing was pushed or published (scratch clone: $work)"
exit 0
