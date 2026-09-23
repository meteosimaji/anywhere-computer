"""Explicit in-memory handoff of an already observed read session, not a login API."""
from __future__ import annotations

import json
import re
from typing import BinaryIO
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator

from .models import Contract


class ObservedHTTPSession(Contract):
    """Never persist this object or put its credentials in argv, reports or MCP calls.

    Possession is not provider approval for automation. Only an operator-authorized,
    already established session may be handed off; this module acquires no credentials.
    Chrome-login mode retains cookies in the HTTPX client jar and may attach its
    User-Agent here. Refresh credentials and request-preparation/protection
    tokens are not stored here.
    """

    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, hide_input_in_errors=True)

    authorization: SecretStr = Field(min_length=8, max_length=16_384, repr=False)
    account_id: str = Field(min_length=1, max_length=256, repr=False)
    catalog_url: str = Field(min_length=1, max_length=4096, repr=False)
    language: str | None = Field(default=None, min_length=1, max_length=64)
    cookie: SecretStr | None = Field(default=None, min_length=1, max_length=32_768,
                                      repr=False)
    user_agent: str | None = Field(default=None, min_length=1, max_length=1024,
                                   repr=False)
    user_email: str | None = Field(default=None, min_length=3, max_length=320,
                                   repr=False)

    @field_validator('authorization')
    @classmethod
    def bearer(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r'Bearer [A-Za-z0-9._~+/-]+=*', value.get_secret_value()) is None:
            raise ValueError('Invalid authorization envelope')
        return value

    @field_validator('account_id')
    @classmethod
    def account(cls, value: str) -> str:
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError('Invalid account envelope')
        return value

    @field_validator('language')
    @classmethod
    def locale(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r'[A-Za-z0-9-]+', value) is None:
            raise ValueError('Invalid language envelope')
        return value

    @field_validator('user_email')
    @classmethod
    def email_envelope(cls, value: str | None) -> str | None:
        if value is not None and ('@' not in value or any(ord(char) < 33 for char in value)):
            raise ValueError('Invalid login email envelope')
        return value

    @field_validator('catalog_url')
    @classmethod
    def catalog(cls, value: str) -> str:
        url = urlsplit(value)
        if (any(ord(char) < 33 or ord(char) > 126 for char in value) or '#' in value
                or url.scheme != 'https' or url.netloc != 'chatgpt.com'
                or url.path != '/backend-api/models'):
            raise ValueError('Only the exact observed HTTPS model-catalog URL is supported')
        return value

    def headers(self) -> dict[str, str]:
        result = {'authorization': self.authorization.get_secret_value(),
                  'chatgpt-account-id': self.account_id}
        if self.language is not None:
            result['oai-language'] = self.language
        if self.cookie is not None:
            result['cookie'] = self.cookie.get_secret_value()
        if self.user_agent is not None:
            result['user-agent'] = self.user_agent
            result['referer'] = 'https://chatgpt.com/'
        return result


def read_http_session(source: BinaryIO) -> ObservedHTTPSession:
    """Consume exactly one bounded UTF-8 JSON line before the controller protocol.

    Use a trusted parent's anonymous stdin pipe. Do not log the first line. All
    validation errors are replaced, since schema errors can include secret inputs.
    Waiting for the supplying process to provide its line is normal stdin blocking;
    network deadlines apply only after this explicit startup handoff completes.
    """
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate session field')
            result[key] = value
        return result

    try:
        raw = source.readline(32_769)
        if not raw.endswith(b'\n') or len(raw) > 32_768:
            raise ValueError('Session envelope is missing or oversized')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
        if not isinstance(data, dict) or set(data) - {
                'authorization', 'account_id', 'catalog_url', 'language'}:
            raise ValueError('Unsupported explicit session field')
        return ObservedHTTPSession.model_validate(data)
    except (ValueError, TypeError, OSError, RecursionError):
        raise ValueError('Invalid HTTP session envelope; no request was made') from None
