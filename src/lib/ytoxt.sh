#!/bin/bash

stop_requested() {
	[ -n "${BKG_ENV:-}" ] && [ -f "$BKG_ENV" ] && grep -q '^BKG_TIMEOUT=1$' "$BKG_ENV" 2>/dev/null
}

ytox() {
	local source_file
	local normalized_file
	local normalized_tmp=""
	local xml_body
	stop_requested && return 3

	source_file="$1"
	normalized_file="$source_file"

	if jq -e 'type == "array"' "$source_file" >/dev/null 2>&1; then
		normalized_tmp=$(mktemp "${TMPDIR:-/tmp}/ytoxt.XXXXXX") || return 1
		jq -c '{"package": .}' "$source_file" >"$normalized_tmp" || {
			rm -f "$normalized_tmp"
			return 1
		}
		normalized_file="$normalized_tmp"
	fi

	stop_requested && {
		[ -n "$normalized_tmp" ] && rm -f "$normalized_tmp"
		return 3
	}

	xml_body=$(yq -ox -I0 "$normalized_file" 2>/dev/null) || {
		[ -n "$normalized_tmp" ] && rm -f "$normalized_tmp"
		return 1
	}

	[ -n "$normalized_tmp" ] && rm -f "$normalized_tmp"
	printf '<?xml version="1.0" encoding="UTF-8"?><xml>%s</xml>' "$(printf '%s' "$xml_body" | sed 's/"/\\"/g')" >"${source_file%.*}.xml" 2>/dev/null
	stat -c %s "${source_file%.*}.xml" || echo -1
}

# ytox + trim: if the json or xml is over 50MB, remove oldest versions
f="$1"
del_n=1
last_xml_size=-1

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
		stop_requested && exit 3
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
done

# Ensure the XML output corresponds to the final JSON.
final_xml_status=0
ytox "$f" >/dev/null 2>&1 || final_xml_status=$?
((final_xml_status != 3)) || exit 3

# If either JSON or XML is > 100MB, there is a bug, but empty each one that is too large to allow others:
[ "$(stat -c %s "$f" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "{}" >"$f"
[ "$(stat -c %s "${f%.*}.xml" 2>/dev/null || echo -1)" -lt 100000000 ] || echo "<?xml version=\"1.0\" encoding=\"UTF-8\"?><xml></xml>" >"${f%.*}.xml"
