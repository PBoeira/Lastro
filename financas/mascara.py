"""Mascaramento para tudo que é gravado, aparece na tela, em logs ou relatórios."""

import re
import unicodedata

# Caracteres de controle (C0, C1), de largura zero e de direção de texto: podem
# manipular o terminal (sequências ANSI/OSC) ou esconder trechos de texto.
_CONTROLE = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
# 5 ou mais dígitos, aceitando até 3 separadores entre eles: espaços (inclusive sem
# quebra), ponto, sublinhado, hífen, traços longos e sinal de menos. Pega conta, CPF,
# cartão e telefone. Vírgula fica de fora para preservar valores como 1.234,56.
# Barra fica de fora para não estragar datas como 31/10/2026 e parcelas como 03/10.
_SEPARADOR = r"[\s._\-‐-―−]"
_SEQUENCIA_NUMERICA = re.compile(rf"\d(?:{_SEPARADOR}{{0,3}}\d){{4,}}")


def final(valor, visiveis: int = 4) -> str:
    """Mostra só os últimos caracteres: 'a1b2c3d4-...-9f0e' vira '…9f0e'."""
    if not valor:
        return "?"
    texto_ = str(valor)
    if len(texto_) <= visiveis:
        return "•" * len(texto_)
    return "…" + texto_[-visiveis:]


def numero(valor, visiveis: int = 4) -> str:
    """Número de conta ou cartão: mantém só os últimos dígitos, '12345-6' vira '••••3456'."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) < visiveis:
        return "••••"
    return "••••" + digitos[-visiveis:]


def texto(valor, limite: int | None = None) -> str | None:
    """Texto seguro para gravar ou exibir: sem caracteres de controle e com números
    longos reduzidos aos 4 últimos dígitos. Aplicar duas vezes dá o mesmo resultado."""
    if valor is None:
        return None
    limpo = unicodedata.normalize("NFKC", str(valor))  # dígitos e espaços "exóticos" viram comuns
    # caracteres invisíveis de formatação (categoria Cf: largura zero, hífen suave, separadores
    # invisíveis) e o combinador U+034F saem antes de mascarar, para não esconder dígitos da máscara
    limpo = "".join(c for c in limpo if unicodedata.category(c) != "Cf" and c != "͏")
    limpo = _CONTROLE.sub(" ", limpo)
    limpo = _SEQUENCIA_NUMERICA.sub(lambda m: "••••" + re.sub(r"\D", "", m.group(0))[-4:], limpo)
    if limite is not None and len(limpo) > limite:
        limpo = limpo[:limite] + "…"
    return limpo
