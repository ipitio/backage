#!/bin/bash

ytox() {
    echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml>$(yq -ox -I0 "$1" | sed 's/"/\\"/g')</xml>" >"${1%.*}.xml" 2>/dev/null
    stat -c %s "${1%.*}.xml" || echo -1
}

# ytox + trim: if the json or xml is over 50MB, remove oldest versions
f="$1"
del_n=1
last_xml_size=-1

[ -f "$f" ] || exit 1

tmp=$(mktemp "${f}.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT

while [ -f "$f" ]; do
	json_size=$(stat -c %s "$f" 2>/dev/null || echo -1)

	if [ "$json_size" -lt 50000000 ]; then
		# Only generate/check XML if JSON is already under limit.
		xml_size=$(ytox "$f" 2>/dev/null || echo -1)
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

	if jq -e '
		def has_versions:
			if type == "array" then
				any(.[]?; ((.version? // []) | type == "array") and ((.version? // []) | length > 0))
			elif type == "object" and ((.package? // null) | type == "array") then
				any(.package[]?; ((.version? // []) | type == "array") and ((.version? // []) | length > 0))
			elif type == "object" then
				((.version? // []) | type == "array") and ((.version? // []) | length > 0)
			else
				false
			end;
		has_versions
	' "$f" >/dev/null; then
		if ! jq -c '
			def id_to_num:
				if type == "number" then .
				elif type == "string" then tonumber? // 0
				else 0 end;
			def vlen:
				(.version? // []) | if type == "array" then length else 0 end;
			def trim_versions($n):
				if ((.version? // []) | type == "array") and ((.version? // []) | length > 0) then
					(
						.version
						| sort_by(.id | id_to_num)
						| .[$n:]
					) as $v
					| .version = $v
				else
					.
				end;
			if type == "array" then
				(to_entries
				| (max_by(.value | vlen) // empty) as $max
				| map(
					if .key == $max.key and ((.value | vlen) > 0)
					then (.value |= trim_versions($n))
					else .
					end
				)
				| map(.value))
			elif type == "object" and ((.package? // null) | type == "array") then
				.package |= (
					to_entries
					| (max_by(.value | vlen) // empty) as $max
					| map(
						if .key == $max.key and ((.value | vlen) > 0)
						then (.value |= trim_versions($n))
						else .
						end
					)
					| map(.value)
				)
			elif type == "object" then
				(to_entries
				| (max_by(.value | vlen) // empty) as $max
				| map(
					if .key == $max.key and ((.value | vlen) > 0)
					then (.value |= trim_versions($n))
					else .
					end
				)
				| from_entries)
			else
				trim_versions($n)
			end
		' --argjson n "$del_n" "$f" >"$tmp"; then
			rm -f "$tmp"
			break
		fi
	else
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
done

# Ensure the XML output corresponds to the final JSON.
ytox "$f" >/dev/null 2>&1

# If either JSON or XML is > 100MB, there is a bug, but empty each one that is too large to allow others:
[ "$(stat -c %s "$f" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "{}" >"$f"
[ "$(stat -c %s "${f%.*}.xml" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml></xml>" >"${f%.*}.xml"
