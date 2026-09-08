"""Runtime-validated wire contracts shared by both sides of the local connection."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Contract):
    pass


class FilePath(Contract):
    path: str = Field(min_length=1)


class ReadFile(FilePath):
    offset: int = 0
    limit: int = Field(default=200, ge=1, le=5000)


class ReadDocument(FilePath):
    section: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class ReadFiles(Contract):
    paths: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=200, ge=1, le=1000)


class WriteFile(FilePath):
    text: str = Field(max_length=4_000_000)
    mode: Literal["create", "replace", "append"] = "create"
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RestoreFile(FilePath):
    backup_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EditFile(FilePath):
    old_text: str = Field(min_length=1)
    new_text: str
    expected_matches: int = Field(default=1, ge=1, le=10000)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ListDirectory(FilePath):
    depth: int = Field(default=1, ge=1, le=8)
    limit: int = Field(default=300, ge=1, le=2000)
    include_hidden: bool = False


class MoveFile(Contract):
    source: str
    destination: str


class StartSearch(FilePath):
    pattern: str = Field(min_length=1, max_length=500)
    kind: Literal["names", "text"] = "names"
    ignore_case: bool = True
    include_hidden: bool = False
    max_results: int = Field(default=1000, ge=1, le=10000)


class SearchId(Contract):
    search_id: str


class SearchPage(SearchId):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class StartSession(Contract):
    command: str = Field(min_length=1, max_length=100000)
    cwd: str
    shell: str | None = None


class SessionId(Contract):
    session_id: str


class SessionInput(SessionId):
    text: str = Field(max_length=100000)


class SessionOutput(SessionId):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=100000)


class OperationId(Contract):
    operation_id: str


class History(Contract):
    limit: int = Field(default=30, ge=1, le=200)


class Request(Contract):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tool: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class Reply(Contract):
    operation_id: str
    state: Literal["completed", "failed", "running", "unknown"]
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None
