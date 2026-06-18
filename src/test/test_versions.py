"""Tests for Python version ingestion helpers."""

from __future__ import annotations

from bkg_py.versions import (
    DownloadMetrics,
    extract_download_metric,
    extract_download_metrics,
    extract_embedded_manifest,
    extract_oci_version_labels,
    manifest_size,
    parse_metric_value,
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


def test_extract_embedded_manifest_preserves_current_html_shape() -> None:
    """Embedded manifest extraction mirrors the shell page scrape."""

    html = """
    <pre><code class="json">{"ignored":true}</code></pre>
    <pre><span>x</span><code>{&quot;layers&quot;:[{&quot;size&quot;:10}]}</code></pre>
    """

    assert extract_embedded_manifest(html) == (
        '{"ignored":true}\n{"layers":[{"size":10}]}'
    )
    assert extract_embedded_manifest("<html></html>") == ""


def test_manifest_size_calculates_layers_and_manifest_average() -> None:
    """Known Docker and OCI manifest layouts keep the current size policy."""

    assert manifest_size('{"layers":[{"size":10},{"size":25},{"size":0}]}').size == 35
    assert (
        manifest_size('{"manifests":[{"size":10},{"size":21},{"size":0}]}').size == 15
    )
    assert (
        manifest_size(
            '{"outer":{"layers":[{"size":4},{"size":6}]},"manifests":[{"size":100}]}'
        ).size
        == 10
    )


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
