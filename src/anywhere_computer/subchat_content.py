"""Explicit ordinary-Chat resource references; neither paths nor access grants."""
from typing import Self

from pydantic import Field, model_validator

from .models import Contract


class SubchatAttachment(Contract):
    """An already uploaded file in the selected Chat account, not a local path."""

    id: str = Field(pattern=r'^file[_-][A-Za-z0-9_-]+$', max_length=256)
    library_file_id: str | None = Field(default=None, max_length=256,
                                        pattern=r'^libfile_[A-Za-z0-9_-]+$')
    name: str = Field(min_length=1, max_length=256)
    mime_type: str = Field(min_length=1, max_length=128)
    size: int = Field(ge=0, le=20_971_520)


class SubchatPlugin(Contract):
    """Use the URI and hint observed from a successful @ selection, never guess."""

    label: str = Field(min_length=1, max_length=128, pattern=r'^[^\[\]\r\n]+$')
    uri: str = Field(pattern=r'^plugin://[A-Za-z0-9_.@/-]+$', max_length=512)
    system_hint: str = Field(pattern=r'^plugin:[A-Za-z0-9_-]+$', max_length=256)


class SubchatResources(Contract):
    attachments: tuple[SubchatAttachment, ...] = Field(default=(), max_length=10)
    plugins: tuple[SubchatPlugin, ...] = Field(default=(), max_length=8)

    @model_validator(mode='after')
    def unique(self) -> Self:
        if (len({item.id for item in self.attachments}) != len(self.attachments)
                or len({item.uri for item in self.plugins}) != len(self.plugins)
                or len({item.system_hint for item in self.plugins}) != len(self.plugins)):
            raise ValueError('Resource references must be unique')
        return self

    def prompt(self, text: str) -> str:
        return ''.join(f'[@{item.label}]({item.uri}) ' for item in self.plugins) + text

    def hints(self) -> list[str]:
        return [item.system_hint for item in self.plugins]

    def files(self) -> list[dict[str, object]]:
        return [item.model_dump(exclude_none=True) for item in self.attachments]
