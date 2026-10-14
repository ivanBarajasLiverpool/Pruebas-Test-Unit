"""Módulo de cálculo para operaciones de suma inteligente."""

from typing import Union


def _convertir_a_numero(valor: Union[int, float, str]) -> Union[int, float]:
    """Intenta convertir un valor numérico o string a int o float.

    Lanza ValueError si no es posible la conversión numérica.
    """
    if isinstance(valor, bool):
        # Evitar tratar booleanos True/False como enteros 1/0
        raise ValueError("Los booleanos no se procesan como números.")
    if isinstance(valor, (int, float)):
        return valor
    if isinstance(valor, str):
        cadena = valor.strip()
        try:
            return int(cadena)
        except ValueError:
            return float(cadena)
    raise ValueError(f"Tipo no soportado para conversión numérica: {type(valor)}")


def suma(a: Union[int, float, str], b: Union[int, float, str]) -> Union[int, float, str]:
    """Retorna el resultado de la suma entre a y b.

    - Si ambos valores son o representan números (ej. 2 y 3, "2" y "3", 2 y "3"),
      retorna la suma aritmética (int o float).
    - Si alguno o ambos son texto no numérico (ej. "hola" y "mundo", "total: " y 10),
      retorna la concatenación como string.

    Args:
        a: Primer número o string.
        b: Segundo número o string.

    Returns:
        Union[int, float, str]: Resultado de la suma aritmética o concatenación.

    Ejemplos:
        >>> suma(2, 3)
        5
        >>> suma("2", "3")
        5
        >>> suma(2, "3")
        5
        >>> suma("2.5", 1.5)
        4.0
        >>> suma("hola", "mundo")
        'holamundo'
        >>> suma("total: ", 10)
        'total: 10'
    """
    try:
        num_a = _convertir_a_numero(a)
        num_b = _convertir_a_numero(b)
        return num_a + num_b
    except (ValueError, TypeError):
        return f"{a}{b}"


