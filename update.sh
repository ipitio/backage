#!/bin/bash
# Update the number of pulls for each package in pkg.json
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio

if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null; then
    sudo apt-get update
    sudo apt-get install curl jq -y
fi

while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    raw_pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "(?<=Total downloads</span>\n          <h3 title=\"$raw_pulls\">)[^<]*")

    if [ -n "$pulls" ]; then

        # Update the index.json file with the new pulls
        jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" --arg raw_pulls "$raw_pulls" '
            if . == [] then
                [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls}]
            else
                map(if .owner == $owner and .repo == $repo and .image == $image then
                        . + {pulls: $pulls, raw_pulls: $raw_pulls}
                    else
                        .
                    end)
                + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls, raw_pulls: $raw_pulls}] end)
            end' index.json >index.tmp.json
        mv index.tmp.json index.json

        # Update the README.md file with the new badge
        if ! grep -q "$owner/$repo/$image" README.md; then
            sed -i "s/\\n\[\/\/\]: # (add next one here)/\[!\[$owner\/$repo\/$image\](https:\/\/img.shields.io\/badge\/dynamic\/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.image%3D%3D%22$image%22)%5D.pulls&logo=github&label=$image)\](https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$image) \\n\[\/\/\]: # (add next one here)/" README.md
        fi
    fi
done <pkg.txt

for i in $(jq -r '.[] | @base64' index.json); do
    _jq() {
        echo "$i" | base64 --decode | jq -r "$@"
    }
    raw_pulls=$(_jq '.raw_pulls')
    pulls=$(_jq '.pulls')
    owner=$(_jq '.owner')
    repo=$(_jq '.repo')
    image=$(_jq '.image')
    printf "%s (%s) pulls\t\t| %s/%s/%s\n" "$pulls" "$raw_pulls" "$owner" "$repo" "$image"
done
