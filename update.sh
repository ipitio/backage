#!/bin/bash

if ! command -v jq &>/dev/null; then
    sudo apt-get update
    sudo apt-get install jq -y
fi

while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    data=$(curl -sSLNZ https://github.com/"$owner"/"$repo"/pkgs/container/"$image")
    query='(?<=Total downloads</span>\n          <h3 title=")[^<]*'
    pulls=$(grep -Pzo "$query" <<<"$data")
    query="${query//\"/\"$pulls\">}"
    pulls=$(grep -Pzo "$query" <<<"$data")

    if [ -n "$pulls" ] && [ "$pulls" -eq "$pulls" ] 2>/dev/null && [ "$pulls" -gt 0 ]; then
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
