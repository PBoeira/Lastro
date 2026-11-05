"""Nome da instituição de cada item.

Nos itens do MeuPluggy o conector se chama sempre 'MeuPluggy'. O banco de origem
é deduzido, só em memória:
  1. do código COMPE da conta, que é o primeiro trecho do transferNumber
     ('260/0001/...' é Nubank, '001/...' é Banco do Brasil). O resto do número
     (agência e conta) é descartado e nunca é gravado nem exibido;
  2. se não houver, do nome do produto (ex.: 'Ourocard' é do Banco do Brasil).
"""

import re

from . import mascara

_COMPE = {
    "001": "Banco do Brasil", "260": "Nubank", "341": "Itaú", "237": "Bradesco", "033": "Santander",
    "104": "Caixa", "077": "Inter", "336": "C6 Bank", "290": "PagBank", "380": "PicPay",
    "323": "Mercado Pago", "208": "BTG Pactual", "102": "XP", "756": "Sicoob", "748": "Sicredi",
    "655": "Neon", "212": "Banco Original", "422": "Safra", "121": "Agibank",
}
_PALAVRAS = (
    (re.compile(r"(?i)nubank|nu pagamentos|nu financeira"), "Nubank"),
    (re.compile(r"(?i)banco do brasil|ourocard|\bbb\b"), "Banco do Brasil"),
)
_GENERICOS = {"", "meupluggy", "meu pluggy"}
_SUFIXO = " (via MeuPluggy)"


def nome(item: dict, contas: list[dict]) -> str:
    conector = str((item.get("connector") or {}).get("name") or "")
    if conector.strip().lower() not in _GENERICOS:
        return mascara.texto(conector, 60)  # conexão direta ou Sandbox: o conector já é a instituição
    for conta in contas:
        trecho = str((conta.get("bankData") or {}).get("transferNumber") or "").split("/", 1)[0].strip()
        if re.fullmatch(r"\d{1,3}", trecho):
            codigo = trecho.zfill(3)
            return _COMPE.get(codigo, f"banco {codigo}") + _SUFIXO
    for conta in contas:
        texto = f"{conta.get('marketingName') or ''} {conta.get('name') or ''}"
        for padrao, instituicao in _PALAVRAS:
            if padrao.search(texto):
                return instituicao + _SUFIXO
    return "MeuPluggy (instituição não identificada)"
