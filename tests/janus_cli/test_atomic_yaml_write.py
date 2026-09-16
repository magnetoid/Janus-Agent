"""Tests for utils.atomic_yaml_write / atomic_text_write — crash-safe writes."""

import os
import stat
from unittest.mock import patch

import pytest
import yaml

from utils import atomic_text_write, atomic_yaml_write


class TestAtomicYamlWrite:
    def test_writes_valid_yaml(self, tmp_path):
        target = tmp_path / "data.yaml"
        data = {"key": "value", "nested": {"a": 1}}

        atomic_yaml_write(target, data)

        assert yaml.safe_load(target.read_text(encoding="utf-8")) == data

    def test_cleans_up_temp_file_on_baseexception(self, tmp_path):
        class SimulatedAbort(BaseException):
            pass

        target = tmp_path / "data.yaml"
        original = {"preserved": True}
        target.write_text(yaml.safe_dump(original), encoding="utf-8")

        with patch("utils.yaml.dump", side_effect=SimulatedAbort):
            with pytest.raises(SimulatedAbort):
                atomic_yaml_write(target, {"new": True})

        tmp_files = [f for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert len(tmp_files) == 0
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == original

    def test_appends_extra_content(self, tmp_path):
        target = tmp_path / "data.yaml"

        atomic_yaml_write(target, {"key": "value"}, extra_content="\n# comment\n")

        text = target.read_text(encoding="utf-8")
        assert "key: value" in text
        assert "# comment" in text


class TestAtomicTextWrite:
    """The sibling for text that must survive a round trip unchanged."""

    def test_writes_the_text_verbatim(self, tmp_path):
        target = tmp_path / "config.yaml"
        text = "# a comment\nmodel:\n  default: x   # trailing\n\nagent:\n  max_turns: 42\n"

        atomic_text_write(target, text)

        assert target.read_text(encoding="utf-8") == text
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["model"]["default"] == "x"

    def test_creates_missing_parents(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "config.yaml"

        atomic_text_write(target, "a: 1\n")

        assert target.read_text(encoding="utf-8") == "a: 1\n"

    def test_replaces_an_existing_file_whole(self, tmp_path):
        target = tmp_path / "config.yaml"
        target.write_text("old: true\nsecond: line\n", encoding="utf-8")

        atomic_text_write(target, "new: true\n")

        assert target.read_text(encoding="utf-8") == "new: true\n"

    def test_writes_utf8_not_the_platform_encoding(self, tmp_path):
        target = tmp_path / "config.yaml"
        text = "greeting: ćao — ok\n"

        atomic_text_write(target, text)

        assert target.read_bytes() == text.encode("utf-8")

    def test_cleans_up_temp_file_on_baseexception(self, tmp_path):
        class SimulatedAbort(BaseException):
            pass

        target = tmp_path / "config.yaml"
        target.write_text("preserved: true\n", encoding="utf-8")

        with patch("utils.atomic_replace", side_effect=SimulatedAbort):
            with pytest.raises(SimulatedAbort):
                atomic_text_write(target, "new: true\n")

        assert [f for f in tmp_path.iterdir() if ".tmp" in f.name] == []
        assert target.read_text(encoding="utf-8") == "preserved: true\n"

    def test_preserves_the_files_permissions(self, tmp_path):
        if os.name != "posix":
            pytest.skip("POSIX-only")

        target = tmp_path / "config.yaml"
        target.write_text("old: true\n", encoding="utf-8")
        os.chmod(target, 0o600)

        atomic_text_write(target, "new: true\n")

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

