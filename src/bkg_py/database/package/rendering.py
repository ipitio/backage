"""SQL templates used by the database-backed renderers."""

PACKAGE_SNAPSHOT_SQL = """
with package_latest as (
    select
        owner_id,
        owner_type,
        package_type,
        owner,
        repo,
        package,
        downloads,
        downloads_month,
        downloads_week,
        downloads_day,
        size,
        date
    from {packages}
    where owner_id = ?
      and owner_type = ?
      and package_type = ?
      and owner = ?
      and repo = ?
      and package = ?
    order by date desc
    limit 1
),
owner_latest as (
    select max(date) as latest_date
    from {packages}
    where owner_id = ?
),
repo_latest as (
    select max(date) as latest_date
    from {packages}
    where owner_id = ?
      and repo = ?
),
owner_ranked as (
    select package, rank() over (order by downloads desc) as rank
    from {packages}
    where owner_id = ?
      and date = (select latest_date from owner_latest)
),
repo_ranked as (
    select package, rank() over (order by downloads desc) as rank
    from {packages}
    where owner_id = ?
      and repo = ?
      and date = (select latest_date from repo_latest)
)
select
    p.owner_id,
    p.owner_type,
    p.package_type,
    p.owner,
    p.repo,
    p.package,
    p.downloads,
    p.downloads_month,
    p.downloads_week,
    p.downloads_day,
    p.size,
    p.date,
    coalesce((
        select rank from owner_ranked where package = p.package
    ), -1) as owner_rank,
    coalesce((
        select rank from repo_ranked where package = p.package
    ), -1) as repo_rank
from package_latest p
"""

OWNER_VERSION_LIMIT_SQL = """
with latest_dates as (
    select owner_id, package, max(date) as latest_date
    from {packages}
    where owner_id = ?
    group by owner_id, package
),
latest_packages as (
    select
        p.owner_id,
        p.owner_type,
        p.package_type,
        p.owner,
        p.repo,
        p.package,
        p.date
    from {packages} p
    join latest_dates l
      on p.owner_id = l.owner_id
     and p.package = l.package
     and p.date = l.latest_date
    where (? is null or p.repo = ?)
),
version_candidates as (
    select
        v.owner_id,
        v.owner_type,
        v.package_type,
        v.owner,
        v.repo,
        v.package,
        v.id,
        v.name,
        v.size,
        v.downloads,
        v.downloads_month,
        v.downloads_week,
        v.downloads_day,
        v.date,
        v.tags,
        case
            when v.id != '' and v.id not glob '*[^0-9]*'
            then cast(v.id as integer)
        end as numeric_id,
        replace(replace(replace(replace(
            coalesce(v.tags, ''), ' ', ''
        ), char(9), ''), char(10), ''), char(13), '') as compact_tags,
        row_number() over (
            partition by v.owner_id, v.package_type, v.repo, v.package, v.id
            order by v.date desc
        ) as version_date_rank
    from {versions} v
    join latest_packages p
      on v.owner_id = p.owner_id
     and v.owner_type = p.owner_type
     and v.package_type = p.package_type
     and v.owner = p.owner
     and v.repo = p.repo
     and v.package = p.package
     and v.date >= p.date
),
version_rows as (
    select *
    from version_candidates
    where version_date_rank = 1
),
legacy_fallback_packages as (
    select 1
    from latest_packages p
    where not exists (
        select 1
        from {versions} v
        where v.owner_id = p.owner_id
          and v.owner_type = p.owner_type
          and v.package_type = p.package_type
          and v.owner = p.owner
          and v.repo = p.repo
          and v.package = p.package
          and v.date >= p.date
        limit 1
    )
      and exists (
        select 1
        from sqlite_master sm
        where sm.type = 'table'
          and sm.name = (
            ? || p.owner_type || '_' || p.package_type || '_'
            || p.owner || '_' || p.repo || '_' || p.package
          )
        limit 1
    )
),
package_marks as (
    select
        owner_id,
        owner_type,
        package_type,
        owner,
        repo,
        package,
        max(numeric_id) as newest_numeric_id,
        coalesce(
            max(case
                when numeric_id is not null
                 and tags is not null and tags != ''
                 and (',' || compact_tags || ',') like '%,latest,%'
                then numeric_id end),
            max(case
                when numeric_id is not null
                 and tags is not null and tags != ''
                 and instr(tags, '^') = 0
                 and instr(tags, '~') = 0
                 and instr(tags, '-') = 0
                then numeric_id end),
            max(case
                when numeric_id is not null
                 and tags is not null and tags != ''
                 and instr(tags, '^') = 0
                 and instr(tags, '~') = 0
                then numeric_id end),
            max(case
                when numeric_id is not null
                 and tags is not null and tags != ''
                 and instr(tags, '^') = 0
                then numeric_id end),
            max(case
                when numeric_id is not null
                 and tags is not null and tags != ''
                then numeric_id end)
        ) as latest_numeric_id
    from version_rows
    group by owner_id, owner_type, package_type, owner, repo, package
),
ranked_versions as (
    select
        v.*,
        (
            240
            + length(coalesce(v.id, ''))
            + length(coalesce(v.name, ''))
            + length(coalesce(v.date, ''))
            + length(coalesce(v.tags, ''))
            + length(cast(coalesce(v.size, -1) as text))
            + length(cast(coalesce(v.downloads, -1) as text))
            + length(cast(coalesce(v.downloads_month, -1) as text))
            + length(cast(coalesce(v.downloads_week, -1) as text))
            + length(cast(coalesce(v.downloads_day, -1) as text))
        ) as estimated_version_bytes,
        row_number() over (
            partition by v.owner_id, v.package_type, v.repo, v.package
            order by
                case when v.numeric_id is null then 1 else 0 end desc,
                coalesce(v.numeric_id, 0) desc,
                v.id desc
        ) as tail_rank,
        case
            when v.numeric_id is not null
             and (
                v.numeric_id = m.newest_numeric_id
                or v.numeric_id = m.latest_numeric_id
             )
            then 1
            else 0
        end as mandatory
    from version_rows v
    join package_marks m
      on v.owner_id = m.owner_id
     and v.owner_type = m.owner_type
     and v.package_type = m.package_type
     and v.owner = m.owner
     and v.repo = m.repo
     and v.package = m.package
),
base_estimate as (
    select
        coalesce((select count(*) from latest_packages), 0) * 900 + 2
            as package_bytes,
        coalesce((
            select sum(estimated_version_bytes)
            from ranked_versions
            where mandatory = 1
        ), 0) as mandatory_version_bytes
),
optional_rank_costs as (
    select tail_rank, sum(estimated_version_bytes) as rank_bytes
    from ranked_versions
    where mandatory = 0
    group by tail_rank
),
candidates as (
    select
        tail_rank as version_limit,
        (select package_bytes + mandatory_version_bytes from base_estimate)
        + sum(rank_bytes) over (
            order by tail_rank
            rows between unbounded preceding and current row
        ) as estimated_bytes
    from optional_rank_costs
)
select case
    when (select count(*) from latest_packages) = 0 then 0
    when (select package_bytes + mandatory_version_bytes
          from base_estimate) >= ? then 0
    when (select count(*) from legacy_fallback_packages) > 0 then ?
    else coalesce((
        select max(version_limit)
        from candidates
        where estimated_bytes <= ?
    ), 0)
end
"""

OWNER_VERSION_ROWS_SQL = """
with latest_dates as (
    select owner_id, package, max(date) as latest_date
    from {packages}
    where owner_id = ?
    group by owner_id, package
),
latest_packages as (
    select
        p.owner_id,
        p.owner_type,
        p.package_type,
        p.owner,
        p.repo,
        p.package,
        p.date
    from {packages} p
    join latest_dates l
      on p.owner_id = l.owner_id
     and p.package = l.package
     and p.date = l.latest_date
    where (? is null or p.repo = ?)
)
select
    v.owner_id,
    v.owner_type,
    v.package_type,
    v.owner,
    v.repo,
    v.package,
    v.id,
    v.name,
    v.size,
    v.downloads,
    v.downloads_month,
    v.downloads_week,
    v.downloads_day,
    v.date,
    v.tags
from {versions} v
join latest_packages p
  on v.owner_id = p.owner_id
 and v.owner_type = p.owner_type
 and v.package_type = p.package_type
 and v.owner = p.owner
 and v.repo = p.repo
 and v.package = p.package
 and v.date >= p.date
order by
    p.owner,
    p.repo,
    p.package_type,
    p.package,
    p.owner_type,
    p.owner_id,
    v.id,
    v.date
"""

RANKED_PACKAGES_SQL = """
with latest_dates as (
    select owner_id, package, max(date) as latest_date
    from {packages}
    where owner_id = ?
    group by owner_id, package
),
latest_packages as (
    select
        p.owner_id,
        p.owner_type,
        p.package_type,
        p.owner,
        p.repo,
        p.package,
        p.downloads,
        p.downloads_month,
        p.downloads_week,
        p.downloads_day,
        p.size,
        p.date
    from {packages} p
    join latest_dates l
      on p.owner_id = l.owner_id
     and p.package = l.package
     and p.date = l.latest_date
),
ranked_packages as (
    select
        *,
        rank() over (order by downloads desc) as owner_rank,
        rank() over (partition by repo order by downloads desc) as repo_rank
    from latest_packages
)
select
    owner_id,
    owner_type,
    package_type,
    owner,
    repo,
    package,
    downloads,
    downloads_month,
    downloads_week,
    downloads_day,
    size,
    date,
    owner_rank,
    repo_rank
from ranked_packages p
where (? is null or p.repo = ?)
order by p.owner, p.repo, p.package_type, p.package
"""
