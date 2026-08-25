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
    create table if not exists "bkg_owner_queue" (
        generation text not null,
        owner_id text not null,
        owner text not null,
        owner_key text not null,
        priority integer not null,
        sequence integer not null,
        reason text not null,
        status text not null check (
            status in ('ready', 'claimed', 'paused', 'completed')
        ),
        attempt_after integer not null default 0,
        claim_token text not null default '',
        claimed_at integer not null default 0,
        outcome text not null default '',
        finished_at integer not null default 0,
        created_at integer not null,
        updated_at integer not null,
        primary key (generation, owner_id),
        unique (generation, owner_key),
        unique (generation, sequence)
    )
    """,
    """
    create table if not exists "bkg_owner_queue_candidates" (
        generation text not null,
        owner text not null,
        owner_key text not null,
        reason text not null,
        attempted_at integer not null,
        primary key (generation, owner_key)
    )
    """,
    """
    create table if not exists "bkg_database_metrics" (
        sample_date text primary key,
        run_count integer not null,
        physical_bytes integer not null,
        logical_bytes integer not null,
        page_size integer not null,
        page_count integer not null,
        freelist_pages integer not null,
        package_rows integer not null,
        version_rows integer not null,
        package_rows_written integer not null,
        version_rows_written integer not null,
        maximum_pre_rotation_bytes integer not null,
        rotation_count integer not null,
        rotation_archive_bytes integer not null,
        snapshot_bytes integer not null,
        object_bytes_json text not null,
        package_rows_by_date_json text not null,
        version_rows_by_date_json text not null
    )
    """,
    """
    create table if not exists "bkg_package_catalog" (
        owner_id text not null default '',
        owner_type text not null default '',
        package_type text not null default '',
        owner text not null,
        repo text not null,
        package text not null,
        observed_at text not null default '',
        primary key (owner, repo, package)
    )
    """,
    """
    create table if not exists "bkg_package_catalog_state" (
        singleton integer primary key check (singleton = 1),
        source_revision text not null,
        initialized_at text not null,
        source_owners integer not null,
        source_repositories integer not null,
        source_packages integer not null
    )
    """,
    """
    create table if not exists "bkg_rotation_events" (
        event_id integer primary key,
        release_tag text not null,
        rotated_at text not null,
        archive_name text not null unique,
        source_bytes integer not null check (source_bytes >= 0),
        compressed_bytes integer not null check (compressed_bytes >= 0),
        retained_since text not null
    )
    """,
    """
    create index if not exists "idx_bkg_owners_date_owner"
    on {owners} (date, owner)
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
    """
    create index if not exists "idx_bkg_owner_queue_ready"
    on "bkg_owner_queue" (
        generation, status, attempt_after, priority, sequence
    )
    """,
    """
    create index if not exists "idx_bkg_package_catalog_identity"
    on "bkg_package_catalog" (owner_id, package_type, repo, package)
    """,
    """
    create index if not exists "idx_bkg_rotation_events_release"
    on "bkg_rotation_events" (release_tag, rotated_at, event_id)
    """,
)

OWNER_SCAN_SCHEMA_MIGRATIONS = (
    (
        "next_page",
        'alter table "bkg_owner_scans" add column next_page integer not null default 1',
    ),
)
