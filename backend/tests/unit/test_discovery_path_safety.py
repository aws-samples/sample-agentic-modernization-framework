"""Path-traversal guards used by discovery ingestion (SAST: path-traversal-open)."""

from __future__ import annotations

import pytest

from backend.src.connectors.adapters.paths import safe_path


def test_safe_path_allows_children(tmp_path):
    assert safe_path(tmp_path, tmp_path / "full_exports" / "a.csv") == (tmp_path / "full_exports" / "a.csv").resolve()


@pytest.mark.parametrize("rel", ["../etc/passwd", "a/../../x.csv"])
def test_safe_path_rejects_dotdot(tmp_path, rel):
    with pytest.raises(ValueError):
        safe_path(tmp_path / "base", tmp_path / "base" / rel)


def test_safe_path_rejects_sibling_prefix(tmp_path):
    # /tmp/x/base vs /tmp/x/base2 — the old str.startswith check let this through.
    (tmp_path / "base").mkdir()
    with pytest.raises(ValueError):
        safe_path(tmp_path / "base", tmp_path / "base2" / "a.csv")


def test_safe_path_rejects_symlink_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (tmp_path / "secret.csv").write_text("x")
    (base / "link.csv").symlink_to(tmp_path / "secret.csv")
    with pytest.raises(ValueError):
        safe_path(base, base / "link.csv")
