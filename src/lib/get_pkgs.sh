#!/bin/bash

$1 "$2" "select package_type, package, max(date) as max_date from '$3' where owner_id = '$4' group by package_type, package having max(date) < '$5' order by max_date asc;" | awk -F'|' '{print "////"$1"//"$2}'
