"""Wheel build, package data, metadata, and README compliance tests."""

import json
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import mcp_server_everesteer

ROOT = Path(__file__).resolve().parent.parent
_FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def test_version_and_registry_manifest_match() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    version = str(project["version"])
    assert version == mcp_server_everesteer.__version__

    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "io.github.tukorama/mcp-server-everesteer"
    assert manifest["version"] == version
    assert manifest["packages"][0]["identifier"] == "mcp-server-everesteer"
    assert manifest["packages"][0]["version"] == version


def test_readme_notice_marker_and_absolute_links() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "mcp-name: io.github.tukorama/mcp-server-everesteer" in readme

    prose = _FENCED_BLOCK.sub("", readme)
    relative_links: list[str] = []
    for match in _MD_LINK.finditer(prose):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative_links.append(target)
    assert relative_links == [], f"PyPI README must not contain relative links: {relative_links}"


def test_wheel_contains_data_licenses_entrypoint_and_lean_deps(tmp_path: Path) -> None:
    uv_bin = shutil.which("uv")
    assert uv_bin is not None, "uv CLI is required for wheel build packaging verification"

    out_dir = tmp_path / "dist"
    subprocess.run(
        [uv_bin, "build", "--wheel", "--no-sources", "--out-dir", str(out_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(out_dir.glob("mcp_server_everesteer-*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert "mcp_server_everesteer/py.typed" in names

        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        assert any(name.endswith(".dist-info/licenses/NOTICE") for name in names)

        ep_file = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entry_points = archive.read(ep_file).decode("utf-8")
        assert "mcp-server-everesteer = mcp_server_everesteer.cli:main" in entry_points

        meta_file = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(meta_file).decode("utf-8")
        assert "Provides-Extra: langgraph" in metadata

        requires_lines = [
            line.lower()
            for line in metadata.splitlines()
            if line.lower().startswith("requires-dist:")
        ]
        for heavy in ("pandas", "numpy", "scipy", "plotnine", "matplotlib"):
            assert not any(heavy in line for line in requires_lines), (
                f"Heavy non-control-plane dependency leaked into METADATA: {heavy}"
            )
