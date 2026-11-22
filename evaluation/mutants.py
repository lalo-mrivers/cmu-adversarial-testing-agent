import json
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sandbox.target_app.src import app as app_module
from sandbox.target_app.src.inventory import Inventory, InventoryError, _Reservation


class Recorder:
    """Records ingress and defect events in request order, in memory and in a JSONL file if given."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.requests = 0
        self.log: list[dict[str, Any]] = []

    def _write(self, event: dict[str, Any]) -> None:
        self.log.append(event)

        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event) + "\n")

    def request(self, method: str, path: str, headers: list[str] | None = None) -> None:
        self.requests += 1
        self._write({"type": "request", "index": self.requests, "method": method, "path": path,
                     "headers": sorted(headers or [])})

    def fire(self, mutant: str) -> None:
        self._write({"type": "fired", "mutant": mutant, "request_index": self.requests})


Mutant = Callable[[Any, Recorder], None]
MUTANTS: dict[str, Mutant] = {}


def mutant(function: Mutant) -> Mutant:
    MUTANTS[function.__name__] = function

    return function


def _bind(inventory: Inventory, name: str, function: Callable[..., Any]) -> None:
    setattr(inventory, name, types.MethodType(function, inventory))


# ---------------------------------------------------------------- REQ-PRODUCT-001 ----------

@mutant
def product_wrong_id(app, rec):
    original = app.state.inventory.get_product

    def get_product(self, product_id):
        rec.fire("product_wrong_id")

        return {**original(product_id), "id": "fixture-other"}

    _bind(app.state.inventory, "get_product", get_product)


@mutant
def product_quantity_string(app, rec):
    original = app.state.inventory.get_product

    def get_product(self, product_id):
        result = original(product_id)
        rec.fire("product_quantity_string")

        return {**result, "available_quantity": str(result["available_quantity"])}

    _bind(app.state.inventory, "get_product", get_product)


@mutant
def product_missing_200(app, rec):
    original = app.state.inventory.get_product

    def get_product(self, product_id):
        try:
            return original(product_id)

        except InventoryError:
            rec.fire("product_missing_200")

            return {"id": product_id, "available_quantity": 0}

    _bind(app.state.inventory, "get_product", get_product)


@mutant
def product_read_mutates(app, rec):
    original = app.state.inventory.get_product

    def get_product(self, product_id):
        result = original(product_id)

        if self._stock[product_id] > 0:
            rec.fire("product_read_mutates")
            self._stock[product_id] -= 1

        return result

    _bind(app.state.inventory, "get_product", get_product)


# ---------------------------------------------------------------- REQ-RESERVATION-001 ------

@mutant
def reserve_accepts_zero(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        if (isinstance(payload, dict) and set(payload) == {"product_id", "quantity"}
                and type(payload["quantity"]) is int and payload["quantity"] == 0
                and payload["product_id"] in self._stock):
            rec.fire("reserve_accepts_zero")
            reservation = _Reservation(id="reservation-zero", product_id=payload["product_id"], quantity=0)
            self._reservations[reservation.id] = reservation

            return reservation.as_dict()

        return original(payload)

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_accepts_bool(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        if isinstance(payload, dict) and type(payload.get("quantity")) is bool:
            rec.fire("reserve_accepts_bool")
            payload = {**payload, "quantity": int(payload["quantity"])}

        return original(payload)

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_accepts_extra_field(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        if isinstance(payload, dict) and set(payload) > {"product_id", "quantity"}:
            rec.fire("reserve_accepts_extra_field")
            payload = {key: payload[key] for key in ("product_id", "quantity") if key in payload}

        return original(payload)

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_validates_product_first(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        if (isinstance(payload, dict) and isinstance(payload.get("product_id"), str)
                and payload["product_id"] not in self._stock):
            rec.fire("reserve_validates_product_first")
            raise InventoryError("PRODUCT_NOT_FOUND")

        return original(payload)

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_duplicate_keys_accepted(app, rec):
    """The last value of a repeated key wins instead of rejecting the body. Patches the module."""

    def decode(body: bytes) -> Any:
        seen: list[bool] = []

        def last_wins(pairs):
            if len({key for key, _ in pairs}) != len(pairs):
                seen.append(True)

            return dict(pairs)

        document = json.loads(body.decode("utf-8"), object_pairs_hook=last_wins,
                              parse_constant=app_module._reject_constant,
                              parse_int=app_module._parse_integer, parse_float=app_module.Decimal)

        if seen:
            rec.fire("reserve_duplicate_keys_accepted")

        return document

    app_module._decode_json = decode


@mutant
def reserve_malformed_json_status(app, rec):
    """Malformed JSON is answered with 422 instead of 400. Patches the module, not the instance."""
    original = app_module._error_response

    def error_response(code, status_code, headers=None):
        if code == "INVALID_JSON":
            rec.fire("reserve_malformed_json_status")
            status_code = 422

        return original(code, status_code, headers)

    app_module._error_response = error_response


# ---------------------------------------------------------------- REQ-RESERVATION-002 ------

@mutant
def reserve_deducts_extra(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        result = original(payload)

        if self._stock[result["product_id"]] > 0:
            rec.fire("reserve_deducts_extra")
            self._stock[result["product_id"]] -= 1

        return result

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_off_by_one_stock(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        try:
            return original(payload)

        except InventoryError as error:
            if error.code != "INSUFFICIENT_STOCK" or payload["quantity"] != self._stock[payload["product_id"]] + 1:
                raise

            rec.fire("reserve_off_by_one_stock")
            reservation = _Reservation(id="reservation-off-by-one", product_id=payload["product_id"],
                                       quantity=payload["quantity"])
            self._reservations[reservation.id] = reservation
            self._stock[payload["product_id"]] = 0

            return reservation.as_dict()

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_partial_on_insufficient(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        try:
            return original(payload)

        except InventoryError as error:
            if error.code == "INSUFFICIENT_STOCK" and self._stock[payload["product_id"]] > 0:
                rec.fire("reserve_partial_on_insufficient")
                self._stock[payload["product_id"]] -= 1

            raise

    _bind(app.state.inventory, "create_reservation", create_reservation)


@mutant
def reserve_id_reused(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        result = original(payload)

        if len(self._reservations) > 1:
            rec.fire("reserve_id_reused")
            stored = self._reservations.pop(result["id"])
            first = next(iter(self._reservations.values()))
            self._reservations[first.id] = _Reservation(
                id=first.id, product_id=stored.product_id, quantity=stored.quantity)

            return {**result, "id": first.id}

        return result

    _bind(app.state.inventory, "create_reservation", create_reservation)


# ---------------------------------------------------------------- REQ-RESERVATION-003 ------

@mutant
def reservation_get_stale_status(app, rec):
    original = app.state.inventory.get_reservation

    def get_reservation(self, reservation_id):
        result = original(reservation_id)

        if result["status"] == "CANCELLED":
            rec.fire("reservation_get_stale_status")

            return {**result, "status": "ACTIVE"}

        return result

    _bind(app.state.inventory, "get_reservation", get_reservation)


@mutant
def reservation_cancelled_not_found(app, rec):
    original = app.state.inventory.get_reservation

    def get_reservation(self, reservation_id):
        result = original(reservation_id)

        if result["status"] == "CANCELLED":
            rec.fire("reservation_cancelled_not_found")
            raise InventoryError("RESERVATION_NOT_FOUND")

        return result

    _bind(app.state.inventory, "get_reservation", get_reservation)


# ---------------------------------------------------------------- REQ-RESERVATION-004 ------

@mutant
def cancel_restores_twice(app, rec):
    original = app.state.inventory.cancel_reservation

    def cancel_reservation(self, reservation_id):
        before = self._reservations.get(reservation_id)
        result = original(reservation_id)

        if before is not None and before.status == "CANCELLED":
            rec.fire("cancel_restores_twice")
            self._stock[before.product_id] += before.quantity

        return result

    _bind(app.state.inventory, "cancel_reservation", cancel_reservation)


@mutant
def cancel_restores_wrong_quantity(app, rec):
    original = app.state.inventory.cancel_reservation

    def cancel_reservation(self, reservation_id):
        before = self._reservations.get(reservation_id)
        result = original(reservation_id)

        if before is not None and before.status == "ACTIVE" and before.quantity > 1:
            rec.fire("cancel_restores_wrong_quantity")
            self._stock[before.product_id] -= 1

        return result

    _bind(app.state.inventory, "cancel_reservation", cancel_reservation)


@mutant
def cancel_unknown_ok(app, rec):
    original = app.state.inventory.cancel_reservation

    def cancel_reservation(self, reservation_id):
        try:
            return original(reservation_id)

        except InventoryError:
            rec.fire("cancel_unknown_ok")

            return {"id": reservation_id, "product_id": "product-001", "quantity": 0, "status": "CANCELLED"}

    _bind(app.state.inventory, "cancel_reservation", cancel_reservation)


# ---------------------------------------------------------------- REQ-INVENTORY-001 ---------

@mutant
def cancel_leaks_other_product(app, rec):
    original = app.state.inventory.cancel_reservation

    def cancel_reservation(self, reservation_id):
        before = self._reservations.get(reservation_id)
        result = original(reservation_id)

        if before is not None and before.status == "ACTIVE":
            other = "product-002" if before.product_id != "product-002" else "product-001"
            rec.fire("cancel_leaks_other_product")
            self._stock[other] += 1

        return result

    _bind(app.state.inventory, "cancel_reservation", cancel_reservation)


@mutant
def reserve_touches_other_product(app, rec):
    original = app.state.inventory.create_reservation

    def create_reservation(self, payload):
        result = original(payload)
        other = "product-002" if result["product_id"] != "product-002" else "product-001"

        if self._stock[other] > 0:
            rec.fire("reserve_touches_other_product")
            self._stock[other] -= 1

        return result

    _bind(app.state.inventory, "create_reservation", create_reservation)
