from pydantic import BaseModel, Field


class IntervalInput(BaseModel):
    process: str = Field(min_length=1, max_length=1)
    mod_total: int
    mod_missed: int
    total_start: int
    total_end: int
    missed_start: int
    missed_end: int
    upper: int


class ShiftInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    intervals: list[IntervalInput] = Field(min_length=1, max_length=200)
    actual_total: int
    actual_missed: int


class ShiftPatch(BaseModel):
    """补录/更正：只允许携带要改的部分，区间整体替换。"""
    name: str | None = Field(default=None, max_length=120)
    intervals: list[IntervalInput] | None = Field(default=None, max_length=200)
    actual_total: int | None = None
    actual_missed: int | None = None


class AdoptInput(BaseModel):
    # 前端必须回传它查看还原依据时看到的版本；后端与当前版本比对
    expected_version: int
