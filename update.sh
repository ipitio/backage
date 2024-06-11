#!/bin/bash
# Update the number of pulls for each package in pkg.txt
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio
#
# shellcheck disable=SC2015

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

# set up templates
[ -f index.json ] || echo "[]" >index.json
[ ! -f README.md ] || rm -f README.md
\cp .README.md README.md

# update the index with new counts...
while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    html=$(curl -sSLNZ "https://github.com/$owner/$repo/pkgs/container/$image")
    raw_pulls=$(echo -e "$html" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    pulls=$(echo -e "$html" | grep -Pzo "(?<=Total downloads</span>\n          <h3 title=\"$raw_pulls\">)[^<]*")
    pulls="${pulls,,}"
    date=$(date -u +"%Y-%m-%d")

    # ...if we get a response
    if [ -n "$pulls" ]; then
        if [ "$1" = "1" ]; then # manual update
            jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" --arg raw_pulls "$raw_pulls" --arg date "$date" '
                if . == [] then
                    [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}]
                else
                    map(if .owner == $owner and .repo == $repo and .image == $image then . else . end)
                    + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}] end)
                end' index.json >index.tmp.json
        else
            jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" --arg raw_pulls "$raw_pulls" --arg date "$date" '
                if . == [] then
                    [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}]
                else
                    map(if .owner == $owner and .repo == $repo and .image == $image then .pulls = $pulls | .raw_pulls = $raw_pulls | .raw_pulls_all[($date)] = $raw_pulls else . end)
                    + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}] end)
                end' index.json >index.tmp.json
        fi

        mv index.tmp.json index.json
    fi
done <pkg.txt

# sort the index by the number of raw_pulls greatest to smallest
jq 'sort_by(.raw_pulls | tonumber) | reverse' index.json >index.tmp.json
mv index.tmp.json index.json

# update the README template with badges...
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
    printf "%s (%s) pulls \t\t\t | %s/%s/%s\n" "$pulls" "$raw_pulls" "$owner" "$repo" "$image"

    # ...that have not been added yet
    grep -q "$owner/$repo/$image" README.md || perl -0777 -pe '
    # map "%" to "%25"
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $image = $ENV{"image"};
    for ($owner, $repo, $image) {
        s/%/%25/g;
    }

    # map percent-encoded characters
    my $label = $image;
    $label =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/eg;

    # add new badge
    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner\/$repo\/$image\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.image%3D%3D%22$image%22)%5D.pulls\&label=$label\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$image\)\n\n/g;
' README.md > README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :
done
