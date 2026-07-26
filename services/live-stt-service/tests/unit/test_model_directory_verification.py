"""Pinned model directory verification must survive Windows scandir semantics.

The anti-swap guard compares the stat recorded while listing the directory
against os.fstat() of the descriptor it then reads. On Windows the directory
enumeration API behind os.scandir() carries neither the volume serial nor the
file index, so entry.stat() reports st_dev=0 and st_ino=0 while fstat() reports
the real values. Measured on the GPU host:

    field      scandir  fstat
    st_dev     0        2659991151
    st_ino     0        2251799815012715

Comparing those made every file look mutated, so the hardened model runtime
could never load a model on Windows — the failure surfaced only as an opaque
``RuntimeError: ValueError`` through the worker supervisor.

Linux scandir carries d_ino, so the platform difference is simulated here
rather than waited for.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.services.streaming_models import (
    _stream_model_directory_sha256,
    _verify_stream_model_directory,
)


def _build_model_dir(root: Path) -> Path:
    model_dir = root / "artifacts" / "live" / ("0" * 40)
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"weights")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    # Windows enumerates README.md before model.bin under a case-insensitive
    # comparison; keep a mixed-case name so ordering stays exercised too.
    (model_dir / "README.md").write_text("card", encoding="utf-8")
    return model_dir


class _WindowsLikeEntry:
    """A scandir entry whose stat() drops st_dev/st_ino, as Windows does."""

    def __init__(self, entry: os.DirEntry[str]) -> None:
        self._entry = entry
        self.name = entry.name
        self.path = entry.path

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        real = self._entry.stat(follow_symlinks=follow_symlinks)
        fields = list(real)
        fields[stat.ST_INO] = 0
        fields[stat.ST_DEV] = 0
        return os.stat_result(fields)


_REAL_SCANDIR = os.scandir


class _WindowsLikeScandir:
    def __init__(self, path: str | Path) -> None:
        self._iterator = _REAL_SCANDIR(path)

    def __enter__(self) -> list[_WindowsLikeEntry]:
        return [_WindowsLikeEntry(entry) for entry in self._iterator]

    def __exit__(self, *exc_info: object) -> None:
        self._iterator.close()


@pytest.fixture()
def windows_like_scandir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "scandir", _WindowsLikeScandir)


def test_directory_digest_survives_scandir_without_dev_and_ino(
    tmp_path: Path, windows_like_scandir: None
) -> None:
    model_dir = _build_model_dir(tmp_path)

    # Must not raise "pinned streaming model file mutated during verification".
    digest = _stream_model_directory_sha256(model_dir)

    assert len(digest) == 64


def test_digest_is_identical_with_and_without_scandir_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform difference must not change the pinned digest itself."""
    model_dir = _build_model_dir(tmp_path)

    native = _stream_model_directory_sha256(model_dir)
    monkeypatch.setattr(os, "scandir", _WindowsLikeScandir)
    windows_like = _stream_model_directory_sha256(model_dir)

    assert native == windows_like


def test_verification_accepts_a_matching_tree_digest(
    tmp_path: Path, windows_like_scandir: None
) -> None:
    model_dir = _build_model_dir(tmp_path)
    tree = _stream_model_directory_sha256(model_dir)

    resolved = _verify_stream_model_directory(model_dir, "", "sha256:" + tree)

    assert resolved == Path(os.path.abspath(model_dir))


def test_a_real_swap_between_listing_and_read_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must still catch a genuine swap, not merely stop complaining.

    Without this, dropping the unequal-identity comparison would look like a
    fix while actually removing the protection.
    """
    model_dir = _build_model_dir(tmp_path)
    real_fstat = os.fstat

    def swapped_fstat(descriptor: int) -> os.stat_result:
        fields = list(real_fstat(descriptor))
        fields[stat.ST_INO] += 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", swapped_fstat)

    with pytest.raises(ValueError, match="mutated during verification"):
        _stream_model_directory_sha256(model_dir)
