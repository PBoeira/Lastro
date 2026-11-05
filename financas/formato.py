"""Formatação de valores em reais."""


def reais(centavos: int | None) -> str:
    if centavos is None:
        return "?"
    sinal = "-" if centavos < 0 else ""
    inteiro, resto = divmod(abs(int(centavos)), 100)
    milhar = f"{inteiro:,}".replace(",", ".")
    return f"{sinal}R$ {milhar},{resto:02d}"
