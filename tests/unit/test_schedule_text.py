"""The schedule sentence of the Recurring page (F3.1), in English and Polish."""

from datetime import date

import pytest

from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.web.schedule_text import describe_rule

TODAY = date(2026, 10, 31)
PAST = date(2026, 1, 1)

CASES = [
    (
        RecurrenceRule(PAST, month_day=10),
        "Monthly on the 10th",
        "Co miesiąc, 10. dnia",
    ),
    (
        RecurrenceRule(PAST, month_day=1),
        "Monthly on the 1st",
        "Co miesiąc, 1. dnia",
    ),
    (
        RecurrenceRule(PAST, month_day=22),
        "Monthly on the 22nd",
        "Co miesiąc, 22. dnia",
    ),
    (
        RecurrenceRule(PAST, month_day=-1),
        "Monthly on the last day",
        "Co miesiąc, ostatniego dnia",
    ),
    (
        RecurrenceRule(PAST, month_day=MonthDay.FIRST_WORKING),
        "Monthly on the first working day",
        "Co miesiąc, pierwszego dnia roboczego",
    ),
    (
        RecurrenceRule(date(2027, 1, 1), interval=3, month_day=15),
        "Every 3 months on the 15th, from January 2027",
        "Co 3 miesiące, 15. dnia, od stycznia 2027",
    ),
    (
        RecurrenceRule(date(2026, 2, 1), interval=6, month_day=MonthDay.LAST_WORKING),
        "Every 6 months on the last working day, from February 2026",
        "Co 6 miesięcy, ostatniego dnia roboczego, od lutego 2026",
    ),
    (
        RecurrenceRule(PAST, freq=Frequency.YEARLY, month=3, month_day=31),
        "Yearly on 31 March",
        "Co roku, 31 marca",
    ),
    (
        RecurrenceRule(PAST, freq=Frequency.YEARLY, month=2, month_day=29, interval=2),
        "Every 2 years on 29 February, from January 2026",
        "Co 2 lata, 29 lutego, od stycznia 2026",
    ),
    (
        RecurrenceRule(PAST, freq=Frequency.WEEKLY, weekdays=(0,)),
        "Weekly on Monday",
        "Co tydzień w poniedziałek",
    ),
    (
        RecurrenceRule(PAST, freq=Frequency.WEEKLY, weekdays=(4, 1, 2)),
        "Weekly on Tuesday, on Wednesday, and on Friday",
        "Co tydzień we wtorek, w środę i w piątek",
    ),
    (
        RecurrenceRule(date(2026, 11, 2), freq=Frequency.WEEKLY, weekdays=(6,), interval=2),
        "Every 2 weeks on Sunday, from Nov 2, 2026",
        "Co 2 tygodnie w niedzielę, od 2 lis 2026",
    ),
    (
        RecurrenceRule(PAST, month_day=10, end_date=date(2027, 12, 10)),
        "Monthly on the 10th, until December 2027",
        "Co miesiąc, 10. dnia, do grudnia 2027",
    ),
    (
        RecurrenceRule(PAST, month_day=5, max_count=12),
        "Monthly on the 5th, 12 times in total",
        "Co miesiąc, 5. dnia, łącznie 12 razy",
    ),
    (
        RecurrenceRule(PAST, month_day=5, max_count=1),
        "Monthly on the 5th, 1 time in total",
        "Co miesiąc, 5. dnia, łącznie 1 raz",
    ),
    (
        RecurrenceRule(PAST, month_day=10, weekend_policy=WeekendPolicy.PREVIOUS),
        "Monthly on the 10th; a weekend or holiday moves it to the working day before",
        "Co miesiąc, 10. dnia; w weekend lub święto przesuwane na poprzedni dzień roboczy",
    ),
    (
        RecurrenceRule(date(2026, 11, 3), freq=Frequency.ONCE),
        "Once on Nov 3, 2026",
        "Jednorazowo 3 lis 2026",
    ),
]


@pytest.mark.parametrize(("rule", "english", "polish"), CASES)
def test_sentence(rule: RecurrenceRule, english: str, polish: str) -> None:
    assert describe_rule(rule, "en", TODAY) == english
    assert describe_rule(rule, "pl", TODAY) == polish


def test_a_future_start_is_named_even_for_monthly_rules() -> None:
    rule = RecurrenceRule(date(2027, 1, 10), month_day=10)
    assert describe_rule(rule, "en", TODAY) == "Monthly on the 10th, from January 2027"
    assert describe_rule(rule, "en", date(2027, 1, 10)) == "Monthly on the 10th"
