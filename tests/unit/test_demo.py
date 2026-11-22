import asyncio

from demo.__main__ import CANCEL, GET_PRODUCT, contract_problems, invariant_failures, play


def stock_readings(rows):
    return [row["body"]["available_quantity"] for row in rows if row["template"] == GET_PRODUCT]


def test_the_defective_application_passes_every_per_response_check_and_still_breaks_the_inventory():
    rows = asyncio.run(play("defective"))
    cancels = [row["body"] for row in rows if row["template"] == CANCEL]

    assert [problem for row in rows for problem in contract_problems(row)] == []
    assert cancels[0] == cancels[1]
    assert stock_readings(rows) == [10, 7, 10, 13]
    assert rows[-1]["status"] == 201
    assert len(invariant_failures(rows)) == 2


def test_the_clean_twin_keeps_the_inventory_and_refuses_to_sell_units_that_do_not_exist():
    rows = asyncio.run(play("clean"))

    assert [problem for row in rows for problem in contract_problems(row)] == []
    assert stock_readings(rows) == [10, 7, 10, 10]
    assert rows[-1]["status"] == 409
    assert invariant_failures(rows) == []
