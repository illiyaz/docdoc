"""Tests for app/tasks/discovery.py.

Covers:
- FilesystemConnector.list_documents() finds files in a directory tree
- Only matching extensions are returned
- Files with unknown extensions are skipped when extensions filter is set
- sha256 and size_bytes are correct
- fetch_document() returns correct bytes
- DiscoveryTask.run() deduplicates by sha256
- DiscoveryTask.run() sorts by source_path
- DiscoveryTask.run() continues when one connector fails
- PostgresConnector raises NotImplementedError
- DocumentInfo keys are present
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.tasks.discovery import (
    DataSourceConnector,
    DiscoveryTask,
    DocumentInfo,
    FilesystemConnector,
    PostgresConnector,
)


# ---------------------------------------------------------------------------
# FilesystemConnector
# ---------------------------------------------------------------------------

class TestFilesystemConnector:

    def test_discovers_file_in_root(self, tmp_path: Path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"PDF content")
        conn = FilesystemConnector(tmp_path)
        docs = conn.list_documents()
        paths = [d["source_path"] for d in docs]
        assert str(f.resolve()) in paths

    def test_discovers_files_recursively(self, tmp_path: Path):
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "report.csv").write_bytes(b"a,b")
        conn = FilesystemConnector(tmp_path)
        docs = conn.list_documents()
        assert len(docs) >= 1

    def test_skips_unknown_extensions_when_filter_set(self, tmp_path: Path):
        (tmp_path / "file.xyz").write_bytes(b"data")
        (tmp_path / "file.pdf").write_bytes(b"pdf")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"pdf"}))
        docs = conn.list_documents()
        assert all(d["file_type"] == "pdf" for d in docs)

    def test_extension_filter_defaults_to_known_extensions(self, tmp_path: Path):
        (tmp_path / "doc.pdf").write_bytes(b"pdf")
        (tmp_path / "unknown.xyz").write_bytes(b"xyz")
        conn = FilesystemConnector(tmp_path)
        docs = conn.list_documents()
        file_types = {d["file_type"] for d in docs}
        assert "pdf" in file_types
        assert "xyz" not in file_types

    def test_sha256_is_correct(self, tmp_path: Path):
        content = b"hello world"
        f = tmp_path / "test.csv"
        f.write_bytes(content)
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"csv"}))
        docs = conn.list_documents()
        assert len(docs) == 1
        expected = hashlib.sha256(content).hexdigest()
        assert docs[0]["sha256"] == expected

    def test_size_bytes_is_correct(self, tmp_path: Path):
        content = b"12345"
        f = tmp_path / "test.csv"
        f.write_bytes(content)
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"csv"}))
        docs = conn.list_documents()
        assert docs[0]["size_bytes"] == 5

    def test_file_name_is_basename(self, tmp_path: Path):
        f = tmp_path / "my_report.xlsx"
        f.write_bytes(b"data")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"xlsx"}))
        docs = conn.list_documents()
        assert docs[0]["file_name"] == "my_report.xlsx"

    def test_file_type_lowercase_no_dot(self, tmp_path: Path):
        f = tmp_path / "doc.PDF"
        f.write_bytes(b"data")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"pdf"}))
        docs = conn.list_documents()
        assert docs[0]["file_type"] == "pdf"

    def test_source_path_is_absolute(self, tmp_path: Path):
        f = tmp_path / "file.csv"
        f.write_bytes(b"x")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"csv"}))
        docs = conn.list_documents()
        assert Path(docs[0]["source_path"]).is_absolute()

    def test_document_info_has_all_required_keys(self, tmp_path: Path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"PDF")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"pdf"}))
        docs = conn.list_documents()
        required = {"source_path", "file_name", "file_type", "size_bytes", "sha256"}
        assert required.issubset(docs[0].keys())

    def test_empty_directory_returns_empty_list(self, tmp_path: Path):
        conn = FilesystemConnector(tmp_path)
        assert conn.list_documents() == []

    def test_skips_directories(self, tmp_path: Path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        conn = FilesystemConnector(tmp_path)
        docs = conn.list_documents()
        for d in docs:
            assert not Path(d["source_path"]).is_dir()

    def test_os_error_is_skipped_with_warning(self, tmp_path: Path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"PDF")
        conn = FilesystemConnector(tmp_path, extensions=frozenset({"pdf"}))
        import app.tasks.discovery as disc_module
        with (
            patch.object(FilesystemConnector, "_describe", side_effect=OSError("permission denied")),
            patch.object(disc_module.logger, "warning") as mock_warn,
        ):
            docs = conn.list_documents()
        # File was skipped — empty result, warning was logged
        assert docs == []
        mock_warn.assert_called_once()

    # -----------------------------------------------------------------------
    # fetch_document
    # -----------------------------------------------------------------------

    def test_fetch_document_returns_file_bytes(self, tmp_path: Path):
        content = b"raw file content"
        f = tmp_path / "file.csv"
        f.write_bytes(content)
        conn = FilesystemConnector(tmp_path)
        assert conn.fetch_document(str(f)) == content

    def test_fetch_document_missing_file_raises_os_error(self, tmp_path: Path):
        conn = FilesystemConnector(tmp_path)
        with pytest.raises(OSError):
            conn.fetch_document(str(tmp_path / "missing.pdf"))


# ---------------------------------------------------------------------------
# PostgresConnector
# ---------------------------------------------------------------------------

class TestPostgresConnector:

    def test_list_documents_raises_not_implemented(self):
        conn = PostgresConnector(table="docs", content_column="body")
        with pytest.raises(NotImplementedError):
            conn.list_documents()

    def test_fetch_document_raises_not_implemented(self):
        conn = PostgresConnector(table="docs", content_column="body")
        with pytest.raises(NotImplementedError):
            conn.fetch_document("some-id")


# ---------------------------------------------------------------------------
# DiscoveryTask
# ---------------------------------------------------------------------------

class TestDiscoveryTask:

    def _make_connector(self, docs: list[DocumentInfo]) -> DataSourceConnector:
        conn = MagicMock(spec=DataSourceConnector)
        conn.list_documents.return_value = docs
        return conn

    def _doc(self, path: str = "/a/b.pdf", sha: str | None = None) -> DocumentInfo:
        content = path.encode()
        return DocumentInfo(
            source_path=path,
            file_name=Path(path).name,
            file_type=Path(path).suffix.lstrip("."),
            size_bytes=len(content),
            sha256=sha or hashlib.sha256(content).hexdigest(),
        )

    def test_returns_all_docs_from_single_connector(self):
        docs = [self._doc("/a/1.pdf"), self._doc("/b/2.csv")]
        task = DiscoveryTask()
        result = task.run([self._make_connector(docs)])
        assert len(result) == 2

    def test_deduplicates_by_sha256(self):
        d1 = self._doc("/a/file.pdf", sha="aaa")
        d2 = self._doc("/b/copy.pdf", sha="aaa")  # same content, different path
        task = DiscoveryTask()
        result = task.run([self._make_connector([d1, d2])])
        assert len(result) == 1

    def test_deduplication_across_connectors(self):
        d1 = self._doc("/a/file.pdf", sha="bbb")
        d2 = self._doc("/b/file.pdf", sha="bbb")  # same content via second connector
        task = DiscoveryTask()
        result = task.run([
            self._make_connector([d1]),
            self._make_connector([d2]),
        ])
        assert len(result) == 1

    def test_different_sha256_both_included(self):
        d1 = self._doc("/a/x.pdf", sha="ccc")
        d2 = self._doc("/b/y.pdf", sha="ddd")
        task = DiscoveryTask()
        result = task.run([self._make_connector([d1, d2])])
        assert len(result) == 2

    def test_sorted_by_source_path(self):
        docs = [self._doc("/z/z.pdf"), self._doc("/a/a.pdf"), self._doc("/m/m.csv")]
        task = DiscoveryTask()
        result = task.run([self._make_connector(docs)])
        paths = [d["source_path"] for d in result]
        assert paths == sorted(paths)

    def test_empty_connectors_returns_empty(self):
        task = DiscoveryTask()
        result = task.run([])
        assert result == []

    def test_connector_raising_continues_to_next(self):
        bad = MagicMock(spec=DataSourceConnector)
        bad.list_documents.side_effect = RuntimeError("scan failed")
        good_doc = self._doc("/ok/file.csv")
        good = self._make_connector([good_doc])
        task = DiscoveryTask()
        result = task.run([bad, good])
        assert len(result) == 1
        assert result[0]["source_path"] == "/ok/file.csv"

    def test_empty_connector_returns_empty(self):
        task = DiscoveryTask()
        result = task.run([self._make_connector([])])
        assert result == []
