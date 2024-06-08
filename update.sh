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
    pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "(?<=Total downloads</span>\n          <h3 title=\"$pulls\">)[^<]*")

    if [ -n "$pulls" ]; then
        jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" '
            if . == [] then
                [{owner: $owner, repo: $repo, image: $image, pulls: $pulls}]
            else
                map(if .owner == $owner and .repo == $repo and .image == $image then
                        .pulls = $pulls
                    else
                        .
                    end)
                + (if any(.[]; .owner == $owner and .repo == $repo and .image == $image) then [] else [{owner: $owner, repo: $repo, image: $image, pulls: $pulls}] end)
            end' index.json >index.tmp.json
        mv index.tmp.json index.json
    fi
done <pkg.txt

for i in $(jq -r '.[] | @base64' index.json); do
    _jq() {
        echo "$i" | base64 --decode | jq -r "$@"
    }
    pulls=$(_jq '.pulls')
    owner=$(_jq '.owner')
    repo=$(_jq '.repo')
    image=$(_jq '.image')
    printf "%s pulls\t\t| %s/%s/%s\n" "$pulls" "$owner" "$repo" "$image"
done
