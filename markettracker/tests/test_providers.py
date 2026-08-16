from types import SimpleNamespace
from unittest.mock import MagicMock

from markettracker import providers


class OperationStub:
    def __init__(self, result):
        self._result = result
        self.result_kwargs = None

    def result(self, **kwargs):
        self.result_kwargs = kwargs
        return self._result

    def results(self, **kwargs):
        self.result_kwargs = kwargs
        return self._result


def test_character_contracts_passes_aa_token_and_uses_django_esi_pagination(monkeypatch):
    operation = OperationStub(
        [SimpleNamespace(contract_id=42, status="outstanding")]
    )
    endpoint = MagicMock(return_value=operation)
    monkeypatch.setattr(
        providers,
        "esi",
        SimpleNamespace(
            client=SimpleNamespace(
                Contracts=SimpleNamespace(
                    GetCharactersCharacterIdContracts=endpoint
                )
            )
        ),
    )
    token = object()

    result = providers.get_character_contracts(9001, token, force_refresh=True)

    endpoint.assert_called_once_with(character_id=9001, token=token)
    assert operation.result_kwargs == {"force_refresh": True}
    assert result == [{"contract_id": 42, "status": "outstanding"}]


def test_region_orders_uses_django_esi_results(monkeypatch):
    operation = OperationStub([SimpleNamespace(order_id=7, price=12.5)])
    endpoint = MagicMock(return_value=operation)
    monkeypatch.setattr(
        providers,
        "esi",
        SimpleNamespace(
            client=SimpleNamespace(
                Market=SimpleNamespace(GetMarketsRegionIdOrders=endpoint)
            )
        ),
    )

    result = providers.get_region_orders(10000002, 34, order_type="sell")

    endpoint.assert_called_once_with(
        order_type="sell", region_id=10000002, type_id=34
    )
    assert operation.result_kwargs == {}
    assert result == [{"order_id": 7, "price": 12.5}]


def test_structure_orders_passes_token_object(monkeypatch):
    operation = OperationStub([SimpleNamespace(order_id=8, type_id=35)])
    endpoint = MagicMock(return_value=operation)
    monkeypatch.setattr(
        providers,
        "esi",
        SimpleNamespace(
            client=SimpleNamespace(
                Market=SimpleNamespace(GetMarketsStructuresStructureId=endpoint)
            )
        ),
    )
    token = object()

    result = providers.get_structure_orders(1020000000000, token)

    endpoint.assert_called_once_with(structure_id=1020000000000, token=token)
    assert result == [{"order_id": 8, "type_id": 35}]
