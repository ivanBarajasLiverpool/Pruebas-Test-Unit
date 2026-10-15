from datetime import date

import pytest
from Calculadora.calcula import (
    calcular_fecha_manana,
    fecha_manana,
    obtener_fecha_manana,
)


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (date(2026, 10, 15), "16-10-2026"),
        (date(2026, 4, 30), "01-05-2026"),
        (date(2026, 12, 31), "01-01-2027"),
        (date(2024, 2, 28), "29-02-2024"),
        (date(2024, 2, 29), "01-03-2024"),
        (date(2026, 2, 28), "01-03-2026"),
        (date(2000, 2, 28), "29-02-2000"),
        (date(2100, 2, 28), "01-03-2100"),
    ],
)
def test_tomorrow_at_calendar_boundaries(base, expected):
    assert obtener_fecha_manana(base) == expected


def test_default_date_is_read_on_each_call(monkeypatch):
    class Clock:
        current = date(2026, 12, 31)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr("Calculadora.calcula.date", Clock)
    assert obtener_fecha_manana() == "01-01-2027"
    Clock.current = date(2027, 1, 1)
    assert obtener_fecha_manana() == "02-01-2027"


@pytest.mark.parametrize("alias", [calcular_fecha_manana, fecha_manana])
def test_aliases_preserve_the_public_contract(alias):
    assert alias(date(2024, 2, 28)) == "29-02-2024"
