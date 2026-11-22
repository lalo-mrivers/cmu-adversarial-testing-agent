import asyncio
import re
from pathlib import Path
from typing import Any

import httpx

from sandbox.target_app.src.app import create_app

SEEDS = Path(__file__).resolve().parents[1] / "sandbox" / "target_app" / "seed"
VARIANTS = {"clean": "products.json", "defective": "products_cancel_retry.json"}
PRODUCT = "product-001"
INITIAL_STOCK = 10
GET_PRODUCT = "/products/{product_id}"
CREATE = "/reservations"
CANCEL = "/reservations/{reservation_id}/cancel"
RESERVATION_FIELDS = {"id", "product_id", "quantity", "status"}


async def play(variant: str) -> list[dict[str, Any]]:
    """The retry scenario and its business consequence, straight against the ASGI application.

    The two variants differ only in their seed file: the defective one switches a fault on.
    """
    app = create_app(SEEDS / VARIANTS[variant])
    rows: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox.invalid") as client:
        async def send(label: str, method: str, template: str, path: str, body: Any = None) -> Any:
            reply = await client.request(method, path, **({} if body is None else {"json": body}))
            rows.append({"label": label, "method": method, "template": template, "path": path,
                         "status": reply.status_code, "body": reply.json()})

            return rows[-1]["body"]

        stock = f"/products/{PRODUCT}"
        await send("read the stock", "GET", GET_PRODUCT, stock)
        created = await send("reserve 3 units", "POST", CREATE, "/reservations",
                             {"product_id": PRODUCT, "quantity": 3})
        cancel = f"/reservations/{created['id']}/cancel"
        await send("read the stock", "GET", GET_PRODUCT, stock)
        await send("cancel", "POST", CANCEL, cancel)
        await send("read the stock", "GET", GET_PRODUCT, stock)
        await send("cancel again (retry)", "POST", CANCEL, cancel)
        await send("read the stock", "GET", GET_PRODUCT, stock)
        await send("reserve 13 units", "POST", CREATE, "/reservations", {"product_id": PRODUCT, "quantity": 13})

    return rows


def contract_problems(row: dict[str, Any]) -> list[str]:
    """What a per-response contract check sees: documented status code and body shape, nothing else."""
    status, body, template = row["status"], row["body"], row["template"]

    if template == GET_PRODUCT:
        valid = (status == 200 and set(body) == {"id", "available_quantity"}
                 and type(body["available_quantity"]) is int and body["available_quantity"] >= 0)

    elif template == CREATE and status == 409:
        valid = body == {"error": {"code": "INSUFFICIENT_STOCK"}}

    elif template == CREATE:
        valid = status == 201 and set(body) == RESERVATION_FIELDS and body["status"] == "ACTIVE"

    else:
        valid = status == 200 and set(body) == RESERVATION_FIELDS and body["status"] == "CANCELLED"

    return [] if valid else [f"unexpected shape or status ({status})"]


def invariant_failures(rows: list[dict[str, Any]]) -> list[str]:
    """REQ-INVENTORY-001 recomputed after every call: stock = initial stock - units of ACTIVE reservations."""
    active: dict[str, int] = {}
    failures: list[str] = []

    for number, row in enumerate(rows, 1):
        body = row["body"]

        if row["template"] == CREATE and row["status"] == 201:
            active[body["id"]] = body["quantity"]

        elif row["template"] == CANCEL and row["status"] == 200:
            active.pop(body["id"], None)

        elif row["template"] == GET_PRODUCT and body["available_quantity"] != INITIAL_STOCK - sum(active.values()):
            failures.append(f"#{number}: stock reads {body['available_quantity']}, "
                            f"initial stock minus ACTIVE units is {INITIAL_STOCK - sum(active.values())}")

        if sum(active.values()) > INITIAL_STOCK:
            failures.append(f"#{number}: ACTIVE reservations hold {sum(active.values())} units "
                            f"but only {INITIAL_STOCK} exist")

    return failures


def _short(path: str) -> str:
    """Keep the tail of a reservation identifier: enough to see it is the same reservation."""

    return re.sub(r"reservation-[0-9a-f]{26}([0-9a-f]{6})", r"reservation-..\1", path)


def _observed(row: dict[str, Any]) -> str:
    body = row["body"]

    if "error" in body:
        return f"error {body['error']['code']}"

    if "available_quantity" in body:
        return f"stock = {body['available_quantity']}"

    return f"{body['status']}, quantity {body['quantity']}"


def transcript() -> int:
    outcomes: dict[str, dict[str, Any]] = {}

    for variant in ("clean", "defective"):
        rows = asyncio.run(play(variant))
        problems = [problem for row in rows for problem in contract_problems(row)]
        cancels = [row["body"] for row in rows if row["template"] == CANCEL]
        outcomes[variant] = {"contract": problems, "same_cancel_body": cancels[0] == cancels[1],
                             "invariant": invariant_failures(rows)}
        print(f"\n=== {variant.upper()} application ===")
        print(f"{'#':>2}  {'step':<22} {'request':<48} {'HTTP':>4}  observed")

        for number, row in enumerate(rows, 1):
            print(f"{number:>2}  {row['label']:<22} {row['method'] + ' ' + _short(row['path']):<48} "
                  f"{row['status']:>4}  {_observed(row)}")

    print("\n=== What each check sees ===")
    print(f"{'':<46}{'clean':<14}defective")

    for name, ok in (
        ("Per-response contract (status + body shape)", lambda o: not o["contract"]),
        ("Retry answers with the same body as the first", lambda o: o["same_cancel_body"]),
        ("Inventory invariant (REQ-INVENTORY-001)", lambda o: not o["invariant"]),
    ):
        print(f"{name:<46}{'pass' if ok(outcomes['clean']) else 'FAIL':<14}"
              f"{'pass' if ok(outcomes['defective']) else 'FAIL'}")

    for failure in outcomes["defective"]["invariant"]:
        print(f"  defective, invariant: {failure}")

    return 0


if __name__ == "__main__":
    raise SystemExit(transcript())
