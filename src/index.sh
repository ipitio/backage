#!/bin/bash

if [ ! -s "$1" ]; then
    echo "Empty file: $1"
    rm -f "$1"
elif [[ "$1" == *.json ]]; then
    jq -e . "$1" &>/dev/null || echo "Invalid json: $1"
else
    xmllint --noout "$1" &>/dev/null || echo "Invalid xml: $1"
fi
