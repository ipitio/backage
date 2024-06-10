#!/bin/bash
# Update the number of pulls for each package in pkg.json
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq -y
fi

rm -f README.md
\cp .README.md README.md

for i in $(jq -r '.[] | @base64' index.json); do
    _jq() {
        echo "$i" | base64 --decode | jq -r "$@"
    }

    owner=$(_jq '.owner')
    repo=$(_jq '.repo')
    image=$(_jq '.image')
    pulls=$(_jq '.pulls')
    raw_pulls=$(_jq '.raw_pulls')
    export owner repo image

    # add to the README with all the new badges that were not in the README
    # if none of $owner/$repo/$image contains "%2F" then badge is safe
    if ! echo "$owner/$repo/$image" | grep -q "%2F"; then
        grep -q "$owner/$repo/$image" README.md || perl -0777 -pe '
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $image = $ENV{"image"};
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner\/$repo\/$image\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.image%3D%3D%22$image%22)%5D.pulls\&label=$image\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$image\)\n\n/g;
' README.md > README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
    fi

    printf "%s (%s) pulls \t\t\t | %s/%s/%s\n" "$pulls" "$raw_pulls" "$owner" "$repo" "$image"
done
