"""Número e total da parcela de um lançamento de cartão.

Usa o que a API informa (creditCardMetadata). Se faltar, tenta o texto da descrição, só
em cartão: 'PARCELA 3/10', 'PARC 03/10' ou 'NOME 1/2' no fim da descrição. Recusa o que
não pode ser parcela (número maior que o total, total acima de 24), o que descarta datas
como 31/10. Uma data como 03/10 no fim da descrição ainda pode ser lida como parcela: por
isso o painel diz de onde veio cada parcela.
"""

import re

_COM_PALAVRA = re.compile(r"\bparc(?:ela)?\.?\s*(\d{1,2})\s*/\s*(\d{1,2})\b", re.IGNORECASE)
_NO_FIM = re.compile(r"(?:^|[\s\-(])(\d{1,2})\s*/\s*(\d{1,2})\s*\)?\s*$")


def parcela_de(linha) -> tuple[int, int, str] | None:
    """Devolve (número, total, origem) ou None. Origem: 'banco' ou 'descrição'."""
    numero, total = linha["parcela_numero"], linha["parcela_total"]
    if numero and total and total > 1:
        return int(numero), int(total), "banco"
    if linha["conta_tipo"] != "CREDIT":
        return None
    descricao = linha["descricao"] or ""
    achado = _COM_PALAVRA.search(descricao) or _NO_FIM.search(descricao)
    if not achado:
        return None
    numero, total = int(achado.group(1)), int(achado.group(2))
    if 2 <= total <= 24 and 1 <= numero <= total:
        return numero, total, "descrição"
    return None
