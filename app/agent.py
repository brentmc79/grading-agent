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
from google.genai import types

import os
import google.auth

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


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
    instruction="""Evaluate the codebase for Tool & Interface Design.
    Score the following criteria (max 5 points each, total max 20 points):
    1. Comprehensive Tool Docstrings: Tool functions must include clear, human-readable descriptions of their purpose and all parameters.
    2. Descriptive Naming: Tool names must be highly specific and clear (e.g., 'create_critical_bug' instead of 'update_jira').
    3. Explicit JSON Schemas: The code must utilize strict input and output schemas to validate tool arguments and constrain LLMs.
    4. Guided Error Handling: Tool error returns must provide descriptive recovery instructions back to the LLM instead of just crashing.
    Provide the score, evidence, and recovery instructions for this category.""",
    mode="single_turn",
    output_schema=CategoryGrade,
)

memory_evaluator = Agent(
    name="memory_evaluator",
    model=sub_model,
    instruction="""Evaluate the codebase for Context & Memory.
    Score the following criteria (max 5 points each, total max 20 points):
    1. Robust System Instructions: A clear "constitution" must be defined in the system prompt for persona, domain knowledge, and constraints.
    2. History Compaction: Code must implement context bloat management (e.g., token-based truncation, sliding windows, summarization) via mechanisms and tools such as ADK compaction, memory bank, or Google Cloud context caching.
    3. Persistent Session State: The agent must connect to a persistent database (vector store, Vertex AI Search, etc.) to efficiently retrieve information or manage conversational history across turns.
    4. Async Memory Operations: Expensive memory generation and consolidation must be coded as background or async tasks to prevent UI blocking.
    Provide the score, evidence, and recovery instructions for this category.""",
    mode="single_turn",
    output_schema=CategoryGrade,
)

orchestration_evaluator = Agent(
    name="orchestration_evaluator",
    model=sub_model,
    instruction="""Evaluate the codebase for Orchestration & Logic.
    Score the following criteria (max 5 points each, total max 20 points):
    1. Multi-Agent Patterns: Complex tasks must utilize proven design patterns (e.g., Coordinator, Sequential) rather than monolithic agents, implemented in ADK.
    2. Strategic Model Routing: The codebase must route specific requests to the most appropriate model (e.g., Flash for fast tasks, Pro for planning).
    3. Guardrails & Policy Plugins: Security and evaluation guardrails (e.g., self-evaluation) must be implemented via existing Google Cloud, ADK, or other agentic tech.
    4. Human-in-the-Loop Hooks: High-stakes actions must include explicit code stops requiring human confirmation before execution.
    Provide the score, evidence, and recovery instructions for this category.""",
    mode="single_turn",
    output_schema=CategoryGrade,
)

observability_evaluator = Agent(
    name="observability_evaluator",
    model=sub_model,
    instruction="""Evaluate the codebase for Observability & Tracing.
    Score the following criteria (max 5 points each, total max 20 points):
    1. Structured JSON Logging: The codebase must utilize structured logging libraries to capture rich metadata rather than simple prints.
    2. Intent vs. Outcome Capture: Logs must explicitly record both the agent's intended action before execution and the actual outcome after.
    3. Distributed Tracing: Implementation of OpenTelemetry (or equivalent) to link spans and trace a request from query to answer.
    4. PII Redaction: Logging and memory pipelines must include active scrubbing mechanisms to redact sensitive data before storage (e.g., using Google Cloud APIs).
    Provide the score, evidence, and recovery instructions for this category.""",
    mode="single_turn",
    output_schema=CategoryGrade,
)

infra_evaluator = Agent(
    name="infra_evaluator",
    model=sub_model,
    instruction="""Evaluate the codebase for Infrastructure & CI/CD.
    Score the following criteria (max 5 points each, total max 15 points):
    1. Automated Evaluation Suites: The repository must contain a testing harness (e.g., against a golden dataset) to statically measure agent regressions.
    2. Infrastructure as Code: The project must include IaC configurations (like Terraform) to programmatically provision necessary resources, utilizing tools like Agents CLI.
    3. Secure Secret Management: No hardcoded API keys; all tools and clients must leverage a secure injection method like Secret Manager.
    Provide the score, evidence, and recovery instructions for this category.""",
    mode="single_turn",
    output_schema=CategoryGrade,
)


# 3. Define Nodes
@node
def prep_node(node_input: Any) -> str:
    """Prepares the input for the evaluators."""
    if hasattr(node_input, "parts") and node_input.parts:
        return node_input.parts[0].text
    elif isinstance(node_input, dict) and "text" in node_input:
        return node_input["text"]
    return str(node_input)


collect_grades = JoinNode(name="collect_grades")


@node
def compile_report(node_input: dict[str, Any]) -> FinalReport:
    """Compiles the final report from individual grades."""
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
    return FinalReport(
        total_score=total_score, grades=grades, overall_summary=summary
    )


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
    ],
)


# Wrapper to make Workflow compatible with LlmAgent sub_agents
class WorkflowAgent(BaseAgent):
    _workflow: Workflow

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, workflow: Workflow, **kwargs):
        super().__init__(
            name=workflow.name,
            description=workflow.description or "",
            **kwargs
        )
        self._workflow = workflow

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        workflow_ctx = Context(ctx, node=self._workflow)
        async for event in self._workflow.run(ctx=workflow_ctx, node_input=ctx.user_content):
            yield event

        # Explicitly transfer back to parent to continue the turn
        if self.parent_agent:
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
    When you receive a URL or a codebase description, you must route it to the `evaluation_workflow` sub-agent.
    Once the workflow completes and returns the FinalReport, you must format the final output as a detailed markdown report for the user.
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
