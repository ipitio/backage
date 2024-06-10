#!/bin/bash
# Update the number of pulls for each package in pkg.txt
# Usage: ./update.sh
# Dependencies: curl, jq
# Copyright (c) ipitio

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

# setup templates
[ -f index.json ] || echo "[]" >index.json
[ ! -f README.md ] || rm -f README.md
\cp .README.md README.md

# update the index with new counts if we get a response
while IFS= read -r line; do
    owner=$(echo "$line" | cut -d'/' -f1)
    repo=$(echo "$line" | cut -d'/' -f2)
    image=$(echo "$line" | cut -d'/' -f3)
    raw_pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo '(?<=Total downloads</span>\n          <h3 title=")\d*')
    pulls=$(curl -sSLN https://github.com/"$owner"/"$repo"/pkgs/container/"$image" | grep -Pzo "(?<=Total downloads</span>\n          <h3 title=\"$raw_pulls\">)[^<]*")
    date=$(date -u +"%Y-%m-%d")

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

# update the README template with new badges
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

    grep -q "$owner/$repo/$image" README.md || perl -0777 -pe '
    my $owner = $ENV{"owner"};
    my $repo = $ENV{"repo"};
    my $image = $ENV{"image"};

    # replace any "%" with "%25"
    $owner =~ s/%/%25/g;
    $repo =~ s/%/%25/g;
    $image =~ s/%/%25/g;

    s/\n\n(\[!\[.*)\n\n/\n\n$1 \[!\[$owner\/$repo\/$image\]\(https:\/\/img.shields.io\/badge\/dynamic\/json\?url=https%3A%2F%2Fraw.githubusercontent.com%2Fipitio%2Fghcr-pulls%2Fmaster%2Findex.json\&query=%24%5B%3F(%40.owner%3D%3D%22$owner%22%20%26%26%20%40.repo%3D%3D%22$repo%22%20%26%26%20%40.image%3D%3D%22$image%22)%5D.pulls\&label=$image\)\]\(https:\/\/github.com\/$owner\/$repo\/pkgs\/container\/$image\)\n\n/g;
' README.md > README.tmp && [ -f README.tmp ] && mv README.tmp README.md || :

    printf "%s (%s) pulls \t\t\t | %s/%s/%s\n" "$pulls" "$raw_pulls" "$owner" "$repo" "$image"
done
