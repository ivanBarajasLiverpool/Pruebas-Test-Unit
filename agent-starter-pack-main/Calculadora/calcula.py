"""Módulo para el cálculo de fechas."""

from datetime import date, timedelta
from typing import Optional


def obtener_fecha_manana(fecha_base: Optional[date] = None) -> str:
    """Calcula y retorna la fecha del día de mañana en formato dd-mm-aaaa.

    Cada vez que se manda a llamar sin argumentos, toma la fecha actual del sistema
    y calcula el día siguiente.

    Args:
        fecha_base: Fecha de referencia opcional (por defecto utiliza la fecha de hoy).

    Returns:
        str: Fecha de mañana formateada como 'dd-mm-aaaa'.

    Ejemplo:
        >>> obtener_fecha_manana()
        '15-10-2026'
    """
    if fecha_base is None:
        fecha_base = date.today()

    manana = fecha_base + timedelta(days=1)
    return manana.strftime("%d-%m-%Y")


# Alias para mayor facilidad de uso
calcular_fecha_manana = obtener_fecha_manana
fecha_manana = obtener_fecha_manana


if __name__ == "__main__":
    resultado = obtener_fecha_manana()
    print(f"Fecha de mañana (dd-mm-aaaa): {resultado}")
