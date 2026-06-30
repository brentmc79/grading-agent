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
    score: int = Field(description="Score for the category (0-10)")
    evidence: str = Field(description="Evidence supporting the score")
    recovery_instructions: str = Field(description="Instructions on how to improve")


class FinalReport(BaseModel):
    total_score: int = Field(description="Sum of all category scores")
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
    instruction="Evaluate tool docstrings, naming, explicit schemas, and error handling. Output a CategoryGrade.",
    mode="single_turn",
    output_schema=CategoryGrade,
)

memory_evaluator = Agent(
    name="memory_evaluator",
    model=sub_model,
    instruction="Evaluate system instructions, history compaction, persistent state, and async operations. Output a CategoryGrade.",
    mode="single_turn",
    output_schema=CategoryGrade,
)

orchestration_evaluator = Agent(
    name="orchestration_evaluator",
    model=sub_model,
    instruction="Evaluate multi-agent patterns, model routing, guardrails, and human-in-the-loop. Output a CategoryGrade.",
    mode="single_turn",
    output_schema=CategoryGrade,
)

observability_evaluator = Agent(
    name="observability_evaluator",
    model=sub_model,
    instruction="Evaluate structured logging, outcome capture, tracing, and PII redaction. Output a CategoryGrade.",
    mode="single_turn",
    output_schema=CategoryGrade,
)

infra_evaluator = Agent(
    name="infra_evaluator",
    model=sub_model,
    instruction="Evaluate automated evaluation suites, IaC, and secret management. Output a CategoryGrade.",
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

    summary = f"Evaluation completed. Total score: {total_score}."
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
    Include the total score, individual category grades (with score, evidence, and recovery instructions), and the overall summary.
    """,
    sub_agents=[evaluation_workflow_agent],
)

root_agent = assessment_coordinator

app = App(
    root_agent=root_agent,
    name="app",
)
