#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$test_dir/../.." && pwd)
site_dir="$repo_dir/site"
dependency_dir=/opt/bkg-site-dev

for command in node npm; do
	command -v "$command" >/dev/null 2>&1 || {
		echo "Missing $command; run this command inside the bkg test image" >&2
		exit 1
	}
done

case $(node --version) in
	v24.*) ;;
	*)
		echo "Frontend checks require the Node 24 feature line" >&2
		exit 1
		;;
esac
[ "$(npm --version)" = "11.17.0" ] || {
	echo "Frontend checks require npm 11.17.0" >&2
	exit 1
}

for file in package.json package-lock.json; do
	cmp -s "$site_dir/$file" "$dependency_dir/$file" || {
		echo "Frontend dependency files changed; rebuild the bkg test image" >&2
		exit 1
	}
done

workspace=$(mktemp -d /tmp/bkg-site-test.XXXXXX)
cleanup() {
	rm -rf "$workspace"
}
trap cleanup EXIT

cp "$site_dir/package.json" "$site_dir/package-lock.json" \
	"$site_dir/astro.config.ts" "$site_dir/tsconfig.json" "$workspace/"
cp -R "$site_dir/public" "$site_dir/scripts" "$site_dir/src" \
	"$site_dir/tests" "$workspace/"
ln -s "$dependency_dir/node_modules" "$workspace/node_modules"

export ASTRO_TELEMETRY_DISABLED=1
cd "$workspace"
npm test
npm run check
npm run build

test -f "dist/.bkg-site-manifest.json" || {
	echo "Astro build did not produce the site-shell manifest" >&2
	exit 1
}
test -f "dist/index.html" || {
	echo "Astro build did not produce the root dashboard entrypoint" >&2
	exit 1
}
grep -Fq '"entrypoint": "index.html"' dist/.bkg-site-manifest.json || {
	echo "Site-shell manifest does not declare the root entrypoint" >&2
	exit 1
}
test ! -e "dist/.bkg-site/candidate/index.html" || {
	echo "Astro build retained the retired dashboard candidate" >&2
	exit 1
}
if grep -R -Fq '__BKG_DATA_ROOT__' dist; then
	echo "Astro build retained an unhydrated data-root token" >&2
	exit 1
fi
