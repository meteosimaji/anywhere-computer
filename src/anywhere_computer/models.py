"""Runtime-validated wire contracts shared by both sides of the local connection."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Contract):
    pass


class OpenWorkspace(Contract):
    path: str = Field(default="", max_length=4096)
    view: Literal["files", "settings", "connection"] = "files"


class CodexThreadPage(Contract):
    limit: int = Field(default=10, ge=1, le=30)
    cursor: str | None = Field(default=None, max_length=2048)


class CodexThreadRead(Contract):
    thread_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    limit: int = Field(default=3, ge=1, le=10)
    cursor: str | None = Field(default=None, max_length=2048)


class CodexSkillsPage(Contract):
    cwd: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=30, ge=1, le=100)
    after: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class CodexSkillRead(Contract):
    skill_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    cwd: str | None = Field(default=None, max_length=4096)


class CodexPluginPage(Contract):
    session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    server: str | None = Field(default=None, min_length=1, max_length=200)
    tool: str | None = Field(default=None, min_length=1, max_length=200)
    query: str | None = Field(default=None, min_length=1, max_length=200)
    summary: bool = False
    cwd: str = Field(min_length=1, max_length=4096)
    limit: int = Field(default=30, ge=1, le=30)
    cursor: str | None = Field(default=None, max_length=2048)


class CodexPluginCall(Contract):
    session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    cwd: str = Field(min_length=1, max_length=4096)
    server: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class OpenPluginSession(Contract):
    cwd: str = Field(min_length=1, max_length=4096)
    idle_timeout: int = Field(default=300, ge=30, le=1800)


class PluginSessionId(Contract):
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class RuntimeSettings(Contract):
    default_shell: str | None = None
    file_read_line_limit: int = Field(default=5000, ge=1, le=5000)
    file_write_line_limit: int = Field(default=10000, ge=1, le=100000)


class UpdateSetting(Contract):
    key: Literal["default_shell", "file_read_line_limit", "file_write_line_limit"]
    value: str | int | None


class ListProcesses(Contract):
    limit: int = Field(default=200, ge=1, le=2000)
    after_pid: int = Field(default=0, ge=0)


class StopProcess(Contract):
    pid: int = Field(ge=1)
    created: float = Field(gt=0, allow_inf_nan=False)
    force: bool = False


class FilePath(Contract):
    path: str = Field(min_length=1)


class ReadFile(FilePath):
    offset: int = 0
    limit: int = Field(default=200, ge=1, le=5000)


class ReadBinary(FilePath):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=262144, ge=1, le=262144)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class WriteBinary(FilePath):
    data_base64: str = Field(max_length=349528)
    mode: Literal["create", "replace", "append"] = "create"
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class TransferId(Contract):
    transfer_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class BeginDownload(TransferId):
    path: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DownloadRange(TransferId):
    offset: int = Field(default=0, ge=0, le=1073741824)
    limit: int = Field(default=262144, ge=1, le=262144)


class BeginUpload(TransferId):
    path: str = Field(min_length=1)
    total_bytes: int = Field(ge=0, le=1073741824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadChunk(TransferId):
    offset: int = Field(ge=0, le=1073741824)
    data_base64: str = Field(min_length=4, max_length=349528)


class ResolveUpload(TransferId):
    action: Literal["confirm_published", "discard_staging"]


class ReadDocument(FilePath):
    section: str | None = None
    cell_range: str | None = Field(default=None, max_length=40)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


class FormulaCell(Contract):
    formula: str = Field(min_length=1, max_length=8192)


SpreadsheetValue = str | int | float | bool | FormulaCell | None


class WriteDocument(FilePath):
    format: Literal["docx", "xlsx"]
    text: str | None = Field(default=None, max_length=1000000)
    rows: list[list[SpreadsheetValue]] | None = Field(default=None, max_length=10000)
    sheets: dict[str, list[list[SpreadsheetValue]]] | None = Field(default=None, max_length=32)
    sheet_name: str = Field(default="Sheet1", min_length=1, max_length=31)
    mode: Literal["create", "replace"] = "create"
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


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


SearchGlob = Annotated[str, Field(min_length=1, max_length=256)]


class StartSearch(FilePath):
    pattern: str = Field(min_length=1, max_length=500)
    mode: Literal["literal", "regex"] = "literal"
    kind: Literal["names", "text", "documents"] = "names"
    ignore_case: bool = True
    include_hidden: bool = False
    max_results: int = Field(default=1000, ge=1, le=10000)
    filename_glob: SearchGlob = "*"
    excluded_directories: list[SearchGlob] = Field(default_factory=list, max_length=32)
    whole_word: bool = False
    context_lines: int = Field(default=0, ge=0, le=10)
    timeout_ms: int = Field(default=30000, ge=1, le=600000)
    max_files: int = Field(default=10000, ge=1, le=100000)
    max_depth: int = Field(default=32, ge=0, le=128)


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
    wait_ms: int = Field(default=0, ge=0, le=30000)
    wait_for_prompt: str | None = Field(default=None, min_length=1, max_length=1000)
    output_limit: int = Field(default=16000, ge=1, le=100000)


class SessionOutput(SessionId):
    cursor: int = 0
    limit: int = Field(default=16000, ge=1, le=100000)
    wait_ms: int = Field(default=0, ge=0, le=30000)


class OperationId(Contract):
    operation_id: str


class History(Contract):
    limit: int = Field(default=30, ge=1, le=200)
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    since: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Request(Contract):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tool: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class Reply(Contract):
    operation_id: str
    state: Literal["completed", "failed", "running", "unknown"]
    data: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None
