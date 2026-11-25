"""Unit tests for download confinement, atomic staging, body checks, and presigned downloads."""

from pathlib import Path

import httpx2
import pytest

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.client.base import EveresteerClient
from mcp_server_everesteer.client.transfer import (
    StagedDownload,
    body_problem,
    confine_output_path,
    kind_for_name,
    media_problem,
    read_file_bytes,
)
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import Refused
from tests.conftest import FakeApi, parquet_bytes

pytestmark = pytest.mark.anyio


def _dirs(tmp_path: Path, *names: str) -> list[Path]:
    made: list[Path] = []
    for name in names:
        folder = tmp_path / name
        folder.mkdir()
        made.append(folder.resolve())
    return made


def test_confine_output_path_keeps_downloads_inside_the_roots(tmp_path: Path) -> None:
    root, other = _dirs(tmp_path, "root", "other")
    assert confine_output_path("out/data.parquet", [root]) == root / "out" / "data.parquet"
    assert confine_output_path(str(root / "a.parquet"), [root]) == root / "a.parquet"
    assert confine_output_path(str(other / "b.parquet"), [root, other]) == other / "b.parquet"
    assert confine_output_path("../../etc/passwd", [root], server_supplied=True) == root / "passwd"
    with pytest.raises(Refused, match=r"\.\."):
        confine_output_path("../escape.parquet", [root])
    with pytest.raises(Refused, match="invalid server-supplied"):
        confine_output_path("..", [root], server_supplied=True)
    with pytest.raises(Refused, match="outside the download directories"):
        confine_output_path(str(other / "c.parquet"), [root])
    with pytest.raises(Refused, match="directory"):
        confine_output_path(str(root), [root])


def test_confine_output_path_resolves_symlinks_before_checking(tmp_path: Path) -> None:
    root, outside = _dirs(tmp_path, "root", "outside")
    (root / "inside").mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "alias").symlink_to(root / "inside", target_is_directory=True)
    with pytest.raises(Refused, match="outside the download directories"):
        confine_output_path("escape/x.parquet", [root])
    assert confine_output_path("alias/x.parquet", [root]) == root / "inside" / "x.parquet"

    target = root / "real.txt"
    target.write_text("keep", encoding="utf-8")
    (root / "link.txt").symlink_to(target)
    with pytest.raises(Refused, match="symlink"):
        confine_output_path("link.txt", [root])
    assert target.read_text(encoding="utf-8") == "keep"


def test_staged_download_replaces_the_target_only_when_published(tmp_path: Path) -> None:
    target = tmp_path / "data.parquet"
    target.write_bytes(b"old")

    staged = StagedDownload.open(target)
    for chunk in (b"PAR1", b"new-bytes", b"PAR1"):
        staged.write(chunk)
    assert target.read_bytes() == b"old"
    assert (staged.head, staged.tail[-4:]) == (b"PAR1new-bytesPAR", b"PAR1")
    assert staged.publish() == len(b"PAR1new-bytesPAR1")
    assert target.read_bytes() == b"PAR1new-bytesPAR1"

    abandoned = StagedDownload.open(target)
    abandoned.write(b"partial")
    abandoned.discard()
    assert target.read_bytes() == b"PAR1new-bytesPAR1"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data.parquet"]


def test_body_and_media_checks_recognise_the_expected_artifact() -> None:
    body = parquet_bytes(b"rows")
    assert body_problem("parquet", body[:16], body[-16:], len(body)) is None
    assert body_problem("parquet", b"PAR1", b"PAR1", 4) is not None
    assert body_problem("parquet", b'{"detail": "x"}', b"}", 15) is not None
    assert body_problem("parquet", b"<!DOCTYPE html>", b"</html>", 400) is not None
    assert body_problem("zip", b"PK\x03\x04rest", b"rest", 8) is None
    assert body_problem("zip", b"PAR1", b"PAR1", 12) is not None
    assert body_problem("binary", b"\x80\x05pickle", b".", 9) is None
    assert body_problem("binary", b"  <html><body>", b"</html>", 40) is not None
    assert body_problem("file", b"<html>", b"</html>", 13) is None

    assert media_problem("parquet", "application/octet-stream") is None
    assert media_problem("parquet", "") is None
    assert media_problem("parquet", "text/html; charset=utf-8") is not None
    assert media_problem("zip", "application/json") is not None
    assert media_problem("binary", "application/problem+json") is not None
    assert media_problem("file", "application/json") is None

    assert kind_for_name("preds.PARQUET") == "parquet"
    assert kind_for_name("bundle.zip") == "zip"
    assert kind_for_name("metrics.json") == "file"


def test_read_file_bytes_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_file_bytes(str(tmp_path / "missing.bin"))
    with pytest.raises(IsADirectoryError):
        read_file_bytes(str(tmp_path))


async def test_presigned_download_strips_api_credentials_on_redirect(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    authed_settings.download_dir = str(tmp_path)
    cdn_url = "https://cdn.everesteer.test/datasets/train.parquet?sig=abc"
    body = parquet_bytes(b"PARQUET_BYTES")
    api.add(ep.FUTURES_DATA, (302, b"", {"Location": cdn_url}))
    api.add_cdn(cdn_url, body)

    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        dest = tmp_path / "train.parquet"
        saved, size = await client.download_presigned(
            ep.FUTURES_DATA, str(dest), kind="parquet", path_args={"split": "train"}
        )
    finally:
        await client.aclose()

    assert saved == str(dest.resolve())
    assert size == len(body)
    assert dest.read_bytes() == body
    assert len(api.cdn_requests) == 1
    assert isinstance(api.cdn_requests[0], httpx2.Request)
