"""
consult_agent_client.py

The OTHER half of the loop from surest_lob_mcp_server.py. That file is
the callee -- it never calls a tool, it just answers when called. This
file is the caller: an LlmAgent wired up with an McpToolset, which is
what actually "knows" the tool list on the other side.

Connects by URL, not by file path. The two processes are independent --
this file never spawns surest_lob_mcp_server.py; it just knows an
address. That's the actual production shape: your Research Agent
doesn't know or care where Lyra Worker's code lives, only that
something answers at /altus/mcp. Start the server on its own first.

discover() proves the tools/list handshake happens on its own (already
verified live, over HTTP). ask() is the actual client-side USE of this
code: send a question in, consult_agent decides on its own whether/how
many times to call consult_surest, you only see the final answer.

You do not need to call discover() before ask() in normal use -- the
Runner resolves surest_toolset's tools automatically the first time it
builds a model request. discover() exists only to make that step visible.

Run (two terminals):
    python surest_lob_mcp_server.py      # starts listening on SUREST_LOB_URL
    python consult_agent_client.py       # connects to it by URL
discover() needs no credentials. ask() needs a real Azure/UHG key wired
into LiteLlm, since that's the point where the model actually gets called.
"""

import asyncio
import os
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

APP_NAME = "consult-agent-demo"
USER_ID = "research-agent"  # or derive from your RA's actual caller context

# Swap via env var in real deployments -- e.g. https://forge-lyra-worker
# .internal/altus/mcp -- with no code change on this side at all.
SUREST_LOB_URL = os.environ.get("SUREST_LOB_URL", "http://127.0.0.1:8000/mcp")

# 1. THE CONNECTION. A URL, nothing else. No command, no args, no
#    knowledge of what language the server is written in or where its
#    code lives -- just an address that speaks MCP.
surest_toolset = McpToolset(
    connection_params=StreamableHTTPConnectionParams(
        url=SUREST_LOB_URL,
        timeout=15,
    ),
    # tool_filter=["consult_surest"],  # optional: narrow discovery to named tools
)

# 2. THE AGENT. Unchanged.
consult_agent = LlmAgent(
    name="consult_agent",
    model=LiteLlm(model="azure/gpt-4.1"),
    instruction=(
        "For Surest claim adjudication questions, call consult_surest "
        "with the user's question instead of answering from memory."
    ),
    tools=[surest_toolset],
)

# 3. WHAT'S NEW. A Runner + session service FOR consult_agent itself --
#    same shape as the Runner inside surest_lob_mcp_server.py, just one
#    layer up. This is what lets you actually invoke the agent instead
#    of only inspecting its tools.
session_service = InMemorySessionService()
runner = Runner(agent=consult_agent, app_name=APP_NAME, session_service=session_service)


async def ask(query: str, session_id: Optional[str] = None) -> tuple[str, str]:
    """This IS 'using the client': send a question in, get consult_agent's
    answer out. Internally the model may call consult_surest zero, one,
    or several times before returning -- you only ever see the result."""
    if session_id is None:
        session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        session_id = session.id

    content = types.Content(role="user", parts=[types.Part(text=query)])
    answer = ""
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=content
    ):
        if event.is_final_response() and event.content and event.content.parts:
            answer = event.content.parts[0].text or answer

    return answer, session_id


async def discover():
    """Proof that discovery happens on its own -- not a required step."""
    tools = await surest_toolset.get_tools()
    for t in tools:
        print(f"discovered: {t.name} -- {t.description}")


async def main():
    await discover()
    answer, session_id = await ask("How is a Surest member's claim adjudicated?")
    print("ANSWER:", answer)
    print("SESSION:", session_id)
    await surest_toolset.close()


if __name__ == "__main__":
    asyncio.run(main())
