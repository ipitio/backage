"""Tests for Python version ingestion helpers."""

from __future__ import annotations

import base64
import json

from bkg_py.version_selection import VersionCandidate
from bkg_py.versions import (
    DownloadMetrics,
    VersionListEntry,
    VersionListingContext,
    extract_download_metric,
    extract_download_metrics,
    extract_embedded_manifest,
    extract_oci_version_labels,
    extract_version_page_data,
    manifest_size,
    parse_metric_value,
    parse_version_listing_html,
    version_cache_records,
    version_candidates,
)


def test_parse_metric_value_matches_shell_metric_units() -> None:
    """Compact GitHub metric text is normalized to integer counts."""

    assert parse_metric_value("1,234") == 1234
    assert parse_metric_value("1.2k") == 1200
    assert parse_metric_value("2M") == 2_000_000
    assert parse_metric_value(" nope ") == -1
    assert parse_metric_value("5Q") == -1


def test_extract_download_metrics_from_version_page_spans() -> None:
    """Version-page metric labels use the current span-adjacent parser."""

    html = """
    <span>Total downloads</span><span class="Counter"> 1.2k </span>
    <span>Last 30 days</span>
    <span>2,345</span>
    <span>Last week</span><span> 50 </span>
    <span>Today</span><span> 6 </span>
    """

    assert extract_download_metrics(html) == DownloadMetrics(
        total=1200,
        month=2345,
        week=50,
        day=6,
    )
    assert extract_download_metric(html, "Missing") == -1


def test_parse_version_listing_html_matches_github_rows() -> None:
    """Version listing rows retain IDs, names, tags, and current decoding rules."""

    html = """
    <li class="Box-row">
      <a href="/orgs/Lazztech/packages/container/libre-closet/123?tag=latest"></a>
      <a href="/orgs/Lazztech/packages/container/libre-closet/123?tag=stable%2F1"></a>
      <a href="/orgs/Lazztech/packages/container/libre-closet/123?tag=latest"></a>
      <a href="/orgs/Lazztech/packages/container/libre-closet/123">sha256:abc</a>
    </li>
    <li class="Box-row">
      <a href="/Lazztech/Libre-Closet/pkgs/container/libre-closet/124"></a>
      <input value="sha256:line%09name%0Dtest" />
    </li>
    <li class="Box-row">
      <a href="/Lazztech/Libre-Closet/pkgs/container/libre-closet/125"></a>
      <span class="color-fg-muted">v1.2.3</span>
    </li>
    <li class="Box-row">
      <a href="/Lazztech/Libre-Closet/pkgs/container/libre-closet/126"></a>
      <span class="color-fg-muted">  not-a-name  </span>
    </li>
    """

    entries = parse_version_listing_html(
        html,
        VersionListingContext(
            owner_type="orgs",
            owner="Lazztech",
            repo="Libre-Closet",
            package_type="container",
            package="libre-closet",
        ),
    )

    assert entries == (
        VersionListEntry(123, "sha256:abc", ("latest", "stable/1")),
        VersionListEntry(124, "sha256:line\tname\rtest"),
        VersionListEntry(125, "v1.2.3"),
        VersionListEntry(126, "126"),
    )
    assert entries[0].json_object() == {
        "id": 123,
        "name": "sha256:abc",
        "tags": ["latest", "stable/1"],
    }
    assert entries[0].candidate() == VersionCandidate(
        "123",
        "sha256:abc",
        ("latest", "stable/1"),
    )


def test_version_cache_records_normalize_ids_sources_and_tags() -> None:
    """Candidate cache rows preserve source JSON and normalize shell tag strings."""

    records = version_cache_records(
        json.dumps(
            [
                {
                    "id": 123,
                    "name": "sha256:abc",
                    "tags": [" latest ", "stable", "latest"],
                    "metadata": {"tags": "nested, stable"},
                },
                {"id": "001", "name": "sha256:def", "tags": "dev"},
                {"id": "bad", "name": "sha256:bad", "tags": ["odd"]},
            ]
        )
    )

    assert [record.version_id for record in records] == ["123", "001", "-1"]
    assert [record.tags for record in records] == [
        "latest,stable,nested",
        "dev",
        "odd",
    ]
    assert json.loads(base64.b64decode(records[0].source).decode()) == {
        "id": 123,
        "name": "sha256:abc",
        "tags": [" latest ", "stable", "latest"],
        "metadata": {"tags": "nested, stable"},
    }
    assert base64.b64decode(records[0].tsv_row().split("\t")[2]).decode() == (
        "latest,stable,nested"
    )

    assert version_candidates(
        json.dumps(
            [
                {
                    "id": 123,
                    "name": "sha256:abc",
                    "tags": [" latest ", "stable", "latest"],
                    "metadata": {"tags": "nested, stable"},
                }
            ]
        )
    ) == (
        VersionCandidate(
            version_id="123",
            name="sha256:abc",
            tags=("latest", "stable", "nested"),
        ),
    )


def test_extract_embedded_manifest_prefers_manifest_section() -> None:
    """The manifest block is selected without unrelated copy snippets."""

    html = """
    <code><pre>
    "features": {
      "ghcr.io/example/pkg@sha256:abc": {}
    }
    </pre></code>
    <h4>Manifest</h4>
    <div>
      <code>
        <pre class="color-fg-muted">{
          &quot;digest&quot;: &quot;sha256:abc&quot;,
          &quot;mediaType&quot;: &quot;application/vnd.oci.image.manifest.v1+json&quot;,
          &quot;layers&quot;: [{&quot;size&quot;: 10}]
        }</pre>
      </code>
    </div>
    """

    assert json.loads(extract_embedded_manifest(html)) == {
        "digest": "sha256:abc",
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "layers": [{"size": 10}],
    }
    assert extract_embedded_manifest("<html></html>") == ""


def test_extract_version_page_data_combines_metrics_and_manifest() -> None:
    """One version-page parse returns all values needed by the shell update."""

    html = """
    <span>Total downloads</span><span>1.5k</span>
    <span>Last 30 days</span><span>234</span>
    <span>Last week</span><span>56</span>
    <span>Today</span><span>7</span>
    <h4>Manifest</h4>
    <code><pre>{
      &quot;mediaType&quot;: &quot;application/vnd.oci.image.manifest.v1+json&quot;,
      &quot;layers&quot;: [{&quot;size&quot;: 10}]
    }</pre></code>
    """

    page_data = extract_version_page_data(html)

    assert page_data.metrics == DownloadMetrics(
        total=1500,
        month=234,
        week=56,
        day=7,
    )
    assert json.loads(page_data.manifest)["layers"] == [{"size": 10}]
    assert page_data.json_object() == {
        "downloads": 1500,
        "downloads_month": 234,
        "downloads_week": 56,
        "downloads_day": 7,
        "manifest": page_data.manifest,
    }


def test_manifest_size_calculates_resolved_layers_and_rejects_indexes() -> None:
    """Only layer descriptors in a resolved platform manifest represent size."""

    assert manifest_size('{"layers":[{"size":10},{"size":25},{"size":0}]}').size == 35
    index = manifest_size('{"manifests":[{"size":10},{"size":21},{"size":0}]}')
    assert index.size == -1
    assert index.fallback_reason == "image index requires platform resolution"
    empty_index = manifest_size(
        '{"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[]}'
    )
    assert empty_index.size == -1
    assert empty_index.fallback_reason == "image index has no manifest descriptors"
    assert empty_index.diagnostic_summary is not None
    assert "manifestEntries=0" in empty_index.diagnostic_summary
    assert (
        manifest_size(
            '{"outer":{"layers":[{"size":4},{"size":6}]},"manifests":[{"size":100}]}'
        ).size
        == 10
    )
    docker_verbose = """
    {
      "Ref": "ghcr.io/example/scratch@sha256:abc",
      "Descriptor": {"size": 313},
      "SchemaV2Manifest": {
        "schemaVersion": 2,
        "config": {"size": 229},
        "layers": []
      }
    }
    """
    empty_layers = manifest_size(docker_verbose)
    assert empty_layers.size == 0
    assert empty_layers.fallback_reason is None

    null_docker_layers = manifest_size(
        '{"mediaType":"application/vnd.docker.distribution.manifest.v2+json",'
        '"schemaVersion":2,"config":{'
        '"mediaType":"application/vnd.docker.container.image.v1+json",'
        '"size":822},"layers":null}'
    )
    assert null_docker_layers.size == 0
    assert null_docker_layers.fallback_reason is None

    artifact_payload = manifest_size(
        '{"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        '"schemaVersion":2,"config":{'
        '"mediaType":"application/x.example.payload+gzip",'
        '"size":29981303},"layers":null}'
    )
    assert artifact_payload.size == -1
    assert artifact_payload.fallback_reason == "unsupported shape"


def test_manifest_size_describes_malformed_and_unsupported_shapes() -> None:
    """Fallbacks produce bounded diagnostics without exposing raw controls."""

    malformed = manifest_size('{"layers":[{"size":10}],"bad":"raw \u0001 control"}')

    assert malformed.size == -1
    assert malformed.fallback_reason == "malformed JSON"
    assert malformed.diagnostic_summary is not None
    assert "sample=" in malformed.diagnostic_summary
    assert "\u0001" not in malformed.diagnostic_summary
    assert "\\u0001" in malformed.diagnostic_summary

    unsupported = manifest_size(
        '{"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        '"schemaVersion":2,"config":{"size":99}}'
    )

    assert unsupported.size == -1
    assert unsupported.fallback_reason == "unsupported shape"
    assert unsupported.diagnostic_summary is not None
    assert "json_type=object" in unsupported.diagnostic_summary
    assert "mediaTypes=application/vnd.oci.image.manifest.v1+json" in (
        unsupported.diagnostic_summary
    )
    assert "layerEntries=0" in unsupported.diagnostic_summary
    assert "manifestEntries=0" in unsupported.diagnostic_summary
    assert "positiveSizeFields=1" in unsupported.diagnostic_summary


def test_extract_oci_version_labels_walks_nested_manifest_data() -> None:
    """OCI version labels can be recovered from nested manifest objects."""

    manifest = """
    {
      "config": {
        "labels": {
          "org.opencontainers.image.version": "1.2.3"
        }
      },
      "nested": [
        {"org.opencontainers.image.version": "latest"},
        {"org.opencontainers.image.version": ""}
      ]
    }
    """

    assert extract_oci_version_labels(manifest) == ("1.2.3", "latest")
    assert not extract_oci_version_labels("{bad json")
