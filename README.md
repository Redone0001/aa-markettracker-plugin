# AA MarketTracker (AllianceAuth plugin)

Plugin do **AllianceAuth** do śledzenia rynku EVE (zlecenia, kontrakty, stany), z integracją ESI i opcjonalnymi powiadomieniami na Discord.  
**Nie wymaga** appki `structures` – używa liczbowego `location_id` (region/structure) do zapytań ESI.

## Funkcje
- Lista śledzonych itemów + progi kolorów (yellow/red).
- Snapshoty zleceń rynkowych (region/structure) i kontraktów.
- Dostawy (deliveries) + proste zarządzanie.
- Webhooki Discord (ping grupy / embed).
- Taski Celery do okresowego odświeżania.

## Wymagania
- Python 3.11
- AllianceAuth `>=4.3.1,<5` (testowane na 4.8.0)
- Django `>=4.2,<5`
- Celery `>=5.2,<6` + django-celery-beat
- `eveuniverse` (regiony etc.)
- `requests`

## Instalacja
```bash
pip install aa-markettracker-plugin