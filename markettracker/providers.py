"""Typed ESI access through django-esi's AA 5 OpenAPI client."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from esi.models import Token
from esi.openapi_clients import ESIClientProvider

from . import __esi_compatibility_date__, __title__, __url__, __version__

esi = ESIClientProvider(
    ua_appname=__title__,
    ua_version=__version__,
    ua_url=__url__,
    compatibility_date=__esi_compatibility_date__,
    operations=[
        "GetCharactersCharacterIdContracts",
        "GetCharactersCharacterIdContractsContractIdItems",
        "GetCharactersCharacterIdOrders",
        "GetMarketsRegionIdHistory",
        "GetMarketsRegionIdOrders",
        "GetMarketsStructuresStructureId",
        "GetUniverseStructuresStructureId",
        "GetUniverseTypesTypeId",
        "PostUniverseNames",
    ],
)


def _plain(value: Any) -> Any:
    """Convert generated ESI response models to JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return _plain(value.dict())
    if hasattr(value, "__dict__"):
        return {
            key: _plain(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def get_character_contracts(
    character_id: int, token: Token, *, force_refresh: bool = False
) -> list[dict]:
    result = esi.client.Contracts.GetCharactersCharacterIdContracts(
        character_id=int(character_id), token=token
    ).results(force_refresh=force_refresh)
    return _plain(result)


def get_character_contract_items(
    character_id: int, contract_id: int, token: Token, *, force_refresh: bool = False
) -> list[dict]:
    result = esi.client.Contracts.GetCharactersCharacterIdContractsContractIdItems(
        character_id=int(character_id),
        contract_id=int(contract_id),
        token=token,
    ).result(force_refresh=force_refresh)
    return _plain(result)


def get_character_orders(character_id: int, token: Token) -> list[dict]:
    result = esi.client.Market.GetCharactersCharacterIdOrders(
        character_id=int(character_id), token=token
    ).result()
    return _plain(result)


def get_market_history(region_id: int, type_id: int) -> list[dict]:
    result = esi.client.Market.GetMarketsRegionIdHistory(
        region_id=int(region_id), type_id=int(type_id)
    ).result()
    return _plain(result)


def get_region_orders(
    region_id: int, type_id: int, *, order_type: str = "sell"
) -> list[dict]:
    result = esi.client.Market.GetMarketsRegionIdOrders(
        order_type=order_type,
        region_id=int(region_id),
        type_id=int(type_id),
    ).results()
    return _plain(result)


def get_structure_orders(structure_id: int, token: Token) -> list[dict]:
    result = esi.client.Market.GetMarketsStructuresStructureId(
        structure_id=int(structure_id), token=token
    ).results()
    return _plain(result)


def get_structure_info(structure_id: int, token: Token) -> dict:
    result = esi.client.Universe.GetUniverseStructuresStructureId(
        structure_id=int(structure_id), token=token
    ).result()
    return _plain(result)


def get_type_info(type_id: int) -> dict:
    result = esi.client.Universe.GetUniverseTypesTypeId(type_id=int(type_id)).result()
    return _plain(result)


def post_universe_names(ids: list[int]) -> list[dict]:
    result = esi.client.Universe.PostUniverseNames(body=[int(value) for value in ids]).result(
        store_cache=False
    )
    return _plain(result)
