"""Pure F4.3 rules: CSV headers, values, templates and category mapping."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from assetboard.domain.categories import PALETTE, Kind, Treatment, name_key
from assetboard.domain.category_mapping import (
    CategorySnapshot,
    MappingChoice,
    MappingError,
    ResolvedMapping,
    SavedMapping,
    default_treatment,
    next_color,
    resolve_mapping,
    savings_words,
    validate_create_uniqueness,
)
from assetboard.domain.import_csv import (
    BUDGET_LINES,
    CATEGORIES,
    INVESTMENT_TRANSACTIONS,
    MONTHS,
    TEMPLATE_FILES,
    TRANSACTIONS,
    HeaderError,
    classify_header,
    detect_delimiter,
    parse_category_name,
    parse_currency,
    parse_date,
    parse_decimal,
    parse_direction,
    parse_kind,
    parse_month,
    parse_planned_amount,
    parse_sort_order,
    parse_timestamp,
    parse_transaction_amount,
    parse_treatment,
    parse_uuid,
)
from assetboard.domain.import_upload import parse_upload
from assetboard.domain.transactions import EntryError, clean_description

FOOD = UUID("11111111-1111-4111-8111-111111111111")
SALARY = UUID("22222222-2222-4222-8222-222222222222")
OTHER = UUID("33333333-3333-4333-8333-333333333333")


def _snapshot(
    category_id: UUID,
    *,
    kind: str = "expense",
    name: str = "Food",
    treatment: str = "regular",
    color: str = PALETTE[0],
    sort_order: int = 0,
    archived: bool = False,
) -> CategorySnapshot:
    return CategorySnapshot(
        id=category_id,
        kind=kind,
        name=name,
        name_key=name_key(name),
        treatment=treatment,
        color=color,
        sort_order=sort_order,
        archived=archived,
    )


# --- headers -----------------------------------------------------------------


def test_headers_are_trimmed_and_case_insensitive() -> None:
    classification = classify_header([" Date ", "DIRECTION", "Amount", "Category", "Extra"])

    assert classification.source == TRANSACTIONS
    assert classification.columns == ("date", "direction", "amount", "category", "extra")
    assert classification.unknown_columns == ("extra",)
    assert classification.missing_columns == ()


@pytest.mark.parametrize(
    ("header", "source"),
    [
        (("date", "direction", "amount", "category"), TRANSACTIONS),
        (("month", "kind", "category", "planned"), BUDGET_LINES),
        (("kind", "name"), CATEGORIES),
        (("instrument", "date", "type", "quantity", "unit_price"), INVESTMENT_TRANSACTIONS),
        (("month", "closed_at"), MONTHS),
    ],
)
def test_the_file_kind_comes_from_the_header(header: tuple[str, ...], source: str) -> None:
    assert classify_header(header).source == source


def test_a_missing_required_column_is_named() -> None:
    classification = classify_header(["date", "direction", "amount"])

    assert classification.missing_columns == ("category",)


def test_duplicate_and_unknown_headers_are_refused() -> None:
    with pytest.raises(HeaderError) as duplicate:
        classify_header(["date", "DATE", "amount", "category"])
    assert duplicate.value.code == "duplicate_column"

    with pytest.raises(HeaderError) as unknown:
        classify_header(["one", "two"])
    assert unknown.value.code == "unknown_file"


@pytest.mark.parametrize(
    ("header", "delimiter"),
    [
        ("date,direction,amount,category", ","),
        ("date;direction;amount;category", ";"),
        ("date;amount", ";"),
        ("date,direction", ","),
        ("one;two", ","),
    ],
)
def test_delimiter_detection_uses_known_column_names(header: str, delimiter: str) -> None:
    assert detect_delimiter(header) == delimiter


# --- values ------------------------------------------------------------------


def test_dates_are_strict_and_bounded() -> None:
    assert parse_date(" 2026-02-28 ") == date(2026, 2, 28)
    for text in ("2026-2-28", "28.02.2026", "2026-02-29", ""):
        with pytest.raises(EntryError) as error:
            parse_date(text)
        assert error.value.code == "invalid_date"
    with pytest.raises(EntryError) as out_of_range:
        parse_date("1899-12-31")
    assert out_of_range.value.code == "date_out_of_range"


def test_months_are_strict() -> None:
    assert str(parse_month("2026-02")) == "2026-02"
    for text in ("2026-2", "2026-13", "02.2026"):
        with pytest.raises(EntryError) as error:
            parse_month(text)
        assert error.value.code == "invalid_month"


@pytest.mark.parametrize(
    ("text", "decimal", "expected"),
    [
        ("12.5", "dot", Decimal("12.5")),
        ("12.500", "dot", Decimal("12.500")),
        ("12,5", "comma", Decimal("12.5")),
    ],
)
def test_plain_decimals_follow_the_upload_option(
    text: str, decimal: str, expected: Decimal
) -> None:
    assert parse_decimal(text, decimal=decimal) == expected


@pytest.mark.parametrize(
    ("text", "decimal"),
    [("12,5", "dot"), ("12.5", "comma"), ("1 234.56", "dot"), ("-1", "dot"), ("", "dot")],
)
def test_locale_or_signed_numbers_are_not_accepted(text: str, decimal: str) -> None:
    with pytest.raises(EntryError) as error:
        parse_decimal(text, decimal=decimal)
    assert error.value.code == "invalid_amount"


def test_amounts_use_the_currency_minor_unit_without_rounding() -> None:
    assert parse_transaction_amount("12.500", "EUR", decimal="dot") == 1250
    with pytest.raises(EntryError) as decimals:
        parse_transaction_amount("12.345", "EUR", decimal="dot")
    assert decimals.value.code == "too_many_decimals"
    with pytest.raises(EntryError) as zero:
        parse_transaction_amount("0", "EUR", decimal="dot")
    assert zero.value.code == "amount_not_positive"
    with pytest.raises(EntryError) as huge:
        parse_transaction_amount("99999999999999999", "EUR", decimal="dot")
    assert huge.value.code == "amount_too_large"


def test_planned_amounts_treat_zero_and_empty_as_no_line() -> None:
    assert parse_planned_amount("", "EUR", decimal="dot") == 0
    assert parse_planned_amount("0.00", "EUR", decimal="dot") == 0
    assert parse_planned_amount("12,5", "EUR", decimal="comma") == 1250
    with pytest.raises(EntryError) as negative:
        parse_planned_amount("-1", "EUR", decimal="dot")
    assert negative.value.code == "invalid_amount"


def test_enum_currency_and_text_values() -> None:
    assert parse_direction("expense") is Kind.EXPENSE
    with pytest.raises(EntryError) as direction:
        parse_direction("Expenses")
    assert direction.value.code == "invalid_direction"

    assert parse_kind("income") is Kind.INCOME
    with pytest.raises(EntryError) as kind:
        parse_kind("Expenses")
    assert kind.value.code == "invalid_kind"

    assert parse_treatment("") is Treatment.REGULAR
    assert parse_treatment("savings") is Treatment.SAVINGS
    with pytest.raises(EntryError) as treatment:
        parse_treatment("Savings")
    assert treatment.value.code == "invalid_treatment"

    assert parse_currency("", "EUR") == "EUR"
    assert parse_currency(" EUR ", "EUR") == "EUR"
    with pytest.raises(EntryError) as currency:
        parse_currency("USD", "EUR")
    assert currency.value.code == "invalid_currency"

    assert parse_sort_order(" 12 ") == 12
    for text in ("", "-1", "1.5"):
        with pytest.raises(EntryError) as sort_order:
            parse_sort_order(text)
        assert sort_order.value.code == "invalid_sort_order"


def test_timestamps_are_converted_to_utc() -> None:
    assert parse_timestamp("2026-01-01T02:00:00+02:00") == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(EntryError) as error:
        parse_timestamp("2026-01-01 02:00:00")
    assert error.value.code == "invalid_timestamp"


def test_uuids_names_and_descriptions_follow_the_shared_rules() -> None:
    identifier = parse_uuid(" 11111111-1111-4111-8111-111111111111 ")
    assert identifier == FOOD
    with pytest.raises(EntryError) as bad_id:
        parse_uuid("not-an-id")
    assert bad_id.value.code == "invalid_id"

    assert parse_category_name("  Eating   out ") == "Eating out"
    with pytest.raises(EntryError) as bad_name:
        parse_category_name(" ")
    assert bad_name.value.code == "invalid_category_name"

    assert clean_description("  Coffee   and cake ") == "Coffee and cake"
    with pytest.raises(EntryError) as long_description:
        clean_description("x" * 201)
    assert long_description.value.code == "description_too_long"


# --- templates ---------------------------------------------------------------


def test_transactions_template_is_the_documented_hand_made_example() -> None:
    assert TEMPLATE_FILES["transactions.csv"] == (
        b"date,direction,amount,category,description\r\n"
        b"2025-01-03,expense,12.5,Food,Bakery\r\n"
        b"2025-01-03,expense,12.5,Food,Bakery\r\n"
        b"2025-01-31,income,3000,Salary,\r\n"
    )


def test_budget_template_has_the_minimal_columns_and_example_rows() -> None:
    content = TEMPLATE_FILES["budget_lines.csv"]
    assert content.startswith(b"month,kind,category,planned\r\n")
    assert content == (
        b"month,kind,category,planned\r\n"
        b"2026-09,expense,Food,550.00\r\n"
        b"2026-09,income,Salary,3000.00\r\n"
    )

    parsed, errors = parse_upload(content)
    assert errors == ()
    assert parsed is not None
    assert parsed.source == BUDGET_LINES
    assert len(parsed.csv_rows) == 2


def test_both_templates_parse_as_their_own_kind() -> None:
    for filename, source in (
        ("transactions.csv", TRANSACTIONS),
        ("budget_lines.csv", BUDGET_LINES),
    ):
        parsed, errors = parse_upload(TEMPLATE_FILES[filename])
        assert errors == ()
        assert parsed is not None
        assert parsed.source == source


# --- mapping -----------------------------------------------------------------


def test_a_same_name_active_category_is_the_default_mapping() -> None:
    food = _snapshot(FOOD)

    mapping = resolve_mapping(
        kind="expense",
        source_name="FOOD",
        source_name_key=name_key("FOOD"),
        categories=[food],
        saved=None,
        choice=None,
    )

    assert mapping.action == "map"
    assert mapping.category_id == FOOD


def test_an_archived_same_name_category_does_not_catch_history() -> None:
    archived = _snapshot(FOOD, archived=True)

    mapping = resolve_mapping(
        kind="expense",
        source_name="Food",
        source_name_key=name_key("Food"),
        categories=[archived],
        saved=None,
        choice=None,
    )

    assert mapping.action == "create"
    assert mapping.create_name == "Food"


@pytest.mark.parametrize(
    ("name", "treatment"),
    [
        ("Savings", "savings"),
        ("Oszczędności", "savings"),
        ("Inwestycje", "savings"),
        ("Food", "regular"),
    ],
)
def test_savings_words_default_new_categories_to_savings(name: str, treatment: str) -> None:
    mapping = resolve_mapping(
        kind="expense",
        source_name=name,
        source_name_key=name_key(name),
        categories=[],
        saved=None,
        choice=None,
    )

    assert mapping.action == "create"
    assert mapping.create_treatment == treatment


def test_savings_words_are_the_union_of_both_languages() -> None:
    assert name_key("savings") in savings_words()
    assert name_key("oszczędności") in savings_words()
    assert default_treatment(name_key("investments")) == "savings"
    assert default_treatment(name_key("inwestycje")) == "savings"
    assert default_treatment(name_key("food")) == "regular"


def test_a_saved_mapping_beats_a_same_name_category() -> None:
    food = _snapshot(FOOD)
    other = _snapshot(OTHER, name="Groceries")

    mapping = resolve_mapping(
        kind="expense",
        source_name="Food",
        source_name_key=name_key("Food"),
        categories=[food, other],
        saved=SavedMapping("expense", name_key("Food"), "map", OTHER),
        choice=None,
    )

    assert mapping.action == "map"
    assert mapping.category_id == OTHER


def test_a_saved_ignore_is_kept_and_a_missing_saved_target_is_invalid() -> None:
    ignored = resolve_mapping(
        kind="expense",
        source_name="Food",
        source_name_key=name_key("Food"),
        categories=[_snapshot(FOOD)],
        saved=SavedMapping("expense", name_key("Food"), "ignore", None),
        choice=None,
    )
    assert ignored.action == "ignore"

    with pytest.raises(MappingError) as missing:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[],
            saved=SavedMapping("expense", name_key("Food"), "map", FOOD),
            choice=None,
        )
    assert missing.value.code == "mapping_category_missing"


def test_a_posted_choice_overrides_saved_and_default_rules() -> None:
    food = _snapshot(FOOD)
    mapping = resolve_mapping(
        kind="expense",
        source_name="Food",
        source_name_key=name_key("Food"),
        categories=[food],
        saved=SavedMapping("expense", name_key("Food"), "map", FOOD),
        choice=MappingChoice("expense", name_key("Food"), "ignore"),
    )
    assert mapping.action == "ignore"

    created = resolve_mapping(
        kind="expense",
        source_name="Food",
        source_name_key=name_key("Food"),
        categories=[food],
        saved=None,
        choice=MappingChoice(
            "expense",
            name_key("Food"),
            "create",
            create_name="Groceries",
            create_treatment="savings",
            create_color="#123456",
        ),
    )
    assert created.action == "create"
    assert (created.create_name, created.create_treatment, created.create_color) == (
        "Groceries",
        "savings",
        "#123456",
    )


def test_mapping_choices_are_validated() -> None:
    food = _snapshot(FOOD)
    salary = _snapshot(SALARY, kind="income", name="Salary")

    with pytest.raises(MappingError) as missing:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[food],
            saved=None,
            choice=MappingChoice("expense", name_key("Food"), "map", OTHER),
        )
    assert missing.value.code == "mapping_category_missing"

    with pytest.raises(MappingError) as wrong_kind:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[food, salary],
            saved=None,
            choice=MappingChoice("expense", name_key("Food"), "map", SALARY),
        )
    assert wrong_kind.value.code == "mapping_kind_mismatch"

    with pytest.raises(MappingError) as clash:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[food],
            saved=None,
            choice=MappingChoice("expense", name_key("Food"), "create", create_name="Food"),
        )
    assert clash.value.code == "category_exists"

    for invalid_name in ("", " "):
        with pytest.raises(MappingError) as name:
            resolve_mapping(
                kind="expense",
                source_name="Food",
                source_name_key=name_key("Food"),
                categories=[],
                saved=None,
                choice=MappingChoice(
                    "expense", name_key("Food"), "create", create_name=invalid_name
                ),
            )
        assert name.value.code == "invalid_category_name"

    with pytest.raises(MappingError) as treatment:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[],
            saved=None,
            choice=MappingChoice(
                "expense",
                name_key("Food"),
                "create",
                create_name="Food",
                create_treatment="other",
            ),
        )
    assert treatment.value.code == "invalid_treatment"

    with pytest.raises(MappingError) as color:
        resolve_mapping(
            kind="expense",
            source_name="Food",
            source_name_key=name_key("Food"),
            categories=[],
            saved=None,
            choice=MappingChoice(
                "expense",
                name_key("Food"),
                "create",
                create_name="Food",
                create_color="red",
            ),
        )
    assert color.value.code == "invalid_color"


def test_a_created_id_resolves_to_the_new_category_on_apply() -> None:
    mapping = resolve_mapping(
        kind="expense",
        source_name="Groceries",
        source_name_key=name_key("Groceries"),
        categories=[],
        saved=None,
        choice=MappingChoice("expense", name_key("Groceries"), "create", create_name="Food"),
        created_ids={("expense", name_key("Groceries")): FOOD},
    )

    assert mapping.action == "map"
    assert mapping.category_id == FOOD


def test_duplicate_create_choices_are_refused() -> None:
    first = ResolvedMapping("expense", "A", "a", "create", None, create_name="Food")
    second = ResolvedMapping("expense", "B", "b", "create", None, create_name="FOOD")
    other_kind = ResolvedMapping("income", "C", "c", "create", None, create_name="Food")

    with pytest.raises(MappingError) as duplicate:
        validate_create_uniqueness([first, second, other_kind])
    assert duplicate.value.code == "duplicate_create_name"

    validate_create_uniqueness([first, other_kind])


def test_next_colour_is_deterministic_and_avoids_used_palette_colours() -> None:
    empty: list[CategorySnapshot] = []
    assert next_color("expense", empty) == PALETTE[0]
    assert next_color("expense", empty) == PALETTE[0]

    one = [_snapshot(FOOD, color=PALETTE[0])]
    assert next_color("expense", one) == PALETTE[1]
    assert next_color("income", one) == PALETTE[0]
    assert next_color("expense", one, offset=1) == PALETTE[2]

    all_colours = [
        _snapshot(UUID(int=index + 1), color=color) for index, color in enumerate(PALETTE)
    ]
    assert next_color("expense", all_colours) == PALETTE[len(all_colours) % len(PALETTE)]
