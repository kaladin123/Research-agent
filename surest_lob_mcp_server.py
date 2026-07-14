"""
surest_lob_mcp_server.py

Same external contract as the first version: ONE MCP tool, consult_surest.
What's new is that surest_lob_agent now has multiple tools of its OWN --
check_claim_status, lookup_adjudication_rule, check_member_eligibility.
These are plain ADK function tools, not MCP tools. Consult Agent never
sees them and never sees how many there are or how they get used; it
only ever sees consult_surest. That's the whole point -- the internal
agent can grow arbitrarily more capable without the outside contract
changing at all.

Run:
    pip install google-adk fastmcp litellm --break-system-packages
    python surest_lob_mcp_server.py
Test:
    fastmcp dev surest_lob_mcp_server.py
"""

from typing import Optional

from fastmcp import FastMCP
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

APP_NAME = "surest-lob-demo"
USER_ID = "mcp-caller"  # derive from auth once you have middleware


# ---------------------------------------------------------------------------
# The agent's OWN tools. Plain functions -- ADK wraps them automatically,
# using the docstring as the description and the type hints as the schema,
# same idea as FastMCP does on the outside. Replace the bodies with your
# real Mongo/API calls. None of these get an @mcp.tool() decorator -- that
# omission is what keeps them invisible outside this process.
# ---------------------------------------------------------------------------

def check_claim_status(claim_id: str) -> dict:
    """Look up the current adjudication status of a claim by its ID."""
    return {"claim_id": claim_id, "status": "approved", "last_updated": "2026-07-10"}


def lookup_adjudication_rule(procedure_code: str) -> dict:
    """Look up the Surest rule that governs how a procedure code is adjudicated."""
    return {"procedure_code": procedure_code, "rule": "stub rule text"}


async def check_member_eligibility(member_id: str) -> dict:
    """Check whether a member currently has active Surest coverage."""
    return {"member_id": member_id, "eligible": True, "plan": "stub plan"}


# The agent. Same LlmAgent as before, now with three tools attached. It
# decides on its own, per query, whether and which of these to call.
agent = LlmAgent(
    name="surest_lob_agent",
    model=LiteLlm(model="azure/gpt-4.1"),  # swap for your UHG gateway model string
    instruction=(
        "Answer questions about Surest member claim adjudication. Use "
        "check_claim_status, lookup_adjudication_rule, and "
        "check_member_eligibility as needed -- don't guess at anything "
        "those tools can answer directly."
    ),
    tools=[check_claim_status, lookup_adjudication_rule, check_member_eligibility],
)

session_service = InMemorySessionService()
runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

mcp = FastMCP("surest-lob-server")


@mcp.tool()
async def consult_surest(query: str, session_id: Optional[str] = None) -> dict:
    """Ask the Surest LOB agent a question. Returns only the final answer --
    the agent's own reasoning never leaves this function."""
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

    return {"answer": answer, "session_id": session_id}


if __name__ == "__main__":
    # Networked, not spawned: the client connects to a URL, it doesn't
    # need to know this file exists or where it lives on disk.
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8000, path="/mcp")
