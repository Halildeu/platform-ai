#!/usr/bin/env python3
"""Stage and verify immutable live-STT model directories.

The runtime manifest is intentionally stored next to the CTranslate2 files.
faster-whisper ignores the additional JSON file while the Windows launcher
uses it to verify every model artifact before allocating GPU memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "platform-ai.live-stt.model-integrity.v1"
MANIFEST_NAME = "integrity-manifest.json"
CHUNK_SIZE = 1024 * 1024
DIRECTORY_HASH_DOMAIN = b"platform-live-stt-model-directory-v1\0"


class ModelIntegrityError(RuntimeError):
    """Raised when staged model bytes do not satisfy the immutable policy."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def _directory_sha256(root: Path) -> str:
    digest = hashlib.sha256(DIRECTORY_HASH_DOMAIN)
    artifacts = list(_relative_artifact_files(root))
    manifest = root / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise ModelIntegrityError("model integrity manifest is missing or unsafe")
    artifacts.append((MANIFEST_NAME, manifest))
    for relative, path in sorted(artifacts, key=lambda item: item[0]):
        encoded = relative.encode("utf-8")
        size = path.stat().st_size
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
                digest.update(block)
    return digest.hexdigest()


def _relative_artifact_files(root: Path) -> Iterable[tuple[str, Path]]:
    # Order by the POSIX text the manifest contract validates, NOT by Path.
    #
    # `sorted(root.rglob("*"))` compares PurePath objects, and on Windows that
    # comparison is case-insensitive. A snapshot containing `README.md` and
    # `model.bin` therefore emits them as (model.bin, README.md), while the
    # reader enforces plain ASCII ascending order ("R" 0x52 < "m" 0x6D) and
    # rejects the manifest this very function produced:
    #
    #   model-integrity-error: model integrity file entry is not canonical
    #
    # The bug is invisible on case-sensitive filesystems, so hosted CI never
    # saw it while it blocked every GPU-host model staging on Windows.
    for relative_text, candidate in sorted(
        _walk_artifact_files(root), key=lambda item: item[0]
    ):
        yield relative_text, candidate


def _walk_artifact_files(root: Path) -> Iterable[tuple[str, Path]]:
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if relative.parts and relative.parts[0] == ".cache":
            continue
        if candidate.is_symlink():
            raise ModelIntegrityError(
                f"model artifact must not be a symbolic link: {relative.as_posix()}"
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ModelIntegrityError(
                f"model artifact has an unsupported type: {relative.as_posix()}"
            )
        relative_text = relative.as_posix()
        if relative_text == MANIFEST_NAME:
            continue
        yield relative_text, candidate


def _copy_source(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ModelIntegrityError("model source must be a regular directory")
    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    for relative, candidate in _relative_artifact_files(source):
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, CHUNK_SIZE)
        copied += 1
    if copied == 0:
        raise ModelIntegrityError("model source contains no artifacts")


def _download_source(repository: str, revision: str, download_root: Path) -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ModelIntegrityError(
            "huggingface_hub is unavailable; install the live-STT runtime dependencies"
        ) from exc

    download_root.mkdir(parents=True, exist_ok=False)
    result = snapshot_download(
        repo_id=repository,
        revision=revision,
        local_dir=str(download_root),
    )
    resolved = Path(result).resolve(strict=True)
    if resolved != download_root.resolve(strict=True):
        raise ModelIntegrityError("snapshot_download returned an unexpected directory")
    return resolved


def _build_manifest(
    root: Path,
    *,
    repository: str,
    revision: str,
    model_bin_sha256: str,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative, candidate in _relative_artifact_files(root):
        files.append(
            {
                "path": relative,
                "size": candidate.stat().st_size,
                "sha256": _sha256(candidate),
            }
        )
    if not files:
        raise ModelIntegrityError("staged model contains no artifacts")
    model_entry = next((item for item in files if item["path"] == "model.bin"), None)
    if model_entry is None:
        raise ModelIntegrityError("staged model does not contain model.bin")
    if model_entry["sha256"] != model_bin_sha256:
        raise ModelIntegrityError("staged model.bin SHA-256 does not match policy")
    return {
        "schema": SCHEMA,
        "repository": repository,
        "revision": revision,
        "modelBinSha256": model_bin_sha256,
        "files": files,
    }


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    target = root / MANIFEST_NAME
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with target.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise ModelIntegrityError("model integrity manifest is missing or unsafe")
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ModelIntegrityError("model integrity manifest must not use a BOM")
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelIntegrityError("model integrity manifest is invalid") from exc
    if not isinstance(value, dict):
        raise ModelIntegrityError("model integrity manifest must be an object")
    return value


def verify(
    root: Path,
    *,
    repository: str,
    revision: str,
    model_bin_sha256: str,
) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ModelIntegrityError("runtime model path must be a regular directory")
    manifest = _load_manifest(root)
    allowed_keys = {"schema", "repository", "revision", "modelBinSha256", "files"}
    if set(manifest) != allowed_keys:
        raise ModelIntegrityError("model integrity manifest has unexpected keys")
    if manifest.get("schema") != SCHEMA:
        raise ModelIntegrityError("model integrity manifest schema is invalid")
    if manifest.get("repository") != repository:
        raise ModelIntegrityError("model repository does not match policy")
    if manifest.get("revision") != revision:
        raise ModelIntegrityError("model revision does not match policy")
    if manifest.get("modelBinSha256") != model_bin_sha256:
        raise ModelIntegrityError("model.bin policy hash does not match manifest")

    recorded = manifest.get("files")
    if not isinstance(recorded, list) or not recorded:
        raise ModelIntegrityError("model integrity manifest has no files")
    normalized: list[dict[str, Any]] = []
    previous = ""
    for entry in recorded:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ModelIntegrityError("model integrity file entry is invalid")
        relative = entry.get("path")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or relative <= previous
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ModelIntegrityError("model integrity file entry is not canonical")
        previous = relative
        normalized.append({"path": relative, "size": size, "sha256": digest})

    actual = [
        {"path": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
        for relative, path in _relative_artifact_files(root)
    ]
    if actual != normalized:
        raise ModelIntegrityError("runtime model artifact set or digest changed")
    model_entry = next((item for item in actual if item["path"] == "model.bin"), None)
    if model_entry is None or model_entry["sha256"] != model_bin_sha256:
        raise ModelIntegrityError("runtime model.bin SHA-256 does not match policy")
    return _directory_sha256(root)


def stage(args: argparse.Namespace) -> None:
    destination = Path(args.destination)
    if destination.exists() or destination.is_symlink():
        raise ModelIntegrityError("staging destination must not already exist")
    source: Path
    download_root: Path | None = None
    if args.source_directory:
        source = Path(args.source_directory).resolve(strict=True)
    else:
        download_root = destination.parent / f".{destination.name}.download"
        if download_root.exists() or download_root.is_symlink():
            raise ModelIntegrityError("download staging directory already exists")
        source = _download_source(args.repository, args.revision, download_root)
    try:
        _copy_source(source, destination)
        manifest = _build_manifest(
            destination,
            repository=args.repository,
            revision=args.revision,
            model_bin_sha256=args.model_bin_sha256,
        )
        _write_manifest(destination, manifest)
        verify(
            destination,
            repository=args.repository,
            revision=args.revision,
            model_bin_sha256=args.model_bin_sha256,
        )
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        if download_root is not None:
            shutil.rmtree(download_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stage", "verify"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--model-bin-sha256", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--source-directory")
    parser.add_argument("--digest-output")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if len(args.revision) != 40 or any(
        c not in "0123456789abcdef" for c in args.revision
    ):
        raise ModelIntegrityError("revision must be an exact lowercase 40-hex commit")
    if len(args.model_bin_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in args.model_bin_sha256
    ):
        raise ModelIntegrityError("model.bin SHA-256 must be lowercase hexadecimal")
    if args.mode == "stage":
        stage(args)
    else:
        if args.source_directory:
            raise ModelIntegrityError("verify does not accept --source-directory")
        digest = verify(
            Path(args.destination),
            repository=args.repository,
            revision=args.revision,
            model_bin_sha256=args.model_bin_sha256,
        )
        if args.digest_output:
            output = Path(args.digest_output)
            if output.exists() or output.is_symlink():
                raise ModelIntegrityError("digest output must not already exist")
            with output.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(digest + "\n")
                stream.flush()
                os.fsync(stream.fileno())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelIntegrityError as exc:
        print(f"model-integrity-error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
