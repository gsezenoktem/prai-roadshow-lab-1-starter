import logging
import os
import json
import re
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from httpx_sse import aconnect_sse

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.genai import types as genai_types
from opentelemetry import trace
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace import TracerProvider, export
from pydantic import BaseModel

from authenticated_httpx import create_authenticated_client

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def save_course_content(content: str, user_id: str) -> str:
    """Save generated course content as a markdown file. Returns the file path."""
    # Extract title from first markdown heading, if present
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if title_match:
        slug = re.sub(r"[^\w]+", "_", title_match.group(1).strip().lower()).strip("_")[:60]
    else:
        slug = "course"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{slug}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


class Feedback(BaseModel):
    score: float
    text: str | None = None
    run_id: str | None = None
    user_id: str | None = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

provider = TracerProvider()
processor = export.BatchSpanProcessor(
    CloudTraceSpanExporter(),
)
provider.add_span_processor(processor)
trace.set_tracer_provider(provider)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_name = os.getenv("AGENT_NAME", None)
agent_server_url = os.getenv("AGENT_SERVER_URL")
if not agent_server_url:
    raise ValueError("AGENT_SERVER_URL environment variable not set")
else:
    agent_server_url = agent_server_url.rstrip("/")

clients: Dict[str, httpx.AsyncClient] = {}

async def get_client(agent_server_origin: str) -> httpx.AsyncClient:
    global clients
    if agent_server_origin not in clients:
        clients[agent_server_origin] = create_authenticated_client(agent_server_origin)
    return clients[agent_server_origin]

async def create_session(agent_server_origin: str, agent_name: str, user_id: str) -> Dict[str, Any]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    session_request_url = f"{agent_server_origin}/apps/{agent_name}/users/{user_id}/sessions"
    session_response = await httpx_client.post(
        session_request_url,
        headers=headers
    )
    session_response.raise_for_status()
    return session_response.json()

async def get_session(agent_server_origin: str, agent_name: str, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    session_request_url = f"{agent_server_origin}/apps/{agent_name}/users/{user_id}/sessions/{session_id}"
    session_response = await httpx_client.get(
        session_request_url,
        headers=headers
    )
    if session_response.status_code == 404:
        return None
    session_response.raise_for_status()
    return session_response.json()


async def list_agents(agent_server_origin: str) -> List[str]:
    httpx_client = await get_client(agent_server_origin)
    headers=[
        ("Content-Type", "application/json")
    ]
    list_url = f"{agent_server_origin}/list-apps"
    list_response = await httpx_client.get(
        list_url,
        headers=headers
    )
    list_response.raise_for_status()
    agent_list = list_response.json()
    if not agent_list:
        agent_list = ["agent"]
    return agent_list


async def query_adk_sever(
        agent_server_origin: str, agent_name: str, user_id: str, message: str, session_id
) -> AsyncGenerator[Dict[str, Any], None]:
    httpx_client = await get_client(agent_server_origin)
    request = {
        "appName": agent_name,
        "userId": user_id,
        "sessionId": session_id,
        "newMessage": {
            "role": "user",
            "parts": [{"text": message}]
        },
        "streaming": False
    }
    async with aconnect_sse(
        httpx_client,
        "POST",
        f"{agent_server_origin}/run_sse",
        json=request
    ) as event_source:
        if event_source.response.is_error:
            event = {
                "author": agent_name,
                "content":{
                    "parts": [
                        {
                            "text": f"Error {event_source.response.text}"
                        }
                    ]
                }
            }
            yield event
        else:
            async for server_event in event_source.aiter_sse():
                event = server_event.json()
                yield event

class SimpleChatRequest(BaseModel):
    message: str
    user_id: str = "test_user"
    session_id: Optional[str] = None

@app.post("/api/chat_stream")
async def chat_stream(request: SimpleChatRequest):
    """Streaming chat endpoint."""
    global agent_name, agent_server_url
    if not agent_name:
        agent_name = (await list_agents(agent_server_url))[0] # type: ignore

    session = None
    if request.session_id:
        session = await get_session(
            agent_server_url, # type: ignore
            agent_name,
            request.user_id,
            request.session_id
        )
    if session is None:
        session = await create_session(
            agent_server_url, # type: ignore
            agent_name,
            request.user_id
        )

    events = query_adk_sever(
        agent_server_url, # type: ignore
        agent_name,
        request.user_id,
        request.message,
        session["id"]
    )

    async def event_generator():
        final_text = ""
        async for event in events:
            # Send progress updates based on which agent is active
            if event["author"] == "researcher":
                 yield json.dumps({"type": "progress", "text": "🔍 Researcher is gathering information..."}) + "\n"
            elif event["author"] == "judge":
                 yield json.dumps({"type": "progress", "text": "⚖️ Judge is evaluating findings..."}) + "\n"
            elif event["author"] == "content_builder":
                 yield json.dumps({"type": "progress", "text": "✍️ Content Builder is writing the course..."}) + "\n"
            # Accumulate final text
            if "content" in event and event["content"]:
                content = genai_types.Content.model_validate(event["content"])
                for part in content.parts: # type: ignore
                    if part.text:
                        final_text += part.text
        # Save course content to file
        stripped = final_text.strip()
        saved_filename = None
        if stripped:
            filepath = save_course_content(stripped, request.user_id)
            saved_filename = os.path.basename(filepath)
            logger.info("Course content saved to %s", filepath)
        # Send final result with filename for direct linking
        result = {"type": "result", "text": stripped}
        if saved_filename:
            result["filename"] = saved_filename
        yield json.dumps(result) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/api/courses")
async def list_courses():
    """Return saved courses sorted by most recent first."""
    courses = []
    output_path = Path(OUTPUT_DIR)
    for filepath in sorted(output_path.glob("*.md"), reverse=True):
        # Read first line to extract the title
        with open(filepath, encoding="utf-8") as f:
            first_line = ""
            for line in f:
                line = line.strip()
                if line:
                    first_line = line
                    break
        title = re.sub(r"^#+\s*", "", first_line) if first_line else filepath.stem
        # Parse timestamp from filename (YYYYMMDD_HHMMSS_slug.md)
        parts = filepath.stem.split("_", 2)
        if len(parts) >= 2:
            try:
                created = datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S").isoformat()
            except ValueError:
                created = None
        else:
            created = None
        courses.append({
            "filename": filepath.name,
            "title": title,
            "created_at": created,
        })
    return courses


@app.get("/api/courses/{filename}")
async def get_course(filename: str):
    """Return the markdown content of a saved course."""
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = Path(OUTPUT_DIR) / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Course not found")
    return PlainTextResponse(filepath.read_text(encoding="utf-8"))


# Mount frontend from the copied location
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
