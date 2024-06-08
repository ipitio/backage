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
    query='(?<=Total downloads</span>\n          <h3 title=")\d*'
    pulls=$(curl -sSLNZ https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "$query")
    query="(?<=Total downloads</span>\n          <h3 title=\"$pulls\">)[^<]*"
    pulls=$(curl -sSLNZ https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "$query")

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
            end' index.json >index.json.tmp
        mv index.json.tmp index.json
    fi
done <pkg.txt
