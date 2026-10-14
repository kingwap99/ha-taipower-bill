"""Real-HA smoke test: does the integration set up and produce entities?"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "taipower_bill"

OPTIONS = {
    "label": "A戶",
    "source_entities": ["sensor.house_a_energy_this_month"],
    "source_mode": "manual",
    "cycle_days": 60,
    "last_read_date": "2026-09-07",
}


async def test_setup_manual_mode(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.house_a_energy_this_month", "123.4")
    entry = MockConfigEntry(
        domain=DOMAIN, title="A戶", options=dict(OPTIONS), unique_id="taipower_bill.a_house"
    )
    entry.add_to_hass(hass)

    ok = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    print("\n=== setup ok:", ok, "| state:", entry.state)

    reg = er.async_get(hass)
    ents = [e for e in reg.entities.values() if e.platform == DOMAIN]
    print("=== registered entities:", len(ents))
    bad = 0
    for e in sorted(ents, key=lambda x: x.entity_id):
        st = hass.states.get(e.entity_id)
        state = st.state if st else "<no state!>"
        if st is None or state in ("unavailable", "unknown"):
            bad += 1
        print(f"    {e.entity_id:58s} = {state}")
    print(f"=== unavailable/unknown/missing: {bad}")
    assert ok
    assert len(ents) >= 23, f"expected the full entity set, got {len(ents)}"
