"""SQLite schema statements for normalized package metadata."""

SCHEMA_SQL = (
    """
    create table if not exists {owners} (
        owner_id text not null,
        owner text not null,
        date text not null,
        primary key (owner_id, date)
    )
    """,
    """
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
        primary key (owner_id, package, date)
    )
    """,
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
    "pragma auto_vacuum = full",
)
