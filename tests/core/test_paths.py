"""
tests/core/test_paths.py

Covers core/paths.py's resolve_config_identity(), the canonicalization helper backing Epic 5's
("Cross-Config Ticker Safety & Portfolio Snapshot Accuracy Fixes" plan) ticker ownership
registry: config.yaml vs ./config.yaml vs an absolute path to the same file must all key
identically in the shared registry.

Run with: pytest tests/core/test_paths.py -v
"""
import os

from momentum_trading.core.paths import resolve_config_identity


class TestResolveConfigIdentity:
    def test_relative_and_dot_slash_variants_resolve_identically(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("metadata: {}\n")
        monkeypatch.chdir(tmp_path)
        identity_a = resolve_config_identity("config.yaml")
        identity_b = resolve_config_identity("./config.yaml")
        identity_c = resolve_config_identity(str(tmp_path / "config.yaml"))
        assert identity_a == identity_b == identity_c

    def test_different_files_resolve_differently(self, tmp_path):
        (tmp_path / "a.yaml").write_text("metadata: {}\n")
        (tmp_path / "b.yaml").write_text("metadata: {}\n")
        identity_a = resolve_config_identity(str(tmp_path / "a.yaml"))
        identity_b = resolve_config_identity(str(tmp_path / "b.yaml"))
        assert identity_a != identity_b

    def test_does_not_require_the_file_to_exist(self, tmp_path):
        # Pure path resolution, no I/O beyond filesystem path canonicalization, must not raise
        # for a file that doesn't exist (load_config()'s own existence check is separate).
        identity = resolve_config_identity(str(tmp_path / "nonexistent.yaml"))
        assert str(tmp_path) in identity

    def test_returns_a_string(self, tmp_path):
        identity = resolve_config_identity(str(tmp_path / "config.yaml"))
        assert isinstance(identity, str)
