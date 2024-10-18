#!/bin/bash

verify_deps() {
    apt_install() {
        sudo apt-get install curl jq parallel sqlite3 sqlite3-pcre zstd libxml2-utils -yqq
    }

    if ! command -v curl &>/dev/null || ! command -v jq &>/dev/null || ! command -v sqlite3 &>/dev/null || ! command -v zstd &>/dev/null || ! command -v parallel &>/dev/null || ! command -v xmllint &>/dev/null || [ ! -f /usr/lib/sqlite3/pcre.so ]; then
        echo "Installing dependencies..."
        if ! apt_install; then
            sudo apt-get update
            apt_install
        fi
    fi

    if ! yq -V | grep -q mikefarah; then
        echo "Installing yq..."
        sudo rm -f /usr/bin/yq
        wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O yq
        sudo mv yq /usr/bin/yq
        sudo chmod +x /usr/bin/yq
    fi

    echo "Dependencies verified!"
}
