"""MCP tests for resources and workflow prompts."""

import json

import pytest
from mcp.types import TextContent, TextResourceContents

from mcp_server_everesteer import knowledge
from mcp_server_everesteer.client import endpoints as ep
from mcp_server_everesteer.config import Settings
from tests.conftest import FakeApi
from tests.mcp.helpers import session

pytestmark = pytest.mark.anyio


async def test_resources_and_docs(authed_settings: Settings, api: FakeApi) -> None:
    api.add(ep.CAPABILITIES, {"mcp": "/mcp"})
    api.add(ep.SCORING, {"weights": {"score_multiple_constant": 0.1}})
    api.add(ep.CURRENT_ROUND, {"round_number": 7})
    api.add(ep.MODELS_LIST, {"models": [{"id": "m1"}]})

    async with session(authed_settings, api) as client:
        listed = {str(r.uri) for r in (await client.list_resources()).resources}
        for topic in knowledge.TOPICS:
            assert f"everesteer://docs/{topic}" in listed
            doc = await client.read_resource(f"everesteer://docs/{topic}")
            content = doc.contents[0]
            assert isinstance(content, TextResourceContents)
            assert len(content.text) > 50

        for uri, key in (
            ("everesteer://capabilities", "mcp"),
            ("everesteer://scoring", "weights"),
            ("everesteer://rounds/current", "round_number"),
            ("everesteer://models", "models"),
        ):
            res = await client.read_resource(uri)
            content = res.contents[0]
            assert isinstance(content, TextResourceContents)
            assert key in json.loads(content.text)


async def test_prompts_reflect_active_toolsets(authed_settings: Settings, api: FakeApi) -> None:
    async with session(authed_settings, api) as client:
        names = {p.name for p in (await client.list_prompts()).prompts}
        assert names == {"orient_and_plan", "validate_and_submit", "diagnose_model"}

    authed_settings.allow_staking = True
    async with session(authed_settings, api) as client:
        names_staking = {p.name for p in (await client.list_prompts()).prompts}
        assert "review_staking" in names_staking
        prompt = await client.get_prompt("diagnose_model", {"model_id": "m1"})
        first = prompt.messages[0].content
        assert isinstance(first, TextContent)
        assert "m1" in first.text
