from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NonBlankStr = Annotated[str, Field(min_length=1, pattern=r"\S")]


class HealthResponse(BaseModel):
    """Response model for the health check endpoint."""

    status: str
    service: str
    version: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user", "assistant"]
    content: NonBlankStr


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: NonBlankStr
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class Usage(BaseModel):
    """Token usage; null values mean the provider did not report that count."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    provider: str
    content: str
    usage: Usage


class ErrorDetail(BaseModel):
    type: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
