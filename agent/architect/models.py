"""Structured outputs produced by the Architect models."""

from pydantic import BaseModel, Field


class ResearchStep(BaseModel):
    reasoning: str = Field(
        description="Why this research step is needed for implementation"
    )
    hypothesis: str = Field(description="The hypothesis to investigate")


class ResearchEvaluation(BaseModel):
    reasoning: str = Field(description="Why the research step is valid or invalid")
    is_valid: bool = Field(description="Whether the research step should run")
