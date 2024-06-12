#!/bin/bash
# Update the number of pulls for each package in pkg.txt
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio
#
# shellcheck disable=SC2015

# check if curl and jq are installed
if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq -y
fi

# clean pkg.txt
awk '{print tolower($0)}' pkg.txt | sort -u | while read -r line; do
    grep -i "^$line$" pkg.txt
done >pkg.tmp.txt
mv pkg.tmp.txt pkg.txt
[ -z "$(tail -c 1 pkg.txt)" ] || echo >>pkg.txt

# update the index with new counts
[ -f index.json ] || echo "[]" >index.json # create the index if it does not exist
while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    [ -f index.json ] || echo "[]" >index.json

    # manual update: skip if the package is already in the index; the rest are updated on a consistent basis
    if [ "$1" = "1" ]; then
        jq -e --arg owner "$owner" --arg repo "$repo" --arg image "$image" '
            any(.[]; .owner == $owner and .repo == $repo and .image == $image)' index.json >/dev/null && continue || :
    fi

    # get the number of pulls, skipping nans
    date=$(date -u +"%Y-%m-%d")
    html=$(curl -sSLNZ "https://github.com/$owner/$repo/pkgs/container/$image")
    raw_pulls=$(echo -e "$html" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    [[ "$raw_pulls" =~ ^[0-9]+$ ]] || continue
    pulls=$(echo "$raw_pulls" | awk '{ split("k M B T P E Z Y", v); s=0; while( $1>999.9 ) { $1/=1000; s++ } print int($1*10)/10 v[s] }') || pulls=-1
    jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" --arg raw_pulls "$raw_pulls" --arg date "$date" '
        if . == [] then
            [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}]
        else
            map(if .owner == $owner and .repo == $repo and .image == $image then .pulls = $pulls | .raw_pulls = $raw_pulls | .raw_pulls_all[($date)] = $raw_pulls else . end)
            + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}] end)
        end' index.json >index.tmp.json
    mv index.tmp.json index.json
done <pkg.txt

# sort the index by the number of raw_pulls greatest to smallest
jq 'sort_by(.raw_pulls | tonumber) | reverse' index.json >index.tmp.json
mv index.tmp.json index.json

# update the README template with badges...
[ ! -f README.md ] || rm -f README.md # remove the old README
\cp .README.md README.md              # copy the template
echo "Total pulls:"
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
    printf "%s (%s) %s/%s/%s\n" "$pulls" "$raw_pulls" "$owner" "$repo" "$image"

    # ...that have not been added yet
    grep -q "$owner/$repo/$image" README.md || perl -0777 -pe '
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $image = $ENV{"image"};

    # decode percent-encoded characters
    for ($owner, $repo, $image) {
        s/%/%25/g;
    }
    my $label = $image;
    $label =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;

    # add new badge
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner\/$repo\/$image\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.image%3D%3D%22$image%22)%5D.pulls\&label=$label\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$image\)\n\n/g;
' README.md >README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
done
