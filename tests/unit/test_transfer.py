"""Unit tests for path safety, symlink refusal, and credential-free presigned downloads."""

from pathlib import Path

import httpx2
import pytest

from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.client.base import EveresteerClient
from mcp_server_everesteer.client.transfer import (
    read_file_bytes,
    safe_output_path,
    write_bytes_no_symlink,
)
from mcp_server_everesteer.config import Settings
from tests.conftest import FakeApi

pytestmark = pytest.mark.anyio


def test_safe_output_path_blocks_traversal_and_strips_server_dirs() -> None:
    assert safe_output_path("out/data.parquet") == Path("out/data.parquet")
    assert safe_output_path("../../etc/passwd", server_supplied=True) == Path("passwd")
    with pytest.raises(ValueError, match=r"\.\."):
        safe_output_path("../escape.parquet")
    with pytest.raises(ValueError, match="invalid server-supplied"):
        safe_output_path("..", server_supplied=True)


def test_write_refuses_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError, match=r"(symlink|symbolic)"):
        write_bytes_no_symlink(link, b"overwrite")
    assert target.read_text(encoding="utf-8") == "keep"


def test_read_file_bytes_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_file_bytes(str(tmp_path / "missing.bin"))
    with pytest.raises(IsADirectoryError):
        read_file_bytes(str(tmp_path))


async def test_presigned_download_strips_api_credentials_on_redirect(
    authed_settings: Settings, api: FakeApi, tmp_path: Path
) -> None:
    cdn_url = "https://cdn.everesteer.test/datasets/train.parquet?sig=abc"
    api.add(ep.FUTURES_DATA, (302, b"", {"Location": cdn_url}))
    api.add_cdn(cdn_url, b"PARQUET_BYTES")

    client = EveresteerClient(authed_settings, transport=api.transport)
    try:
        dest = tmp_path / "train.parquet"
        saved, size = await client.download_presigned(
            ep.FUTURES_DATA, str(dest), path_args={"split": "train"}
        )
    finally:
        await client.aclose()

    assert saved == str(dest)
    assert size == len(b"PARQUET_BYTES")
    assert dest.read_bytes() == b"PARQUET_BYTES"
    assert len(api.cdn_requests) == 1
    assert isinstance(api.cdn_requests[0], httpx2.Request)
