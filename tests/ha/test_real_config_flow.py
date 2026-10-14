"""The config flow, on a real Home Assistant.

This is the very first thing anybody touches: 新增整合 → 填表 → 完成.  v0.2.0
could not even be added to an instance, so it is worth pinning the flows end to
end rather than trusting the offline suite, which never renders a real
voluptuous schema, a real selector, or a real entity registry.

Covered: 新增 (user) → a working entry, the label → statistics-id rule, the
duplicate abort, the two layers of input checking (selector vs ``validate_basics``),
重新設定 (reconfigure) and the 選項 (options) flow that carries the money knobs.
"""

from __future__ import annotations

from datetime import timedelta

import homeassistant.util.dt as dt_util
import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "taipower_bill"

USER_INPUT = {
    "label": "A戶",
    "source_entities": ["sensor.house_a_energy_this_month"],
    "source_mode": "manual",
    "last_read_date": "2026-09-07",
    "cycle_days": 60,
}


async def _form(hass: HomeAssistant) -> dict:
    """Start a 新增 flow and assert it rendered a form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _add(hass: HomeAssistant, **overrides) -> dict:
    result = await _form(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, **overrides}
    )
    await hass.async_block_till_done()
    return result


async def test_the_user_flow_creates_a_working_entry(hass: HomeAssistant) -> None:
    """新增整合 → 填表 → 真的生出 entity（不是只有一筆 config entry）。"""
    result = await _add(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "A戶"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.data["label"] == "A戶"
    assert entry.state is ConfigEntryState.LOADED

    # ...and the platforms came up, not just the entry.
    sensors = [s for s in hass.states.async_entity_ids("sensor") if "tai_dian" in s]
    assert sensors, "the flow created an entry that produced no entities"


async def test_the_label_becomes_the_statistics_id(hass: HomeAssistant) -> None:
    """`unique_id` (＝外部統計 id 的來源) 由戶別推導，非英數字會被收斂掉。

    ``_slug`` lower-cases and replaces every run of non ``[a-z0-9]`` with ``_``,
    so 「A戶」→ ``taipower_bill.a``.  That is deliberate -- the value ends up in
    a statistic_id, which HA validates as ``domain:object`` -- but it means two
    labels that differ only in CJK characters collapse to the same id, and the
    duplicate check then refuses the second one instead of silently colliding.
    """
    await _add(hass)
    assert hass.config_entries.async_entries(DOMAIN)[0].unique_id == "taipower_bill.a"

    await _add(hass, label="B戶")
    assert sorted(e.unique_id for e in hass.config_entries.async_entries(DOMAIN)) == [
        "taipower_bill.a",
        "taipower_bill.b",
    ]


async def test_a_duplicate_label_aborts(hass: HomeAssistant) -> None:
    """Two 戶 with the same name would collide on the statistics ids."""
    await _add(hass)
    result = await _add(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_bad_input_is_refused_with_a_readable_error(hass: HomeAssistant) -> None:
    """``validate_basics`` errors come back as a re-rendered form, not a crash."""
    too_old = (dt_util.now().date() - timedelta(days=401)).isoformat()
    cases = (
        ({"label": "   "}, {"label": "label_required"}),
        # 統計模式沒有來源就沒有度數可用。
        (
            {"source_mode": "statistics", "source_entities": []},
            {"source_entities": "source_required"},
        ),
        ({"last_read_date": too_old}, {"last_read_date": "last_read_too_old"}),
    )
    for overrides, expected in cases:
        result = await _form(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, **overrides}
        )
        assert result["type"] is FlowResultType.FORM, overrides
        assert result["errors"] == expected, overrides
        assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_future_read_date_is_refused(hass: HomeAssistant) -> None:
    """The exact mistake that made every sensor blank in v0.1."""
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()
    result = await _form(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "last_read_date": tomorrow}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"last_read_date": "last_read_in_future"}


async def test_the_selector_rejects_a_malformed_date_before_the_validator(
    hass: HomeAssistant,
) -> None:
    """``DateSelector`` is the first line of defence and raises ``InvalidData``.

    A malformed date never reaches ``validate_basics``: the schema itself
    refuses it.  HA surfaces that to the frontend as a validation error rather
    than a re-rendered form, and the flow must survive it -- so this also checks
    that the same flow can be completed afterwards.
    """
    result = await _form(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "last_read_date": "not-a-date"}
        )

    assert hass.config_entries.async_entries(DOMAIN) == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_options_flow_stores_the_money_knobs(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="A戶",
        options=dict(USER_INPUT),
        unique_id="taipower_bill.a",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"surcharge_per_kwh": 0.5, "business_tax_rate": 0.05, "min_charge_per_month": 120},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.options["surcharge_per_kwh"] == 0.5
    assert entry.options["business_tax_rate"] == 0.05
    assert entry.options["min_charge_per_month"] == 120
    # The update listener reloads on an options change, so the entry stays usable.
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_renders_and_saves(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="A戶",
        data=dict(USER_INPUT),
        unique_id="taipower_bill.a",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "last_read_date": "2026-08-07"}
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    # ``async_update_reload_and_abort`` rewrites the entry and reloads it.
    assert entry.data["last_read_date"] == "2026-08-07"
    assert entry.state is ConfigEntryState.LOADED
