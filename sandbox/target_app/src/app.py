import argparse
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .inventory import Inventory, InventoryError


_DEFAULT_SEED_FILE = (
    Path(__file__).resolve().parents[1]
    / "seed"
    / "products.json"
)

_ERROR_STATUS = {
    "INVALID_REQUEST": 422,
    "PRODUCT_NOT_FOUND": 404,
    "INSUFFICIENT_STOCK": 409,
    "RESERVATION_NOT_FOUND": 404,
}


def _unique_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Rejects duplicate keys in any object in the document."""
    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}.")

        result[key] = value

    return result


def _reject_constant(value: str) -> Any:
    """Rejects NaN, Infinity, and -Infinity."""
    raise ValueError(f"Non-finite JSON number: {value}.")


def _parse_integer(value: str) -> int:
    """Preserves large integers without converting them to floating point."""

    return int(Decimal(value))


def _decode_json(body: bytes) -> Any:
    """Decodes JSON without type coercion or numeric loss."""

    return json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_keys,
        parse_constant=_reject_constant,
        parse_int=_parse_integer,
        parse_float=Decimal,
    )


def _error_response(
    code: str,
    status_code: int,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Builds the common error response format."""

    return JSONResponse(
        content={"error": {"code": code}},
        status_code=status_code,
        headers=headers,
    )


async def _handle_inventory_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Converts business errors into their HTTP codes."""

    if not isinstance(exc, InventoryError):
        raise exc

    return _error_response(
        code=exc.code,
        status_code=_ERROR_STATUS[exc.code],
    )


async def _handle_http_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Handles missing routes and unsupported methods."""

    if not isinstance(exc, HTTPException):
        raise exc

    code = {
        404: "ROUTE_NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }.get(exc.status_code, "HTTP_ERROR")

    return _error_response(
        code=code,
        status_code=exc.status_code,
        headers=exc.headers,
    )


async def _handle_unexpected_error(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Returns a generic error; the server logs the exception."""

    return _error_response(
        code="INTERNAL_ERROR",
        status_code=500,
    )


def create_app(
    seed_path: str | Path = _DEFAULT_SEED_FILE,
) -> Starlette:
    """Builds an application with its own Inventory instance."""
    inventory = Inventory(seed_path)

    async def get_product(request: Request) -> JSONResponse:
        product = inventory.get_product(
            request.path_params["product_id"]
        )

        return JSONResponse(product)

    async def create_reservation(request: Request) -> JSONResponse:
        body = await request.body()

        try:
            payload = _decode_json(body)

        except (
            UnicodeError,
            ValueError,
            DecimalException,
            RecursionError,
        ):
            return _error_response(
                code="INVALID_JSON",
                status_code=400,
            )

        reservation = inventory.create_reservation(payload)

        return JSONResponse(
            content=reservation,
            status_code=201,
        )

    async def get_reservation(request: Request) -> JSONResponse:
        reservation = inventory.get_reservation(
            request.path_params["reservation_id"]
        )

        return JSONResponse(reservation)

    async def cancel_reservation(request: Request) -> JSONResponse:
        # The contract requires ignoring any received body.
        reservation = inventory.cancel_reservation(
            request.path_params["reservation_id"]
        )

        return JSONResponse(reservation)

    application = Starlette(
        debug=False,
        routes=[
            Route(
                "/products/{product_id}",
                endpoint=get_product,
                methods=["GET"],
                name="getProduct",
            ),
            Route(
                "/reservations",
                endpoint=create_reservation,
                methods=["POST"],
                name="createReservation",
            ),
            Route(
                "/reservations/{reservation_id}",
                endpoint=get_reservation,
                methods=["GET"],
                name="getReservation",
            ),
            Route(
                "/reservations/{reservation_id}/cancel",
                endpoint=cancel_reservation,
                methods=["POST"],
                name="cancelReservation",
            ),
        ],
        exception_handlers={
            InventoryError: _handle_inventory_error,
            HTTPException: _handle_http_error,
            Exception: _handle_unexpected_error,
        },
    )

    application.router.redirect_slashes = False
    application.state.inventory = inventory

    return application


def main(argv: Sequence[str] | None = None) -> int:
    """Starts the sandbox with explicit parameters."""
    parser = argparse.ArgumentParser(
        description="Reservation application server."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address where the server will listen.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="HTTP port for the server.",
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=_DEFAULT_SEED_FILE,
        help="JSON file with the initial inventory.",
    )

    args = parser.parse_args(argv)

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")

    try:
        application = create_app(args.seed_file)

    except (OSError, ValueError) as error:
        parser.error(
            f"Could not load the initial inventory: {error}"
        )

    try:
        uvicorn.run(
            application,
            host=args.host,
            port=args.port,
            workers=1,
            reload=False,
            proxy_headers=False,
            forwarded_allow_ips="",
            log_level="info",
        )

    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())