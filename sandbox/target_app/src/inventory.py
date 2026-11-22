import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import uuid4


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+")

##nventario y reservas en memoria para la aplicación de prueba
class InventoryError(Exception):
    """Business error that the HTTP layer will convert into a response."""

    def __init__(
        self,
        code: Literal[
            "INVALID_REQUEST",
            "PRODUCT_NOT_FOUND",
            "INSUFFICIENT_STOCK",
            "RESERVATION_NOT_FOUND",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Reservation:
    """Internal immutable record of a reservation."""

    id: str
    product_id: str
    quantity: int
    status: Literal["ACTIVE", "CANCELLED"] = "ACTIVE"

    def as_dict(self) -> dict[str, str | int]:
        """Returns a representation independent of the internal record."""

        return {
            "id": self.id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "status": self.status,
        }


def _unique_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Rejects JSON objects with duplicate keys."""
    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}.")

        result[key] = value

    return result


def _reject_constant(value: str) -> Any:
    """Rejects non-finite JSON numbers allowed by the parser."""
    raise ValueError(f"Non-finite JSON number: {value}.")


## Defects that a seed can switch on, so that a registered system can run against a flawed variant.
## The default seed has none. A defect changes behavior only; no response or snapshot names it.
KNOWN_FAULTS = frozenset({"cancel_restores_twice"})


def _load_seed(seed_path: Path) -> tuple[dict[str, int], frozenset[str]]:
    """Loads and validates the products (and optional faults) before creating the inventory."""
    document = json.loads(
        seed_path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=_unique_keys,
        parse_constant=_reject_constant,
        parse_float=Decimal,
    )

    if (
        not isinstance(document, dict)
        or not {"products"} <= set(document) <= {"products", "faults"}
    ):
        raise ValueError(
            "The seed must be an object containing 'products' and, optionally, 'faults'."
        )

    faults = document.get("faults", [])

    if (
        not isinstance(faults, list)
        or any(not isinstance(fault, str) for fault in faults)
        or not set(faults) <= KNOWN_FAULTS
    ):
        raise ValueError(
            f"'faults' must be a list of names among {sorted(KNOWN_FAULTS)}."
        )

    products = document["products"]

    if not isinstance(products, list) or not products:
        raise ValueError(
            "'products' must be a non-empty JSON array."
        )

    initial_stock: dict[str, int] = {}

    for product in products:
        if (
            not isinstance(product, dict)
            or set(product) != {"id", "available_quantity"}
        ):
            raise ValueError(
                "Each seed product must contain exactly "
                "'id' and 'available_quantity'."
            )

        product_id = product["id"]
        quantity = product["available_quantity"]

        if (
            not isinstance(product_id, str)
            or _IDENTIFIER.fullmatch(product_id) is None
        ):
            raise ValueError(
                "Product IDs must contain only letters, digits, "
                "underscores or hyphens."
            )

        if type(quantity) is not int or quantity < 0:
            raise ValueError(
                "Initial quantities must be non-negative integers."
            )

        if product_id in initial_stock:
            raise ValueError(
                f"Duplicate product ID in seed: {product_id!r}."
            )

        initial_stock[product_id] = quantity

    return initial_stock, frozenset(faults)


class Inventory:
    """Manages products and reservations within a single process.

    The application must share the same instance across its requests.

    The initial stock is captured when the instance is created.
    reset() restores that copy and clears reservations without modifying
    or re-reading the initial seed file.
    """
    

    def __init__(self, seed_path: str | Path) -> None:
        self._initial_stock, self._faults = _load_seed(Path(seed_path))
        self._stock = self._initial_stock.copy()
        self._reservations: dict[str, _Reservation] = {}
        self._lock = Lock()

    def snapshot_state(self) -> dict[str, Any]:
        """Returns an independent snapshot of stock and reservations."""

        with self._lock:
            return {
                "products": [
                    {
                        "id": product_id,
                        "available_quantity": quantity,
                    }
                    for product_id, quantity in sorted(self._stock.items())
                ],
                "reservations": [
                    reservation.as_dict()
                    for reservation in self._reservations.values()
                ],
            }

    def reset(self) -> None:
        """Restores the initial stock and clears reservations."""

        with self._lock:
            self._stock = self._initial_stock.copy()
            self._reservations.clear()

    def get_product(
        self,
        product_id: str,
    ) -> dict[str, str | int]:
        """Returns the current stock without modifying the state."""

        with self._lock:
            if product_id not in self._stock:
                raise InventoryError("PRODUCT_NOT_FOUND")

            return {
                "id": product_id,
                "available_quantity": self._stock[product_id],
            }

    def create_reservation(
        self,
        payload: Any,
    ) -> dict[str, str | int]:
        """Validates the decoded payload and creates an active reservation.

        The HTTP layer must decode JSON without converting strings,
        booleans, or decimal numbers into integers, and must reject
        malformed JSON, duplicate keys, and non-finite constants.
        """

        if (
            not isinstance(payload, dict)
            or set(payload) != {"product_id", "quantity"}
        ):
            raise InventoryError("INVALID_REQUEST")

        product_id = payload["product_id"]
        quantity = payload["quantity"]

        if (
            not isinstance(product_id, str)
            or _IDENTIFIER.fullmatch(product_id) is None
        ):
            raise InventoryError("INVALID_REQUEST")

        # bool is a subclass of int; the exact-type check excludes it.
        if type(quantity) is not int or quantity <= 0:
            raise InventoryError("INVALID_REQUEST")

        with self._lock:
            if product_id not in self._stock:
                raise InventoryError("PRODUCT_NOT_FOUND")

            available_quantity = self._stock[product_id]

            if quantity > available_quantity:
                raise InventoryError("INSUFFICIENT_STOCK")

            reservation_id = f"reservation-{uuid4().hex}"

            while reservation_id in self._reservations:
                reservation_id = f"reservation-{uuid4().hex}"

            reservation = _Reservation(
                id=reservation_id,
                product_id=product_id,
                quantity=quantity,
            )
            remaining_quantity = available_quantity - quantity

            self._reservations[reservation_id] = reservation
            self._stock[product_id] = remaining_quantity

            return reservation.as_dict()

    def get_reservation(
        self,
        reservation_id: str,
    ) -> dict[str, str | int]:
        """Returns a reservation, including one that has already been cancelled."""

        with self._lock:
            reservation = self._reservations.get(reservation_id)

            if reservation is None:
                raise InventoryError("RESERVATION_NOT_FOUND")

            return reservation.as_dict()

    def cancel_reservation(
        self,
        reservation_id: str,
    ) -> dict[str, str | int]:
        """Cancels the reservation and restores its units exactly once."""

        with self._lock:
            reservation = self._reservations.get(reservation_id)

            if reservation is None:
                raise InventoryError("RESERVATION_NOT_FOUND")

            if reservation.status == "CANCELLED":
                if "cancel_restores_twice" in self._faults:
                    self._stock[reservation.product_id] += reservation.quantity

                return reservation.as_dict()

            cancelled = replace(reservation, status="CANCELLED")
            restored_quantity = (
                self._stock[reservation.product_id]
                + reservation.quantity
            )

            self._reservations[reservation_id] = cancelled
            self._stock[reservation.product_id] = restored_quantity

            return cancelled.as_dict()