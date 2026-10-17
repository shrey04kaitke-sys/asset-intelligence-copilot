"""Time-window resolution. All windows are inclusive date ranges ending (by default) at 'today'."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class TimeWindow(BaseModel):
    """Exactly one style: explicit dates, a trailing period, or a calendar year."""
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = Field(None, description="Inclusive start (YYYY-MM-DD)")
    end_date: date | None = Field(None, description="Inclusive end (YYYY-MM-DD); default today")
    last_n: int | None = Field(None, ge=1, le=2000, description="Trailing period length")
    unit: Literal["days", "weeks", "months", "years"] | None = None
    calendar_year: int | None = Field(None, ge=1990, le=2100, description="A whole calendar year; for the current year it ends today")

    @model_validator(mode="after")
    def _check(self):
        if (self.last_n is None) != (self.unit is None):
            raise ValueError("last_n and unit must be given together")
        styles = sum([self.start_date is not None or self.end_date is not None, self.last_n is not None, self.calendar_year is not None])
        if styles > 1:
            raise ValueError("use only one of: explicit dates, last_n+unit, calendar_year")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date is after end_date")
        return self

    def resolve(self, today: date) -> tuple[date | None, date, str]:
        """Return (start, end, label). start None means 'from the beginning of the data'."""
        if self.calendar_year is not None:
            y = self.calendar_year
            start, end = date(y, 1, 1), min(date(y, 12, 31), today)
            return start, end, f"calendar year {y}" + (" to date" if end < date(y, 12, 31) else "")
        if self.last_n is not None:
            end = today
            if self.unit == "days":
                start = end - timedelta(days=self.last_n)
            elif self.unit == "weeks":
                start = end - timedelta(weeks=self.last_n)
            elif self.unit == "months":
                start = add_months(end, -self.last_n)
            else:
                start = add_months(end, -12 * self.last_n)
            return start, end, f"last {self.last_n} {self.unit} ({start.isoformat()} to {end.isoformat()})"
        end = self.end_date or today
        if self.start_date or self.end_date:
            return self.start_date, end, f"{self.start_date or 'start of data'} to {end}"
        return None, end, "all available history"


def day_bounds(d: date | None, end: bool = False) -> str | None:
    if d is None:
        return None
    return (datetime(d.year, d.month, d.day) + (timedelta(days=1) if end else timedelta())).strftime("%Y-%m-%dT00:00:00Z")
