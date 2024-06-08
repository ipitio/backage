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
    pulls=$(grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*' <<<"$data")

    if [ -n "$pulls" ] && [ "$pulls" -eq "$pulls" ] 2>/dev/null && [ "$pulls" -gt 0 ]; then
        jq --arg owner "$owner" --arg repo "$repo" --arg image "$image" --arg pulls "$pulls" 'select(.owner == $owner and .repo == $repo and .image == $image) |= . + {"pulls":$pulls}' index.json >tmp.$$.json && mv tmp.$$.json index.json
    fi
done <pkg.txt
