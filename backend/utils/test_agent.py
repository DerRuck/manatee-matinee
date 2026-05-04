"""
Local smoke test for the Hello World agent.

Run from the repo root (parent of backend/):
    python -m backend.utils.test_agent

Reads ANTHROPIC_API_KEY from the .env file at repo root.

"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from backend.agents.hello_world import HelloWorldAgent

agent = HelloWorldAgent()

result = agent.run(
    "Write a brief summary for outreach purposes.",
    context={
        "municipality": "Rookery Bay National Estuarine Research Reserve",
        "state": "Florida",
        "contact": "Reserve Manager, Collier County",
        "notes": (
            "Manages 110,000 acres of mangrove estuary near Naples; "
            "key habitat for manatees, sea turtles, and shorebirds; "
            "active water quality monitoring program"
        ),
    },
)

print(result.content)
print()
print(f"model:          {result.model}")
print(f"input_tokens:   {result.input_tokens}")
print(f"output_tokens:  {result.output_tokens}")
print(f"cache_created:  {result.cache_creation_tokens}")
print(f"cache_read:     {result.cache_read_tokens}")
