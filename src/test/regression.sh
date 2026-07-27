#!/bin/bash

set -euo pipefail

test_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

bash "$test_dir/quality.sh"
bash "$test_dir/shellcheck.sh"
bash "$test_dir/workflow.sh"
bash "$test_dir/python.sh"
