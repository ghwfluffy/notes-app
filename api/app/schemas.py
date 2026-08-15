from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ListCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    color: str = Field(default="#6750a4", pattern=r"^#[0-9a-fA-F]{6}$")


class ListPatch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    position: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_update(self) -> ListPatch:
        if not self.model_fields_set:
            raise ValueError("At least one list field is required.")
        for field in ("name", "color", "position"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class ItemCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    details: str | None = Field(default=None, max_length=10_000)
    completed: bool = False


class ItemPatch(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=500)
    details: str | None = Field(default=None, max_length=10_000)
    completed: bool | None = None
    position: int | None = Field(default=None, ge=0)
    list_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def has_update(self) -> ItemPatch:
        if not self.model_fields_set:
            raise ValueError("At least one item field is required.")
        for field in ("title", "completed", "position", "list_id"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self
