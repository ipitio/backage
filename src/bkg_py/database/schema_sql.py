"""SQLite schema statements for normalized package metadata."""

PACKAGE_PRIMARY_KEY = (
    "owner_id",
    "package_type",
    "repo",
    "package",
    "date",
)

PACKAGES_TABLE_SQL = """
    create table if not exists {packages} (
        owner_id text,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        size integer not null,
        date text not null,
        primary key (owner_id, package_type, repo, package, date)
    )
"""

SCHEMA_SQL = (
    """
    create table if not exists {owners} (
        owner_id text not null,
        owner text not null,
        date text not null,
        primary key (owner_id, date)
    )
    """,
    PACKAGES_TABLE_SQL,
    """
    create table if not exists {versions} (
        owner_id text not null,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        id text not null,
        name text not null,
        size integer not null,
        downloads integer not null,
        downloads_month integer not null,
        downloads_week integer not null,
        downloads_day integer not null,
        date text not null,
        tags text,
        primary key (owner_id, package_type, repo, package, id, date)
    )
    """,
    """
    create table if not exists "bkg_owner_scans" (
        owner_id text primary key,
        owner text not null,
        marker text not null,
        status text not null,
        started_at integer not null,
        updated_at integer not null,
        next_page integer not null default 1,
        completed_at integer,
        failure_count integer not null default 0,
        retry_after integer not null default 0,
        last_error text not null default ''
    )
    """,
    """
    create table if not exists "bkg_owner_scan_packages" (
        owner_id text not null,
        marker text not null,
        owner_type text not null,
        package_type text not null,
        repo text not null,
        package text not null,
        primary key (
            owner_id, marker, owner_type, package_type, repo, package
        )
    )
    """,
    """
    create table if not exists "bkg_package_publications" (
        owner_id text not null,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        updated_at text not null,
        primary key (
            owner_id, owner_type, package_type, owner, repo, package
        )
    )
    """,
    """
    create table if not exists "bkg_package_batch_progress" (
        owner_id text not null,
        owner_type text not null,
        package_type text not null,
        owner text not null,
        repo text not null,
        package text not null,
        batch_marker text not null,
        completed_at text not null,
        primary key (
            owner_id, owner_type, package_type, owner, repo, package
        )
    )
    """,
    """
    create index if not exists "idx_bkg_owners_date_owner"
    on {owners} (date, owner)
    """,
    """
    create index if not exists "idx_bkg_packages_owner_repo_package_date"
    on {packages} (owner_id, owner, repo, package, date)
    """,
    """
    create index if not exists "idx_bkg_packages_owner_name_date"
    on {packages} (owner_id, owner, date)
    """,
    """
    create index if not exists "idx_bkg_packages_owner_date_downloads"
    on {packages} (owner_id, date, downloads desc, package)
    """,
    """
    create index if not exists "idx_bkg_packages_owner_repo_date_downloads"
    on {packages} (owner_id, repo, date, downloads desc, package)
    """,
    """
    create index if not exists "idx_bkg_versions_package_date"
    on {versions} (owner_id, package_type, repo, package, date)
    """,
    """
    create index if not exists "idx_bkg_versions_date"
    on {versions} (date)
    """,
    """
    create index if not exists "idx_bkg_owner_scans_retry"
    on "bkg_owner_scans" (status, retry_after, owner)
    """,
    """
    create index if not exists "idx_bkg_package_publications_owner"
    on "bkg_package_publications" (owner_id, owner, repo, package)
    """,
    """
    create index if not exists "idx_bkg_package_batch_progress_marker"
    on "bkg_package_batch_progress" (batch_marker, owner_id, owner)
    """,
    "pragma auto_vacuum = full",
)

OWNER_SCAN_SCHEMA_MIGRATIONS = (
    (
        "next_page",
        'alter table "bkg_owner_scans" add column next_page integer not null default 1',
    ),
)
