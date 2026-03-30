#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

bash "$test_dir/discovery.sh"
bash "$test_dir/arrays.sh"