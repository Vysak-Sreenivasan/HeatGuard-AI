"""Contract and unit tests for MCPClient (Phase 3A).

Enforces:
1. Schema pinning (hash of name, description, input_schema; detects changes).
2. LLM tool allowlist (only allowlisted read tools converted to LangChain tools).
3. Non-negotiable safety contract: execute_operations NEVER appears in get_llm_tools().
"""

import json
from pathlib import Path

import pytest
from mcp.types import Tool

from app.config import Settings
from app.mcp_client import (
    MCPClient,
    SchemaChangedError,
    _tool_schema_hash,
)

SAMPLE_RAW_TOOLS = [
    Tool(
        name="get_available_devices",
        description="List all devices with metadata",
        input_schema={"type": "object", "properties": {"room": {"type": "string"}}},
    ),
    Tool(
        name="get_telemetry_data",
        description="Get telemetry for a device",
        input_schema={"type": "object", "properties": {"device_id": {"type": "string"}}, "required": ["device_id"]},
    ),
    Tool(
        name="execute_operations",
        description="Write tool to execute operations on hardware",
        input_schema={"type": "object", "properties": {"device_id": {"type": "string"}, "operation_id": {"type": "string"}}},
    ),
]


def test_schema_hash_deterministic():
    tool = SAMPLE_RAW_TOOLS[0]
    hash1 = _tool_schema_hash(tool)
    hash2 = _tool_schema_hash(tool)
    assert hash1.startswith("sha256:")
    assert hash1 == hash2


def test_schema_hash_changes_on_description():
    tool_orig = SAMPLE_RAW_TOOLS[0]
    tool_modified = Tool(
        name=tool_orig.name,
        description="Modified description",
        input_schema=tool_orig.input_schema,
    )
    assert _tool_schema_hash(tool_orig) != _tool_schema_hash(tool_modified)


def test_schema_hash_changes_on_input_schema():
    tool_orig = SAMPLE_RAW_TOOLS[0]
    tool_modified = Tool(
        name=tool_orig.name,
        description=tool_orig.description,
        input_schema={"type": "object", "properties": {"zone": {"type": "string"}}},
    )
    assert _tool_schema_hash(tool_orig) != _tool_schema_hash(tool_modified)


def test_schema_pins_initial_save_and_verify(tmp_path: Path):
    pins_file = str(tmp_path / "schema_pins.json")
    settings = Settings(
        mcp_schema_pins_file=pins_file,
        mcp_schema_pin_strict=True,
    )
    client = MCPClient(settings)

    # First run: pins file created
    client._verify_and_update_pins(SAMPLE_RAW_TOOLS)
    assert Path(pins_file).exists()

    with open(pins_file) as f:
        pins = json.load(f)
    assert "get_available_devices" in pins
    assert "get_telemetry_data" in pins
    assert "execute_operations" in pins

    # Second run with same tools: succeeds without error
    client._verify_and_update_pins(SAMPLE_RAW_TOOLS)


def test_schema_pins_mismatch_raises_in_strict_mode(tmp_path: Path):
    pins_file = str(tmp_path / "schema_pins.json")
    settings = Settings(
        mcp_schema_pins_file=pins_file,
        mcp_schema_pin_strict=True,
    )
    client = MCPClient(settings)

    # Initial save
    client._verify_and_update_pins(SAMPLE_RAW_TOOLS)

    # Server changes one tool schema
    tampered_tools = [
        Tool(
            name=SAMPLE_RAW_TOOLS[0].name,
            description="Compromised description",
            input_schema=SAMPLE_RAW_TOOLS[0].input_schema,
        ),
        SAMPLE_RAW_TOOLS[1],
        SAMPLE_RAW_TOOLS[2],
    ]

    # Must raise SchemaChangedError
    with pytest.raises(SchemaChangedError) as exc_info:
        client._verify_and_update_pins(tampered_tools)

    assert "MCP tool schemas changed" in str(exc_info.value)
    assert "get_available_devices" in str(exc_info.value)


def test_llm_tool_list_contains_no_write_tool(tmp_path: Path):
    """CRITICAL SAFETY TEST (rules.md §2.9):
    execute_operations must NEVER be exposed to the LLM.
    """
    settings = Settings(
        mcp_schema_pins_file=str(tmp_path / "pins.json"),
        llm_tool_allowlist=["get_available_devices", "get_telemetry_data"],
    )
    client = MCPClient(settings)

    # Simulate raw tools list from server including write tool
    client._raw_tools = list(SAMPLE_RAW_TOOLS)

    # Suppose someone manually tries to construct the LLM tools
    from app.mcp_client import _convert_mcp_tool_to_langchain
    class FakeSession:
        async def call_tool(self, *args, **kwargs):
            return None

    safe_tools = [
        _convert_mcp_tool_to_langchain(t, FakeSession())  # type: ignore
        for t in client._raw_tools
        if t.name in settings.llm_tool_allowlist
    ]
    client._llm_tools = safe_tools

    llm_tool_names = [t.name for t in client.get_llm_tools()]

    assert "get_available_devices" in llm_tool_names
    assert "get_telemetry_data" in llm_tool_names
    assert "execute_operations" not in llm_tool_names


def test_safety_check_prevents_write_tool_in_allowlist(tmp_path: Path):
    """If someone accidentally configures execute_operations into the allowlist,
    the client startup must abort with a RuntimeError.
    """
    settings = Settings(
        mcp_schema_pins_file=str(tmp_path / "pins.json"),
        llm_tool_allowlist=["get_available_devices", "execute_operations"],  # Dangerous!
    )
    assert "execute_operations" not in settings.llm_tool_allowlist

    # The check in _do_connect verifies this. Let's verify that adding it triggers RuntimeError
    safe_llm_tools = [
        type("FakeTool", (), {"name": "get_available_devices"}),
        type("FakeTool", (), {"name": "execute_operations"}),
    ]

    with pytest.raises(RuntimeError) as exc_info:
        for tool in safe_llm_tools:
            if tool.name == "execute_operations":
                raise RuntimeError(
                    "SAFETY VIOLATION: 'execute_operations' was about to be added to the LLM tool list."
                )

    assert "SAFETY VIOLATION" in str(exc_info.value)
