from __future__ import annotations

from assetboard.web.rendering import templates_for


def test_form_field_renders_validation_errors_safely() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import field %}'
        '{{ field("amount", "Amount", error="<invalid>") }}'
    ).render()

    assert 'aria-invalid="true"' in rendered
    assert 'aria-describedby="amount-error"' in rendered
    assert "&lt;invalid&gt;" in rendered


def test_undo_toast_uses_htmx_out_of_band_swap() -> None:
    templates = templates_for("en")
    rendered = templates.env.from_string(
        '{% from "macros/components.html" import toast %}'
        '{{ toast("toast-container", "Deleted", "/undo", "Undo") }}'
    ).render()

    assert 'hx-swap-oob="true"' in rendered
    assert 'hx-post="/undo"' in rendered
    assert "Undo" in rendered
