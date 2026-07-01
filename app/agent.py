# ruff: noqa
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

import datetime
from zoneinfo import ZoneInfo
from typing import Any, AsyncGenerator

from pydantic import BaseModel, Field, ConfigDict

from google.adk.agents import Agent, BaseAgent, InvocationContext, Context
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import Workflow, node, JoinNode, START
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.events.request_input import RequestInput
from google.genai import types
from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor

import os
import logging
import json
import sys
import re
import google.auth
from google.cloud import firestore
from .tools import (
    clone_repository,
    cleanup_repository,
    list_directory,
    read_file,
    search_code,
)


def redact_sensitive_info(text: str) -> str:
    """Redacts sensitive information like emails and API keys from text."""
    if not isinstance(text, str):
        return text
    # Redact email addresses
    text = re.sub(
        r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED_EMAIL]", text
    )
    # Redact Google API Keys
    text = re.sub(r"AIzaSy[a-zA-Z0-9_-]{33,}", "[REDACTED_API_KEY]", text)
    # Redact GitHub PATs
    text = re.sub(r"ghp_[a-zA-Z0-9]{36}", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"github_pat_[a-zA-Z0-9_]{82}", "[REDACTED_GITHUB_TOKEN]", text)
    return text


def redact_data(data: Any) -> Any:
    """Recursively redacts sensitive info from dicts, lists, and strings."""
    if isinstance(data, str):
        return redact_sensitive_info(data)
    elif isinstance(data, dict):
        return {k: redact_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_data(item) for item in data]
    return data


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_info(record.getMessage()),
        }
        if record.exc_info:
            log_entry["exception"] = redact_sensitive_info(
                self.formatException(record.exc_info)
            )

        if hasattr(record, "extra_fields"):
            log_entry.update(redact_data(record.extra_fields))

        return json.dumps(log_entry)


logger = logging.getLogger("grading_agent")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    # Configure ADK internal logger for debugging
    adk_logger = logging.getLogger("google_adk")
    adk_logger.setLevel(logging.DEBUG)
    adk_logger.addHandler(handler)
    adk_logger.propagate = False

# Instrument Gemini client for tracing
GoogleGenAiSdkInstrumentor().instrument()

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
EVAL_MODE = os.environ.get("GRADER_EVAL_MODE", "false").lower() == "true"


# 1. Define Pydantic Models
class CategoryGrade(BaseModel):
    score: int = Field(
        description="Score for the category. Max 20 for Tools, Memory, Orchestration, Observability; Max 15 for Infrastructure."
    )
    evidence: str = Field(description="Evidence supporting the score")
    recovery_instructions: str = Field(description="Instructions on how to improve")


class FinalReport(BaseModel):
    total_score: int = Field(description="Sum of all category scores (max 95)")
    grades: dict[str, CategoryGrade] = Field(
        description="Grades keyed by category name"
    )
    overall_summary: str = Field(description="Overall summary of the evaluation")


# 2. Define Sub-agents
# Using gemini-2.5-flash for sub-agents as a fast, capable default
sub_model = Gemini(model="gemini-2.5-flash")

tool_evaluator = Agent(
    name="tool_evaluator",
    model=sub_model,
    instruction="""You are an expert evaluator for Tool & Interface Design.
    Your input is the path to the repository. The provided tools (`list_directory`, `read_file`, `search_code`) are configured to automatically operate on this repository.
    You must use these tools to inspect the codebase and evaluate it against the following criteria (max 5 points each, total max 20 points):
    1. Comprehensive Tool Docstrings: Tool functions must include clear, human-readable descriptions of their purpose and all parameters.
    2. Descriptive Naming: Tool names must be highly specific and clear (e.g., 'create_critical_bug' instead of 'update_jira').
    3. Explicit JSON Schemas: The code must utilize strict input and output schemas to validate tool arguments and constrain LLMs (e.g. using Pydantic).
    4. Guided Error Handling: Tool error returns must provide descriptive recovery instructions back to the LLM instead of just crashing.
    
    CRITICAL: You must ONLY use evidence from the files you have actually read in the repository during this turn. Do NOT refer to any files, paths, or code that do not exist in this repository. Any evidence must be verifiable by reading the files.
    Call the tools directly using the function calling interface. Do NOT write Python code (e.g., using 'print' or 'default_api') to call them.
    
    Start by listing the directory to find where tools are defined, then read the files to inspect the tool definitions.
    Provide the score, evidence (quoting file names and line numbers if possible), and recovery instructions for this category.
    You must output a CategoryGrade JSON object.
    """,
    mode="single_turn",
    output_schema=CategoryGrade,
    tools=[list_directory, read_file, search_code],
)

memory_evaluator = Agent(
    name="memory_evaluator",
    model=sub_model,
    instruction="""You are an expert evaluator for Context & Memory.
    Your input is the path to the repository. The provided tools (`list_directory`, `read_file`, `search_code`) are configured to automatically operate on this repository.
    You must use these tools to inspect the codebase and evaluate it against the following criteria (max 5 points each, total max 20 points):
    1. Robust System Instructions: A clear "constitution" must be defined in the system prompt for persona, domain knowledge, and constraints.
    2. History Compaction: Code must implement context bloat management (e.g., token-based truncation, sliding windows, summarization) via mechanisms and tools such as ADK compaction, memory bank, or Google Cloud context caching.
    3. Persistent Session State: The agent must connect to a persistent database (vector store, Vertex AI Search, Firestore, etc.) to efficiently retrieve information or manage conversational history across turns.
    4. Async Memory Operations: Expensive memory generation and consolidation must be coded as background or async tasks to prevent UI blocking.
    
    CRITICAL: You must ONLY use evidence from the files you have actually read in the repository during this turn. Do NOT refer to any files, paths, or code that do not exist in this repository. Any evidence must be verifiable by reading the files.
    Call the tools directly using the function calling interface. Do NOT write Python code (e.g., using 'print' or 'default_api') to call them.
    
    Inspect the agent configuration, prompts, and database integrations.
    Provide the score, evidence (quoting file names and line numbers if possible), and recovery instructions for this category.
    You must output a CategoryGrade JSON object.
    """,
    mode="single_turn",
    output_schema=CategoryGrade,
    tools=[list_directory, read_file, search_code],
)

orchestration_evaluator = Agent(
    name="orchestration_evaluator",
    model=sub_model,
    instruction="""You are an expert evaluator for Orchestration & Logic.
    Your input is the path to the repository. The provided tools (`list_directory`, `read_file`, `search_code`) are configured to automatically operate on this repository.
    You must use these tools to inspect the codebase and evaluate it against the following criteria (max 5 points each, total max 20 points):
    1. Multi-Agent Patterns: Complex tasks must utilize proven design patterns (e.g., Coordinator, Sequential) rather than monolithic agents, implemented in ADK.
    2. Strategic Model Routing: The codebase must route specific requests to the most appropriate model (e.g., Flash for fast tasks, Pro for planning).
    3. Guardrails & Policy Plugins: Security and evaluation guardrails (e.g., self-evaluation, input validation) must be implemented.
    4. Human-in-the-Loop Hooks: High-stakes actions must include explicit code stops requiring human confirmation before execution (e.g. using RequestInput).
    
    CRITICAL: You must ONLY use evidence from the files you have actually read in the repository during this turn. Do NOT refer to any files, paths, or code that do not exist in this repository. Any evidence must be verifiable by reading the files.
    Call the tools directly using the function calling interface. Do NOT write Python code (e.g., using 'print' or 'default_api') to call them.
    
    Inspect the agent definitions, workflows, and coordinator logic.
    Provide the score, evidence (quoting file names and line numbers if possible), and recovery instructions for this category.
    You must output a CategoryGrade JSON object.
    """,
    mode="single_turn",
    output_schema=CategoryGrade,
    tools=[list_directory, read_file, search_code],
)

observability_evaluator = Agent(
    name="observability_evaluator",
    model=sub_model,
    instruction="""You are an expert evaluator for Observability & Tracing.
    Your input is the path to the repository. The provided tools (`list_directory`, `read_file`, `search_code`) are configured to automatically operate on this repository.
    You must use these tools to inspect the codebase and evaluate it against the following criteria (max 5 points each, total max 20 points):
    1. Structured JSON Logging: The codebase must utilize structured logging libraries to capture rich metadata rather than simple prints.
    2. Intent vs. Outcome Capture: Logs must explicitly record both the agent's intended action before execution and the actual outcome after.
    3. Distributed Tracing: Implementation of OpenTelemetry (or equivalent) to link spans and trace a request from query to answer.
    4. PII Redaction: Logging and memory pipelines must include active scrubbing mechanisms to redact sensitive data before storage (e.g., using Google Cloud APIs).
    
    CRITICAL: You must ONLY use evidence from the files you have actually read in the repository during this turn. Do NOT refer to any files, paths, or code that do not exist in this repository. Any evidence must be verifiable by reading the files.
    Call the tools directly using the function calling interface. Do NOT write Python code (e.g., using 'print' or 'default_api') to call them.
    
    Inspect the logging configuration and tracing setup in the code.
    Provide the score, evidence (quoting file names and line numbers if possible), and recovery instructions for this category.
    You must output a CategoryGrade JSON object.
    """,
    mode="single_turn",
    output_schema=CategoryGrade,
    tools=[list_directory, read_file, search_code],
)

infra_evaluator = Agent(
    name="infra_evaluator",
    model=sub_model,
    instruction="""You are an expert evaluator for Infrastructure & CI/CD.
    Your input is the path to the repository. The provided tools (`list_directory`, `read_file`, `search_code`) are configured to automatically operate on this repository.
    You must use these tools to inspect the codebase and evaluate it against the following criteria (max 5 points each, total max 15 points):
    1. Automated Evaluation Suites: The repository must contain a testing harness (e.g., against a golden dataset using agents-cli eval) to statically measure agent regressions.
    2. Infrastructure as Code: The project must include IaC configurations (like Terraform) to programmatically provision necessary resources.
    3. Secure Secret Management: No hardcoded API keys; all tools and clients must leverage a secure injection method like Secret Manager or environment variables.
    
    CRITICAL: You must ONLY use evidence from the files you have actually read in the repository during this turn. Do NOT refer to any files, paths, or code that do not exist in this repository. Any evidence must be verifiable by reading the files.
    Call the tools directly using the function calling interface. Do NOT write Python code (e.g., using 'print' or 'default_api') to call them.
    
    Inspect the tests, deployment configurations, Terraform files, and secret handling.
    Provide the score, evidence (quoting file names and line numbers if possible), and recovery instructions for this category.
    You must output a CategoryGrade JSON object.
    """,
    mode="single_turn",
    output_schema=CategoryGrade,
    tools=[list_directory, read_file, search_code],
)


# 3. Define Nodes
@node(rerun_on_resume=True)
async def prep_node(
    node_input: Any, ctx: Context
) -> AsyncGenerator[Event | RequestInput, None]:
    """Prepares the input and asks for confirmation if it's a GitHub URL."""
    logger.info(
        "prep_node started", extra={"extra_fields": {"node_input": str(node_input)}}
    )
    target_url = ctx.state.get("target_url")

    if not target_url:
        text = ""
        if hasattr(node_input, "parts") and node_input.parts:
            text = node_input.parts[0].text
        elif isinstance(node_input, dict) and "text" in node_input:
            text = node_input["text"]
        else:
            text = str(node_input)

        # Extract GitHub URL or local path
        match = re.search(r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", text)
        if match:
            target_url = match.group(0)
        else:
            # Try to find a local path (starts with / or ./ or ../)
            path_match = re.search(r"(?:/|\./|\.\./)[a-zA-Z0-9_/.-]+", text)
            if path_match:
                target_url = path_match.group(0)
            else:
                target_url = text

        ctx.state["target_url"] = target_url
        logger.info(
            "prep_node: target_url initialized",
            extra={"extra_fields": {"target_url": target_url}},
        )

    is_local = os.path.isdir(target_url)
    if "github.com" not in target_url and not is_local:
        logger.warning(
            "prep_node: invalid URL or path",
            extra={"extra_fields": {"target_url": target_url}},
        )
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Error: Only GitHub repositories or local directories are supported for evaluation."
                    )
                ],
            )
        )
        return

    if not EVAL_MODE:
        confirmation = None
        if ctx.resume_inputs and "confirm_eval" in ctx.resume_inputs:
            confirmation_raw = ctx.resume_inputs.get("confirm_eval", "")
            if isinstance(confirmation_raw, dict):
                confirmation = confirmation_raw.get("result", "")
            else:
                confirmation = str(confirmation_raw)
        elif node_input:
            text = ""
            if hasattr(node_input, "parts") and node_input.parts:
                text = node_input.parts[0].text
            elif isinstance(node_input, dict) and "text" in node_input:
                text = node_input["text"]
            else:
                text = str(node_input)

            if text.strip().lower() in ["yes", "y", "no", "n"]:
                confirmation = text

        if confirmation is None:
            logger.info(
                "prep_node: pausing for user confirmation",
                extra={"extra_fields": {"target_url": target_url}},
            )
            yield RequestInput(
                interrupt_id="confirm_eval",
                message=f"Do you want to proceed with evaluating the repository: {target_url}? (yes/no)",
            )
            return

        confirmation = confirmation.strip().lower()
        logger.info(
            "prep_node: resumed with confirmation",
            extra={"extra_fields": {"confirmation": confirmation}},
        )
        if confirmation not in ["yes", "y"]:
            logger.info(
                "prep_node: evaluation cancelled by user",
                extra={"extra_fields": {"target_url": target_url}},
            )
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Evaluation cancelled by user.")],
                )
            )
            return

    try:
        local_path = clone_repository(target_url, ctx.session.id)
        ctx.state["local_path"] = local_path
    except Exception as e:
        logger.error(
            "prep_node: failed to clone repository",
            extra={"extra_fields": {"error": str(e)}},
        )
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text=f"Error: Failed to clone repository {target_url}. Details: {str(e)}"
                    )
                ],
            )
        )
        return

    logger.info(
        "prep_node completed",
        extra={"extra_fields": {"target_url": target_url, "local_path": local_path}},
    )
    yield Event(output=local_path)


collect_grades = JoinNode(name="collect_grades")


@node
def compile_report(node_input: dict[str, Any]) -> FinalReport:
    """Compiles the final report from individual grades."""
    categories = list(node_input.keys())
    logger.info(
        "compile_report started", extra={"extra_fields": {"categories": categories}}
    )
    grades = {}
    total_score = 0
    for name, grade in node_input.items():
        if isinstance(grade, dict):
            grade_obj = CategoryGrade(**grade)
        else:
            grade_obj = grade
        grades[name] = grade_obj
        total_score += grade_obj.score

    summary = f"Evaluation completed. Total score: {total_score}/95."
    logger.info(
        "compile_report completed", extra={"extra_fields": {"total_score": total_score}}
    )
    return FinalReport(total_score=total_score, grades=grades, overall_summary=summary)


collect_final_data = JoinNode(name="collect_final_data")


@node
async def store_report(node_input: dict[str, Any], ctx: Context) -> FinalReport | None:
    """Stores the evaluation report in Firestore and cleans up the cloned repo."""
    local_path = node_input.get("prep_node")
    final_report = node_input.get("compile_report")
    session_id = ctx.session.id

    # Retrieve the original URL from state
    url = ctx.state.get("target_url")

    logger.info(
        "store_report started",
        extra={
            "extra_fields": {
                "session_id": session_id,
                "url": url,
                "local_path": local_path,
            }
        },
    )

    if not url or not final_report:
        logger.warning(
            "store_report: missing url or final_report in input, skipping storage",
            extra={"extra_fields": {"session_id": session_id}},
        )
        if local_path:
            cleanup_repository(local_path)
        return None

    if isinstance(final_report, dict):
        final_report_obj = FinalReport(**final_report)
    else:
        final_report_obj = final_report

    try:
        db = firestore.AsyncClient(database=FIRESTORE_DATABASE)
        doc_ref = db.collection("evaluations").document(session_id)
        await doc_ref.set(
            {
                "session_id": session_id,
                "url": url,
                "total_score": final_report_obj.total_score,
                "grades": {
                    name: {
                        "score": grade.score,
                        "evidence": grade.evidence,
                        "recovery_instructions": grade.recovery_instructions,
                    }
                    for name, grade in final_report_obj.grades.items()
                },
                "overall_summary": final_report_obj.overall_summary,
                "timestamp": firestore.SERVER_TIMESTAMP,
            }
        )
        logger.info(
            "store_report: successfully stored in Firestore",
            extra={"extra_fields": {"session_id": session_id}},
        )
    except Exception as e:
        logger.error(
            "store_report: failed to store in Firestore",
            exc_info=True,
            extra={"extra_fields": {"session_id": session_id}},
        )
        raise e
    finally:
        if local_path:
            cleanup_repository(local_path)

    return final_report_obj


# 4. Define Workflow
evaluation_workflow = Workflow(
    name="evaluation_workflow",
    description="Evaluates a codebase or agent configuration and returns a structured final report.",
    edges=[
        (START, prep_node),
        (
            prep_node,
            (
                tool_evaluator,
                memory_evaluator,
                orchestration_evaluator,
                observability_evaluator,
                infra_evaluator,
            ),
        ),
        (
            (
                tool_evaluator,
                memory_evaluator,
                orchestration_evaluator,
                observability_evaluator,
                infra_evaluator,
            ),
            collect_grades,
        ),
        (collect_grades, compile_report),
        (prep_node, collect_final_data),
        (compile_report, collect_final_data),
        (collect_final_data, store_report),
    ],
)


# Wrapper to make Workflow compatible with LlmAgent sub_agents
class WorkflowAgent(BaseAgent):
    _workflow: Workflow

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, workflow: Workflow, **kwargs):
        super().__init__(
            name=workflow.name, description=workflow.description or "", **kwargs
        )
        self._workflow = workflow

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        # Extract resume_inputs from user_content if present
        resume_inputs = {}
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if part.function_response and part.function_response.id:
                    resume_inputs[part.function_response.id] = (
                        part.function_response.response
                    )

        workflow_ctx = Context(ctx, node=self._workflow, resume_inputs=resume_inputs)
        async for event in self._workflow.run(
            ctx=workflow_ctx, node_input=ctx.user_content
        ):
            yield event
        paused = bool(workflow_ctx.interrupt_ids)

        # Explicitly transfer back to parent to continue the turn, only if not paused
        if self.parent_agent and not paused:
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                branch=ctx.branch,
                actions=EventActions(transfer_to_agent=self.parent_agent.name),
            )


evaluation_workflow_agent = WorkflowAgent(evaluation_workflow)


# 5. Define Root Agent
assessment_coordinator = Agent(
    name="assessment_coordinator",
    model=Gemini(model="gemini-2.5-pro"),
    instruction="""You are the Assessment Coordinator. 
    Your job is to coordinate the evaluation of a codebase or agent configuration.
    1. When you receive a URL or a local directory path, you must route it to the `evaluation_workflow` sub-agent. If the input is not a URL or a valid local path, ask the user to provide one.
    2. If the `evaluation_workflow` has paused to ask the user for confirmation or input, and the user responds, you must call the `evaluation_workflow` again, passing the user's response to it so it can resume.
    3. Once the workflow completes and returns the FinalReport, you must format the final output as a detailed markdown report for the user.
    4. Self-Evaluation: Before presenting the report to the user, you must verify that:
       - The report contains all 5 categories (Tools, Memory, Orchestration, Observability, Infrastructure).
       - Each category has a score, evidence, and recovery instructions.
       - The total score is the sum of the category scores.
       - If any information is missing or inconsistent, you must explain the issue instead of presenting an incomplete report.
    Important: Include the total score exactly as returned (e.g., X/95), and list the individual category grades with their scores (out of 20 for Tools, Memory, Orchestration, Observability; and out of 15 for Infrastructure). Do not scale or modify the scores.
    Include the evidence and recovery instructions for each category, and the overall summary.
    """,
    sub_agents=[evaluation_workflow_agent],
)

root_agent = assessment_coordinator

app = App(
    root_agent=root_agent,
    name="app",
)
