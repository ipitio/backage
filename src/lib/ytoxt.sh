#!/bin/bash

stop_requested() {
	[ -n "${BKG_ENV:-}" ] && [ -f "$BKG_ENV" ] && grep -q '^BKG_TIMEOUT=1$' "$BKG_ENV" 2>/dev/null
}

ytox() {
	local source_file
	local xml_file
	local xml_tmp
	stop_requested && return 3

	source_file="$1"
	xml_file="${source_file%.*}.xml"
	xml_tmp=$(mktemp "$(dirname "$xml_file")/.${xml_file##*/}.XXXXXX") || return 1

	stop_requested && {
		rm -f "$xml_tmp"
		return 3
	}

	printf '%s' '<?xml version="1.0" encoding="UTF-8"?><xml>' >"$xml_tmp" || {
		rm -f "$xml_tmp"
		return 1
	}

	if ! (
		set -o pipefail
		jq -c 'if type == "array" then {package: .} else . end' "$source_file" \
			| yq -ox -I0 - 2>/dev/null \
			| sed 's/"/\\"/g'
	) >>"$xml_tmp"; then
		rm -f "$xml_tmp"
		return 1
	fi

	printf '%s' '</xml>' >>"$xml_tmp" || {
		rm -f "$xml_tmp"
		return 1
	}

	mv -f "$xml_tmp" "$xml_file" || {
		rm -f "$xml_tmp"
		return 1
	}
	stat -c %s "$xml_file" || echo -1
}

# ytox + trim: if the json or xml is over 50MB, remove oldest versions
f="$1"
del_n=1
last_xml_size=-1
xml_current=false

[ -f "$f" ] || exit 1

tmp=$(mktemp "$(dirname "$f")/.${f##*/}.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT

while [ -f "$f" ]; do
	stop_requested && exit 3
	json_size=$(stat -c %s "$f" 2>/dev/null || echo -1)

	if [ "$json_size" -lt 50000000 ]; then
		# Only generate/check XML if JSON is already under limit.
		xml_size=$(ytox "$f" 2>/dev/null)
		xml_status=$?
		((xml_status != 3)) || exit 3
		[ "$xml_status" -eq 0 ] || xml_size=-1
		[ "$xml_status" -eq 0 ] && xml_current=true || xml_current=false
		# If XML size can't be determined, treat it as oversized so we keep trimming.
		[ "$xml_size" -ge 0 ] || xml_size=50000000

		if [ "$xml_size" -lt 50000000 ]; then
			break
		fi

		# If XML is still too large, keep trimming, but avoid redoing work forever.
		if [ "$xml_size" -eq "$last_xml_size" ] && [ "$last_xml_size" -ge 0 ]; then
			break
		fi
		last_xml_size="$xml_size"

		# XML still too large: increase trimming aggressiveness as well.
		if [ "$del_n" -lt 65536 ]; then
			del_n=$((del_n * 2))
		fi
	else
		# JSON is still too large: increase trimming aggressiveness.
		if [ "$json_size" -ge 50000000 ]; then
			if [ "$del_n" -lt 65536 ]; then
				del_n=$((del_n * 2))
			fi
		fi
	fi

	stop_requested && exit 3
	if ! jq -c '
		def id_to_num:
			if type == "number" then .
			elif type == "string" then tonumber? // 0
			else 0 end;
		def version_len:
			if type == "object" then
				(.version? // []) | if type == "array" then length else 0 end
			else
				0
			end;
		def versions_sorted:
			(.version? // []) as $versions
			| if ($versions | type) != "array" or ($versions | length) < 2 then
				true
			else
				reduce range(1; ($versions | length)) as $i (true; . and (($versions[$i - 1].id | id_to_num) <= ($versions[$i].id | id_to_num)))
			end;
		def trim_version_holder($n):
			if version_len > 0 then
				if versions_sorted then
					.version |= .[$n:]
				else
					.version |= (sort_by(.id | id_to_num) | .[$n:])
				end
			else
				.
			end;
		def has_versions:
			if type == "array" then
				any(.[]?; (. | version_len) > 0)
			elif type == "object" and ((.package? // null) | type == "array") then
				any(.package[]?; (. | version_len) > 0)
			elif type == "object" then
				version_len > 0
			else
				false
			end;
		def drop_one($arr):
			(
				$arr
				| to_entries
				| (min_by([ (.value.raw_downloads // 0 | tonumber? // 0), (.value.date // "") ]) // null) as $target
				| if $target == null then
					map(.value)
				else
					[ .[] | select(.key != $target.key) | .value ]
				end
			);
		def trim_largest_versions($n):
			if type == "array" then
				(to_entries
				| (max_by(.value | version_len) // null) as $max
				| if $max == null or (($max.value | version_len) == 0) then
					map(.value)
				else
					map(
						if .key == $max.key then
							(.value |= trim_version_holder($n))
						else
							.
						end
					)
					| map(.value)
				end)
			elif type == "object" and ((.package? // null) | type == "array") then
				.package |= (
					to_entries
					| (max_by(.value | version_len) // null) as $max
					| if $max == null or (($max.value | version_len) == 0) then
						map(.value)
					else
						map(
							if .key == $max.key then
								(.value |= trim_version_holder($n))
							else
								.
							end
						)
						| map(.value)
					end
				)
			elif type == "object" then
				trim_version_holder($n)
			else
				.
			end;
		if has_versions then
			trim_largest_versions($n)
		else
			if type == "array" then
				drop_one(.)
			elif type == "object" and ((.package? // null) | type == "array") then
				.package |= drop_one(.package)
			elif type == "object" then
				(
					to_entries
					| (min_by([ (.value.raw_downloads // 0 | tonumber? // 0), (.value.date // "") ]) // null) as $target
					| if $target == null then
						from_entries
					else
						([ .[] | select(.key != $target.key) ] | from_entries)
					end
				)
			else
				.
			end
		end
	' --argjson n "$del_n" "$f" >"$tmp"; then
		rm -f "$tmp"
		break
	fi

	tmp_size=$(stat -c %s "$tmp" 2>/dev/null || echo -1)

	# If trimming didn't reduce size, retry with more aggressive deletion instead of stalling.
	if [ "$json_size" -ge 0 ] && [ "$tmp_size" -ge 0 ] && [ "$tmp_size" -ge "$json_size" ]; then
		rm -f "$tmp"

		if [ "$del_n" -lt 65536 ]; then
			del_n=$((del_n * 2))
			continue
		fi

		# If we're already at max aggressiveness, fall back to trimming whole packages once.
		stop_requested && exit 3
		if ! jq -c '
			def drop_one($arr):
				(
					$arr
					| to_entries
					| (min_by([ (.value.raw_downloads // 0 | tonumber? // 0), (.value.date // "") ]) // null) as $target
					| if $target == null then
						map(.value)
					else
						[ .[] | select(.key != $target.key) | .value ]
					end
				);
			if type == "array" then
				drop_one(.)
			elif type == "object" and ((.package? // null) | type == "array") then
				.package |= drop_one(.package)
			elif type == "object" then
				(
					to_entries
					| (min_by([ (.value.raw_downloads // 0 | tonumber? // 0), (.value.date // "") ]) // null) as $target
					| if $target == null then
						from_entries
					else
						([ .[] | select(.key != $target.key) ] | from_entries)
					end
				)
			else
				.
			end
		' "$f" >"$tmp"; then
			rm -f "$tmp"
			break
		fi

		tmp_size=$(stat -c %s "$tmp" 2>/dev/null || echo -1)
		if [ "$json_size" -ge 0 ] && [ "$tmp_size" -ge 0 ] && [ "$tmp_size" -ge "$json_size" ]; then
			rm -f "$tmp"
			break
		fi
	fi

	mv "$tmp" "$f"
	xml_current=false
done

# Ensure the XML output corresponds to the final JSON.
final_xml_status=0
if ! $xml_current; then
	ytox "$f" >/dev/null 2>&1 || final_xml_status=$?
	((final_xml_status != 3)) || exit 3
fi

# If either JSON or XML is > 100MB, there is a bug, but empty each one that is too large to allow others:
[ "$(stat -c %s "$f" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "{}" >"$f"
[ "$(stat -c %s "${f%.*}.xml" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml></xml>" >"${f%.*}.xml"
