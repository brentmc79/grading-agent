# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncGenerator, Optional

import aiohttp
from fastapi import FastAPI, Request, Cookie, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import google.auth
from google.auth.transport.requests import Request as AuthRequest
from google.cloud import firestore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("grader_frontend")

app = FastAPI(title="Grader Agent Dashboard")

# Mount static files and templates
# We will create these directories next
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

templates = Jinja2Templates(directory=templates_dir)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Configuration
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
db = firestore.AsyncClient(database=FIRESTORE_DATABASE)

# Detect Backend
METADATA_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../deployment_metadata.json"))
BACKEND_URL = "http://127.0.0.1:8000"  # Default local
IS_REMOTE = False
ENGINE_ID = os.environ.get("REMOTE_AGENT_RUNTIME_ID")
GCP_PROJECT = None
GCP_LOCATION = None

if not ENGINE_ID and os.path.exists(METADATA_PATH):
    try:
        with open(METADATA_PATH) as f:
            meta = json.load(f)
        ENGINE_ID = meta.get("remote_agent_runtime_id")
    except Exception as e:
        logger.warning(f"Failed to parse deployment_metadata.json: {e}")

if ENGINE_ID:
    try:
        # Parse project and location from engine_id
        # projects/PROJECT/locations/LOCATION/reasoningEngines/ID
        match = re.match(r"projects/([^/]+)/locations/([^/]+)", ENGINE_ID)
        if match:
            GCP_PROJECT = match.group(1)
            GCP_LOCATION = match.group(2)
            BACKEND_URL = f"https://{GCP_LOCATION}-aiplatform.googleapis.com/reasoningEngines/v1/{ENGINE_ID}/api"
            IS_REMOTE = True
            logger.info(f"Detected remote backend: {BACKEND_URL}")
    except Exception as e:
        logger.error(f"Failed to configure remote backend: {e}")

if not IS_REMOTE:
    logger.info(f"Using local backend: {BACKEND_URL}")


class SubmitRequest(BaseModel):
    url: str


class ResumeRequest(BaseModel):
    session_id: str
    interrupt_id: str
    response: str


# Helper to get auth headers if remote
async def get_backend_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if IS_REMOTE:
        try:
            credentials, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            # Refresh credentials to get token
            auth_req = AuthRequest()
            await asyncio.to_thread(credentials.refresh, auth_req)
            headers["Authorization"] = f"Bearer {credentials.token}"
        except Exception as e:
            logger.error(f"Failed to get Google credentials: {e}")
            raise HTTPException(status_code=500, detail="Failed to authenticate with backend")
    return headers


@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request, grader_session_id: Optional[str] = Cookie(None)):
    """Serves the main dashboard HTML page."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"session_id": grader_session_id, "is_remote": IS_REMOTE},
    )


@app.get("/api/submissions")
async def get_submissions():
    """Fetches all submissions from Firestore and groups them by URL."""
    try:
        evaluations_ref = db.collection("evaluations")
        # Order by timestamp descending to get latest first
        query = evaluations_ref.order_by("timestamp", direction=firestore.Query.DESCENDING)
        docs = await query.get()

        projects = {}
        for doc in docs:
            data = doc.to_dict()
            url = data.get("url")
            if not url:
                continue

            # Convert timestamp to string
            timestamp = data.get("timestamp")
            if timestamp:
                data["timestamp"] = timestamp.isoformat()

            if url not in projects:
                projects[url] = {
                    "url": url,
                    "latest_score": data.get("total_score"),
                    "latest_timestamp": data.get("timestamp"),
                    "history": [],
                }
            projects[url]["history"].append(data)

        return list(projects.values())
    except Exception as e:
        logger.error(f"Failed to fetch submissions from Firestore: {e}")
        return JSONResponse(
            status_code=500, content={"detail": f"Failed to fetch submissions: {str(e)}"}
        )


@app.post("/api/submit")
async def submit_evaluation(payload: SubmitRequest, response: Response):
    """Initiates a new evaluation session with the backend."""
    url = payload.url
    user_id = "dashboard_user"

    headers = await get_backend_headers()
    
    # Step 1: Create a session on the backend
    session_url = f"{BACKEND_URL}/apps/app/users/{user_id}/sessions"
    logger.info(f"Creating session at {session_url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(session_url, headers=headers, json={}) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Failed to create session: {resp.status} - {error_text}")
                    raise HTTPException(status_code=resp.status, detail=f"Backend session creation failed: {error_text}")
                
                session_data = await resp.json()
                session_id = session_data["id"]
                
        # Set the session ID in a cookie
        response.set_cookie(key="grader_session_id", value=session_id, max_age=3600 * 24)
        return {"session_id": session_id, "url": url}
    except Exception as e:
        logger.error(f"Error during submission: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

def check_for_hitl(event_data: dict) -> tuple[str | None, str | None]:
    """Detects if the event is a HITL checkpoint (local or remote/A2A)."""
    # 1. Raw ADK format
    if "interrupt_id" in event_data:
        return event_data["interrupt_id"], event_data.get("message")
        
    # 2. A2A format (function call to adk_request_input)
    content = event_data.get("content") or {}
    parts = content.get("parts") or []
    for part in parts:
        # Support both snake_case and camelCase
        func_call = part.get("function_call") or part.get("functionCall")
        if func_call:
            name = func_call.get("name")
            args = func_call.get("args") or {}
            # Detect if it is the HITL tool
            is_hitl_tool = name == "adk_request_input" or "interruptId" in args or "interrupt_id" in args
            
            if is_hitl_tool:
                interrupt_id = args.get("interruptId") or args.get("interrupt_id") or func_call.get("id")
                message = args.get("message")
                return interrupt_id, message
    return None, None


@app.get("/api/stream/{session_id}")
async def stream_evaluation(
    session_id: str, 
    url: str, 
    resume: bool = False, 
    response: str = None, 
    interrupt_id: str = None
):
    """Streams the evaluation progress from the backend using SSE."""
    
    async def event_generator() -> AsyncGenerator[str, None]:
        headers = await get_backend_headers()
        run_sse_url = f"{BACKEND_URL}/run_sse"
        
        if resume and interrupt_id and response:
            new_message = {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "name": "adk_request_input",
                            "id": interrupt_id,
                            "response": {"result": response},
                        }
                    }
                ],
            }
            logger.info(f"Streaming resume for session {session_id} with response {response}")
        else:
            new_message = {"role": "user", "parts": [{"text": f"Evaluate {url}"}]}
            logger.info(f"Connecting to backend stream at {run_sse_url} for session {session_id}")

        data = {
            "app_name": "app",
            "user_id": "dashboard_user",
            "session_id": session_id,
            "new_message": new_message,
            "streaming": True,
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(run_sse_url, headers=headers, json=data) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        yield f"event: error\ndata: {json.dumps({'message': f'Backend stream failed: {error_text}'})}\n\n"
                        return
                    
                    async for line in response.content:
                        if line:
                            line_str = line.decode("utf-8").strip()
                            if line_str.startswith("data: "):
                                event_data_raw = line_str[6:]
                                logger.info(f"Raw event from backend: {event_data_raw[:200]}...")
                                try:
                                    event_data = json.loads(event_data_raw)
                                    
                                    # Detect HITL (local or remote/A2A)
                                    detected_interrupt_id, hitl_msg = check_for_hitl(event_data)
                                    if detected_interrupt_id:
                                        normalized_checkpoint = {
                                            "interrupt_id": detected_interrupt_id,
                                            "message": hitl_msg or "Confirmation required to proceed."
                                        }
                                        yield f"event: checkpoint\ndata: {json.dumps(normalized_checkpoint)}\n\n"
                                    # Check if it is the final report from the evaluation workflow
                                    elif event_data.get("author") == "evaluation_workflow" and "output" in event_data and isinstance(event_data["output"], dict) and "total_score" in event_data["output"]:
                                        yield f"event: complete\ndata: {event_data_raw}\n\n"
                                    else:
                                        yield f"data: {event_data_raw}\n\n"
                                except json.JSONDecodeError:
                                    # If not JSON, just forward as raw data
                                    yield f"data: {event_data_raw}\n\n"
        except Exception as e:
            logger.error(f"Error in event generator: {e}")
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")



@app.get("/api/submissions/{session_id}/report")
async def get_report_markdown(session_id: str):
    """Fetches a submission and returns its formatted markdown report."""
    try:
        doc_ref = db.collection("evaluations").document(session_id)
        doc = await doc_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Submission not found")
        
        data = doc.to_dict()
        
        # Format as Markdown
        report = []
        report.append(f"# Agent Evaluation Report")
        report.append(f"**Repository**: {data.get('url')}")
        
        # Format timestamp
        timestamp = data.get("timestamp")
        if timestamp:
            report.append(f"**Date**: {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        
        report.append(f"**Total Score**: {data.get('total_score')}/95")
        report.append(f"\n## Overall Summary\n{data.get('overall_summary')}")
        report.append(f"\n---\n")
        
        categories = {
            "tool_evaluator": "1. Tool & Interface Design",
            "memory_evaluator": "2. Context & Memory",
            "orchestration_evaluator": "3. Orchestration & Logic",
            "observability_evaluator": "4. Observability & Tracing",
            "infra_evaluator": "5. Infrastructure & CI/CD"
        }
        
        grades = data.get("grades", {})
        for key, title in categories.items():
            grade_data = grades.get(key)
            if grade_data:
                max_score = 15 if key == "infra_evaluator" else 20
                report.append(f"### {title}")
                report.append(f"**Score**: {grade_data.get('score')}/{max_score}")
                report.append(f"\n**Evidence**:\n{grade_data.get('evidence')}")
                report.append(f"\n**Recovery Instructions**:\n{grade_data.get('recovery_instructions')}")
                report.append(f"\n---\n")
                
        markdown_text = "\n".join(report)
        return {"markdown": markdown_text}
    except Exception as e:
        logger.error(f"Failed to generate report for session {session_id}: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

