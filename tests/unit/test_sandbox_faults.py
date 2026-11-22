import json
from pathlib import Path

import pytest

from sandbox.target_app.src.inventory import Inventory

SEEDS = Path(__file__).resolve().parents[2] / "sandbox" / "target_app" / "seed"
PRODUCTS = [{"id": "product-001", "available_quantity": 10}]


def stock(inventory):
    return inventory.get_product("product-001")["available_quantity"]


def cancel_twice(inventory):
    reservation = inventory.create_reservation({"product_id": "product-001", "quantity": 3})
    inventory.cancel_reservation(reservation["id"])
    first = stock(inventory)
    answer = inventory.cancel_reservation(reservation["id"])

    return first, stock(inventory), answer, reservation


def test_the_default_seed_restores_units_exactly_once():
    first, second, answer, reservation = cancel_twice(Inventory(SEEDS / "products.json"))

    assert (first, second) == (10, 10)
    assert answer == {**reservation, "status": "CANCELLED"}


def test_the_flawed_seed_answers_the_retry_identically_but_restores_the_units_again():
    clean = cancel_twice(Inventory(SEEDS / "products.json"))
    flawed = Inventory(SEEDS / "products_cancel_retry.json")
    first, second, answer, reservation = cancel_twice(flawed)

    assert (first, second) == (10, 13)
    assert answer == {**reservation, "status": "CANCELLED"}
    assert answer["status"] == clean[2]["status"] and answer["quantity"] == clean[2]["quantity"]


def test_a_fault_leaves_no_trace_in_what_an_agent_can_read():
    inventory = Inventory(SEEDS / "products_cancel_retry.json")

    assert "fault" not in json.dumps(inventory.snapshot_state()).lower()
    assert inventory.snapshot_state() == Inventory(SEEDS / "products.json").snapshot_state()

    inventory.reset()
    assert cancel_twice(inventory)[:2] == (10, 13)  # A reset restores the stock, not the defect.


@pytest.mark.parametrize("faults", ["cancel_restores_twice", ["nope"], [1], {"a": 1}])
def test_unknown_or_malformed_faults_are_rejected(tmp_path, faults):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"products": PRODUCTS, "faults": faults}), encoding="utf-8")

    with pytest.raises(ValueError, match="faults"):
        Inventory(seed)


def test_the_seed_still_rejects_other_unknown_keys(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"products": PRODUCTS, "extra": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="products"):
        Inventory(seed)
