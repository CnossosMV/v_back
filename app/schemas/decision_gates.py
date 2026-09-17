from pydantic import BaseModel, Field


class DecisionChoiceCreate(BaseModel):
    option_key: str = Field(..., min_length=1, max_length=80)
    reason: str | None = Field(None, max_length=2000)
