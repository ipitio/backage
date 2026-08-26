#!/bin/bash

set -euo pipefail

source_repository=${BKG_SOURCE_REPOSITORY:-ipitio/backage}
target_owner=""
target_name=""
build_mode=auto

usage() {
	cat <<'EOF'
Usage: setup-fork.sh [-o OWNER] [-n NAME] [-s OWNER/REPOSITORY] [-b | -B]

Create and initialize a default-branch-only Backage fork with GitHub CLI.

Options:
  -o, --owner OWNER        Personal account or organization for the fork
  -n, --name NAME          Fork repository name (default: source name)
  -s, --source REPOSITORY  Source repository (default: ipitio/backage)
  -b, --build              Dispatch Build even when the fork already exists
  -B, --no-build           Do not dispatch the initial Build
  -h, --help               Show this help
EOF
}

die() {
	printf '%s\n' "$*" >&2
	exit 1
}

require_value() {
	(($# >= 2)) || die "Option $1 requires a value"
}

while (($#)); do
	case $1 in
	-o | --owner)
		require_value "$@"
		target_owner=$2
		shift 2
		;;
	-n | --name)
		require_value "$@"
		target_name=$2
		shift 2
		;;
	-s | --source)
		require_value "$@"
		source_repository=$2
		shift 2
		;;
	-b | --build)
		build_mode=always
		shift
		;;
	-B | --no-build)
		build_mode=never
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	--)
		shift
		break
		;;
	*)
		die "Unknown option: $1"
		;;
	esac
done
(($# == 0)) || die "Unexpected positional argument: $1"

command -v gh >/dev/null 2>&1 || die "GitHub CLI (gh) is required"
gh auth status >/dev/null 2>&1 || die "Authenticate GitHub CLI with: gh auth login"

current_user=$(gh api user --jq .login)
[[ -n $target_owner ]] || target_owner=$current_user
source_repository=$(gh api "repos/$source_repository" --jq .full_name)
source_name=${source_repository#*/}
[[ -n $target_name ]] || target_name=$source_name
target_repository=$target_owner/$target_name

created=false
if gh api "repos/$target_repository" >/dev/null 2>&1; then
	root_source=$(gh api "repos/$target_repository" --jq '.source.full_name // ""')
	[[ ${root_source,,} == "${source_repository,,}" ]] ||
		die "$target_repository exists but is not a fork of $source_repository"
	printf 'Using existing fork %s\n' "$target_repository"
else
	fork_arguments=(
		repo fork "$source_repository"
		--fork-name "$target_name"
		--default-branch-only
		--clone=false
	)
	if [[ ${target_owner,,} != "${current_user,,}" ]]; then
		fork_arguments+=(--org "$target_owner")
	fi
	gh "${fork_arguments[@]}"
	created=true
	printf 'Created %s from only the default branch of %s\n' \
		"$target_repository" "$source_repository"
fi

repository_ready=false
for _ in {1..15}; do
	if default_branch=$(gh api "repos/$target_repository" --jq .default_branch 2>/dev/null); then
		repository_ready=true
		break
	fi
	sleep 2
done
[[ $repository_ready == true ]] ||
	die "GitHub did not finish creating $target_repository"

if [[ $created == true ]]; then
	inherited_branch_output=$(
		gh api --paginate "repos/$target_repository/branches?per_page=100" \
			--jq '.[].name'
	)
	mapfile -t inherited_branches <<<"$inherited_branch_output"
	for branch in "${inherited_branches[@]}"; do
		[[ -n $branch ]] || continue
		[[ $branch == "$default_branch" ]] && continue
		gh api --method DELETE \
			"repos/$target_repository/git/refs/heads/$branch"
		printf 'Removed inherited branch %s\n' "$branch"
	done
fi

gh api --method PUT "repos/$target_repository/actions/permissions" \
	-F enabled=true -f allowed_actions=all >/dev/null

workflow_count=$(
	gh api "repos/$target_repository/actions/workflows?per_page=1" \
		--jq .total_count
)
if [[ $workflow_count == 0 ]]; then
	gh api --method PUT "repos/$target_repository/actions/permissions" \
		-F enabled=false >/dev/null
	gh api --method PUT "repos/$target_repository/actions/permissions" \
		-F enabled=true -f allowed_actions=all >/dev/null
fi

for _ in {1..15}; do
	workflow_count=$(
		gh api "repos/$target_repository/actions/workflows?per_page=1" \
			--jq .total_count
	)
	((workflow_count > 0)) && break
	sleep 2
done
((workflow_count > 0)) || die "GitHub did not register the fork's workflows"

disabled_workflow_output=$(
	gh api --paginate \
		"repos/$target_repository/actions/workflows?per_page=100" \
		--jq '.workflows[] | select(.state == "disabled_fork") | [.id, .name] | @tsv'
)
while IFS=$'\t' read -r workflow_id workflow_name; do
	[[ -n $workflow_id ]] || continue
	gh workflow enable "$workflow_id" --repo "$target_repository"
	printf 'Enabled %s\n' "$workflow_name"
done <<<"$disabled_workflow_output"

if [[ $build_mode == always || ($build_mode == auto && $created == true) ]]; then
	gh workflow run publish.yml --repo "$target_repository" --ref "$default_branch"
	printf 'Dispatched the initial Build on %s\n' "$default_branch"
else
	printf 'Build not dispatched; run with --build when needed\n'
fi

printf 'Fork setup complete: https://github.com/%s\n' "$target_repository"
