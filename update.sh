#!/bin/bash
# Update the number of pulls for each package in pkg.json
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq -y
fi

# sort pkg.txt and remove duplicates
sort -u pkg.txt -o pkg.txt

# create the index if it does not exist
[ -f index.json ] || echo "[]" >index.json

# setup the README template
[ ! -f README.md ] || rm -f README.md
\cp .README.md README.md

# loop through each package in pkg.txt
while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    raw_pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "(?<=Total downloads</span>\n          <h3 title=\"$raw_pulls\">)[^<]*")
    date=$(date -u +"%Y-%m-%d")

    # update the index with the new counts if we got a response
    if [ -n "$pulls" ]; then
        jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" --arg raw_pulls "$raw_pulls" --arg date "$date" '
            if . == [] then
                [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}]
            else
                map(if .owner == $owner and .repo == $repo and .image == $image then .pulls = $pulls | .raw_pulls = $raw_pulls | .raw_pulls_all[($date)] = $raw_pulls else . end)
                + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls, raw_pulls_all: {($date): $raw_pulls}}] end)
            end' index.json >index.tmp.json
        mv index.tmp.json index.json
    fi
done <pkg.txt

# sort the index by the number of raw_pulls and remove the latest date if it is the same as the previous
jq 'sort_by(.raw_pulls | tonumber) | reverse | map(.raw_pulls_all |= with_entries(select(.key != keys[-1])))' index.json >index.tmp.json
mv index.tmp.json index.json

# loop through each package in the index
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

    # update the README template with all working badges
    # if none of $owner/$repo/$image contains "%2F" then badge works with shields.io
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
