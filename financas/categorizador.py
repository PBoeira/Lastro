"""Comando 'categorizar': dá a cada lançamento uma categoria e uma natureza
(despesa, receita ou neutro) e gera a página local de revisão.

Ordem de decisão (a primeira que resolver vence):
  1. ajustes.local.toml: decisão manual por lançamento;
  2. [[regra]]: primeiro as estruturais do regras.toml (par entre contas próprias, movimento do seu
     CPF, Pix no crédito, pagamento repetido, compra estornada, parcelamento cancelado), que usam fatos calculados antes
     sobre todos os lançamentos; depois as pessoais (regras.local.toml); depois o resto das
     genéricas (regras.toml);
  3. regras.toml, [categoria_api]: categoria da Pluggy, quando vier;
  4. regras.toml, [[regra_final]]: Pix, TED e boletos genéricos (duvidosos);
  5. "Sem categoria", marcado como duvidoso.

Neutros (transferência entre contas próprias, pagamento de fatura, poupança e
resgate) não entram em receitas nem em despesas, o que evita contar em dobro.

A saída na tela mostra só contagens e totais. Os lançamentos em si aparecem
apenas na página local de revisão.
"""

import hashlib
import re
import sys
import tomllib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config, db, formato, mascara, parcelas, revisao

NATUREZAS = ("despesa", "receita", "neutro")
SEM_CATEGORIA = {"saida": "Sem categoria (saída)", "entrada": "Sem categoria (entrada)"}
_OPERACOES_TRANSFERENCIA = {"PIX", "TED", "DOC", "TRANSFERENCIA_MESMA_INSTITUICAO"}
_TEXTO_TRANSFERENCIA = re.compile(r"\b(pix|ted|doc|transf)", re.IGNORECASE)
_JANELA_PAR_DIAS = 2
_CAMPOS_TEXTO = ("descricao", "instituicao", "categoria_api", "documento")
_CAMPOS_BOOL = ("mesmo_titular", "par_proprio", "parcelado", "pessoa", "pix_credito_conta", "pix_credito_cartao",
                "pagamento_duplicado", "compra_estornada", "serie_cancelada")
_PALAVRAS_VAZIAS = {"pix", "transferencia", "enviada", "enviado", "pelo", "para", "credito", "compra", "pagamento",
                    "com", "dos", "das", "ltda", "parcela", "parc", "no", "na", "de", "da", "do", "via", "nubank",
                    "estorno", "devolucao", "cancelamento", "parcelamento", "reembolso", "cashback"}
_TEXTO_PAGAMENTO = re.compile(r"\b(pagamento|pagto|pgto)\b", re.IGNORECASE)
_DIAS_DUPLICADO = 5
_CONSOLIDADO = re.compile(r"^\s*pagamento recebido", re.IGNORECASE)
_INDIVIDUAL = re.compile(r"\bpgto\b", re.IGNORECASE)
_DIAS_CONSOLIDADO = 40
_DIAS_CANCELAMENTO = 150
_TEXTO_CANCELAMENTO = re.compile(r"estorn|cancel|devol|reembols|parcelamento")
_DIAS_ESTORNO = 120
_CAMPOS = set(_CAMPOS_TEXTO) | set(_CAMPOS_BOOL) | {"conta", "sentido", "operacao", "contraparte"}


class ErroRegras(ValueError):
    pass


def sem_acento(texto: str | None) -> str:
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def normalizar(texto: str | None) -> str:
    return sem_acento(texto).lower()


def id_curto(transacao_id: str) -> str:
    return transacao_id.replace("-", "")[:10]


# ---------------------------------------------------------------- regras

@dataclass
class Regra:
    nome: str
    categoria: str
    duvidoso: bool
    condicoes: dict = field(default_factory=dict)
    local: bool = False


_FLAGS_ESTRUTURAIS = {"par_proprio", "mesmo_titular", "pix_credito_conta", "pix_credito_cartao",
                      "pagamento_duplicado", "compra_estornada", "serie_cancelada"}


@dataclass
class Regras:
    categorias: dict
    regras: list
    finais: list
    mapa_api: list  # [(padrão, categoria ou {saida, entrada})]


# Grupo com alternativa ou repetição dentro e repetido de novo, como (a|aa)+ ou (a+)*: pode levar
# um tempo exponencial e travar o comando. Opcional (?) e grupos sem repetição continuam valendo.
_REPETICAO_ARRISCADA = re.compile(r"\((?:[^()\\]|\\.)*(?:[|+*]|\{\d)(?:[^()\\]|\\.)*\)\s*[+*{]")
# Também arriscados: grupo aninhado repetido, como ((a|aa))+, e três ou mais repetições sem limite
# seguidas, como a*a*a*b. Padrão longo demais é recusado.
_GRUPO_ANINHADO_REPETIDO = re.compile(r"\)\s*\)\s*[+*{]")
_REPETICOES_SEGUIDAS = re.compile(r"(?:(?:\\.|\[[^\]]*\]|[^\\()|\[\]])[*+]\??){3,}")
_LIMITE_PADRAO = 400
_LIMITE_TEXTO = 500


def _expressao(valor, onde: str) -> re.Pattern:
    texto = str(valor)
    if len(texto) > _LIMITE_PADRAO:
        raise ErroRegras(f"{onde}: expressão com mais de {_LIMITE_PADRAO} caracteres; divida em mais de uma regra.")
    if (_REPETICAO_ARRISCADA.search(texto) or _GRUPO_ANINHADO_REPETIDO.search(texto)
            or _REPETICOES_SEGUIDAS.search(texto)):
        raise ErroRegras(f"{onde}: grupo repetido com alternativa ou repetição dentro (ex.: (a|b)+); "
                         "isso pode travar o programa. Reescreva sem repetir o grupo.")
    try:
        # só tira acentos do padrão (minúsculas mudariam \B, \D, \S, \W); IGNORECASE cuida do resto
        return re.compile(sem_acento(str(valor)), re.IGNORECASE)
    except re.error as erro:
        raise ErroRegras(f"{onde}: expressão regular inválida ({erro})") from None


def _compilar(bruta: dict, onde: str, categorias: dict) -> Regra:
    if not isinstance(bruta, dict):
        raise ErroRegras(f"{onde}: formato inválido")
    categoria = bruta.get("categoria")
    if categoria not in categorias:
        raise ErroRegras(f"{onde}: categoria '{categoria}' não existe em [categorias]")
    desconhecidos = set(bruta) - _CAMPOS - {"categoria", "duvidoso", "nome"}
    if desconhecidos:
        raise ErroRegras(f"{onde}: campo(s) desconhecido(s): {', '.join(sorted(desconhecidos))}")
    condicoes: dict = {}
    for campo in _CAMPOS_TEXTO:
        if campo in bruta:
            condicoes[campo] = _expressao(bruta[campo], f"{onde}, {campo}")
    for campo in _CAMPOS_BOOL:
        if campo in bruta:
            if not isinstance(bruta[campo], bool):
                raise ErroRegras(f"{onde}: {campo} precisa ser true ou false")
            condicoes[campo] = bruta[campo]
    for campo, aceitos in (("conta", ("BANK", "CREDIT")), ("sentido", ("entrada", "saida")),
                           ("contraparte", ("CPF", "CNPJ"))):
        if campo in bruta:
            if bruta[campo] not in aceitos:
                raise ErroRegras(f"{onde}: {campo} precisa ser {' ou '.join(aceitos)}")
            condicoes[campo] = bruta[campo]
    if "operacao" in bruta:
        operacoes = bruta["operacao"]
        if not isinstance(operacoes, list) or not all(isinstance(o, str) for o in operacoes):
            raise ErroRegras(f"{onde}: operacao precisa ser uma lista de textos")
        condicoes["operacao"] = {o.upper() for o in operacoes}
    if not condicoes:
        raise ErroRegras(f"{onde}: regra sem nenhuma condição")
    return Regra(str(bruta.get("nome") or onde), categoria, bool(bruta.get("duvidoso", False)), condicoes)


def fotografar_regras(caminhos=None) -> dict:
    """Lê uma vez só os arquivos que decidem as categorias (bytes, ou None se o arquivo não existe).
    A mesma leitura serve para a impressão digital e para as regras aplicadas: trocar um arquivo
    durante a execução não muda o que é aplicado."""
    caminhos = caminhos or (config.CAMINHO_REGRAS, config.CAMINHO_REGRAS_LOCAL, config.CAMINHO_AJUSTES)
    return {caminho: (caminho.read_bytes() if caminho.exists() else None) for caminho in caminhos}


def _conteudo(caminho: Path, conteudos: dict | None) -> bytes | None:
    """Bytes da fotografia, se houver; senão, lidos do disco agora (None se o arquivo não existe)."""
    if conteudos is not None and caminho in conteudos:
        return conteudos[caminho]
    return caminho.read_bytes() if caminho.exists() else None


def _ler_toml(caminho: Path, conteudo: bytes) -> dict:
    try:
        return tomllib.loads(conteudo.decode("utf-8"))
    except tomllib.TOMLDecodeError as erro:
        raise ErroRegras(f"{caminho.name}: erro de formato ({erro})") from None


def carregar_regras(caminho: Path = config.CAMINHO_REGRAS,
                    caminho_local: Path | None = config.CAMINHO_REGRAS_LOCAL, conteudos: dict | None = None) -> Regras:
    """Regras genéricas (regras.toml, vão para o repositório) mais as pessoais (regras.local.toml,
    fora do repositório). Ordem em [[regra]]: as genéricas estruturais (pares entre contas, Pix no
    crédito, pagamento repetido, compra estornada), depois as pessoais, depois o resto das
    genéricas; em [[regra_final]] e [categoria_api], as pessoais primeiro. As pessoais podem criar
    categorias novas. caminho_local=None ignora o arquivo pessoal (os testes fazem isso).
    conteudos: a fotografia de fotografar_regras(); sem ela, os arquivos são lidos agora."""
    principal = _conteudo(caminho, conteudos)
    if principal is None:
        raise ErroRegras(f"{caminho.name}: arquivo não encontrado")
    fontes = [("", caminho, _ler_toml(caminho, principal))]
    local = _conteudo(caminho_local, conteudos) if caminho_local is not None else None
    if local is not None:
        fontes.insert(0, ("local: ", caminho_local, _ler_toml(caminho_local, local)))
    categorias: dict = {}
    for _, origem, dados in reversed(fontes):
        for nome, natureza in (dados.get("categorias") or {}).items():
            if natureza not in NATUREZAS:
                raise ErroRegras(f"{origem.name} [categorias]: '{nome}' precisa ser despesa, receita ou neutro")
            categorias[nome] = natureza
    for obrigatoria, natureza in ((SEM_CATEGORIA["saida"], "despesa"), (SEM_CATEGORIA["entrada"], "receita")):
        categorias.setdefault(obrigatoria, natureza)
    regras, finais, mapa_api = [], [], []
    for prefixo, origem, dados in fontes:
        for secao, destino_lista in (("regra", regras), ("regra_final", finais)):
            for numero, bruta in enumerate(dados.get(secao) or [], 1):
                regra = _compilar(bruta, f"{origem.name} [[{secao}]] nº {numero}", categorias)
                regra.nome = prefixo + regra.nome
                regra.local = bool(prefixo)
                destino_lista.append(regra)
        for padrao, destino in (dados.get("categoria_api") or {}).items():
            onde = f"{origem.name} [categoria_api] '{padrao}'"
            alvos = destino.values() if isinstance(destino, dict) else [destino]
            if isinstance(destino, dict) and set(destino) - {"saida", "entrada"}:
                raise ErroRegras(f"{onde}: use só saida e entrada")
            for alvo in alvos:
                if alvo not in categorias:
                    raise ErroRegras(f"{onde}: categoria '{alvo}' não existe em [categorias]")
            mapa_api.append((_expressao(padrao, onde), destino))
    # Regras genéricas estruturais (pares entre contas, Pix no crédito, pagamento repetido, compra
    # estornada) vêm antes de tudo, inclusive das pessoais: nenhuma regra de loja passa por cima delas.
    estruturais = [r for r in regras if not r.local and set(r.condicoes) & _FLAGS_ESTRUTURAIS]
    regras = estruturais + [r for r in regras if r not in estruturais]
    return Regras(categorias, regras, finais, mapa_api)


def carregar_ajustes(categorias: dict, caminho: Path = config.CAMINHO_AJUSTES, conteudos: dict | None = None) -> dict:
    conteudo = _conteudo(caminho, conteudos)
    if conteudo is None:
        return {}
    dados = _ler_toml(caminho, conteudo)
    ajustes = dict(dados.get("lancamentos") or {})
    for chave, categoria in ajustes.items():
        if categoria not in categorias:
            raise ErroRegras(f"{caminho.name}: '{chave}' usa a categoria '{categoria}', que não existe em [categorias]")
    return ajustes


# ---------------------------------------------------------------- classificação

def _parece_transferencia(linha) -> bool:
    operacoes = {linha["tipo_operacao"], linha["metodo_pagamento"]} - {None}
    texto = " ".join(filter(None, (linha["descricao"], linha["descricao_bruta"])))
    return bool(operacoes & _OPERACOES_TRANSFERENCIA) or bool(_TEXTO_TRANSFERENCIA.search(sem_acento(texto)))


def encontrar_pares(linhas) -> set[str]:
    """Transferências entre duas contas conectadas: saída numa conta e entrada do mesmo valor,
    em até 2 dias, em outra conta corrente sua. Entram na busca as que têm o seu CPF do outro
    lado e as que parecem transferência sem CPF informado. As do seu CPF que ficarem sem par
    vêm (ou vão) de uma conta sua que não está conectada."""
    candidatas = [l for l in linhas if l["conta_tipo"] == "BANK" and (
        l["contraparte_mesmo_titular"] == 1
        or (l["contraparte_mesmo_titular"] is None and _parece_transferencia(l)))]
    entradas = defaultdict(list)
    for linha in candidatas:
        if linha["valor_centavos"] > 0:
            entradas[linha["valor_centavos"]].append(linha)
    usadas: set[str] = set()
    pares: set[str] = set()
    for saida in sorted((l for l in candidatas if l["valor_centavos"] < 0), key=lambda l: l["data_utc"]):
        dia_saida = date.fromisoformat(saida["dia"])
        melhor = None
        for entrada in entradas.get(-saida["valor_centavos"], []):
            if entrada["id"] in usadas or entrada["conta_id"] == saida["conta_id"]:
                continue
            distancia = abs((date.fromisoformat(entrada["dia"]) - dia_saida).days)
            if distancia <= _JANELA_PAR_DIAS and (melhor is None or distancia < melhor[0]):
                melhor = (distancia, entrada)
        if melhor:
            usadas.add(melhor[1]["id"])
            pares.update((saida["id"], melhor[1]["id"]))
    return pares


def mes_de_inicio(dia: str, numero: int) -> int:
    """Mês (como número ano*12+mês) em que caiu a parcela 1 de uma série."""
    ano, mes = int(dia[:4]), int(dia[5:7])
    return ano * 12 + (mes - 1) - (numero - 1)


def _nome_tokens(texto: str | None) -> set[str]:
    palavras = re.split(r"[^a-z]+", normalizar(texto))
    return {p for p in palavras if len(p) >= 3 and p not in _PALAVRAS_VAZIAS}


def encontrar_pix_credito(linhas) -> tuple[set[str], set[str]]:
    """Pix no crédito: cobrança no cartão com o nome da pessoa e, no mesmo dia (folga de 1),
    uma transferência enviada na conta da mesma instituição para a mesma pessoa, com valor
    igual ou até 25% menor (a diferença é a taxa). No Pix parcelado, compara com a parcela
    vezes o número de parcelas. Devolve (ids da conta, ids do cartão); no parcelado, todas
    as parcelas da série entram como cartão."""
    from .revisao import assinatura  # import tardio: revisao importa este módulo

    saidas_conta = sorted((l for l in linhas if l["conta_tipo"] == "BANK" and l["valor_centavos"] < 0
                           and l["contraparte_mesmo_titular"] != 1 and _parece_transferencia(l)),
                          key=lambda l: l["data_utc"])
    cobrancas = defaultdict(list)
    for linha in linhas:
        if linha["conta_tipo"] == "CREDIT" and linha["valor_centavos"] < 0:
            cobrancas[linha["instituicao"]].append(linha)
    usados_cartao: set[str] = set()
    ids_conta: set[str] = set()
    series: set[tuple] = set()
    for saida in saidas_conta:
        nome = _nome_tokens(saida["contraparte_nome"]) or _nome_tokens((saida["descricao"] or "").rsplit(" - ", 1)[-1])
        if not nome:
            continue
        exigidos = min(2, len(nome))
        dia_saida = date.fromisoformat(saida["dia"])
        valor_conta = -saida["valor_centavos"]
        melhor = None
        for cobranca in cobrancas.get(saida["instituicao"], []):
            if cobranca["id"] in usados_cartao:
                continue
            if abs((date.fromisoformat(cobranca["dia"]) - dia_saida).days) > 1:
                continue
            if len(nome & _nome_tokens(f"{cobranca['descricao']} {cobranca['comerciante'] or ''}")) < exigidos:
                continue
            parcela = parcelas.parcela_de(cobranca)
            quantidade = parcela[1] if parcela else 1
            bruto = -cobranca["valor_centavos"] * quantidade
            if not (0.75 * bruto <= valor_conta <= bruto + 100):
                continue
            if melhor is None or abs(bruto - valor_conta) < melhor[0]:
                melhor = (abs(bruto - valor_conta), cobranca, parcela)
        if melhor:
            _, cobranca, parcela = melhor
            usados_cartao.add(cobranca["id"])
            ids_conta.add(saida["id"])
            if parcela:
                inicio = mes_de_inicio(cobranca["dia"], parcela[0])
                series.add((cobranca["conta_id"], assinatura(cobranca["descricao"]), parcela[1], inicio))
    ids_cartao = set(usados_cartao)
    for linha in linhas:
        parcela = parcelas.parcela_de(linha) if linha["conta_tipo"] == "CREDIT" else None
        if parcela:
            inicio = mes_de_inicio(linha["dia"], parcela[0])
            if (linha["conta_id"], assinatura(linha["descricao"]), parcela[1], inicio) in series:
                ids_cartao.add(linha["id"])
    return ids_conta, ids_cartao


def _e_pagamento_cartao(linha) -> bool:
    return linha["conta_tipo"] == "CREDIT" and linha["valor_centavos"] > 0 and (
        linha["tipo_operacao"] == "PAGAMENTO_FATURA" or bool(_TEXTO_PAGAMENTO.search(sem_acento(linha["descricao"]))))


def _dias(a, b) -> int:
    return abs((date.fromisoformat(a["dia"]) - date.fromisoformat(b["dia"])).days)


def encontrar_pagamentos_duplicados(linhas) -> set[str]:
    """O mesmo pagamento de fatura duas vezes no cartão: um informado pelo banco e outro montado
    pela Pluggy a partir da fatura. Mesmo valor, até 5 dias de distância e operação ou descrição
    diferentes. Dois pagamentos iguais com a mesma descrição (ex.: dois pagamentos em espécie de
    mesmo valor) continuam valendo os dois. Marca como duplicado o montado pela Pluggy."""
    pagamentos = sorted((l for l in linhas if _e_pagamento_cartao(l)), key=lambda l: l["data_utc"])
    usados: set[str] = set()
    duplicados: set[str] = set()
    for i, primeiro in enumerate(pagamentos):
        if primeiro["id"] in usados:
            continue
        for segundo in pagamentos[i + 1:]:
            if _dias(primeiro, segundo) > _DIAS_DUPLICADO:
                break
            if segundo["id"] in usados or segundo["conta_id"] != primeiro["conta_id"]:
                continue
            diferentes = ((primeiro["tipo_operacao"] or "") != (segundo["tipo_operacao"] or "")
                          or normalizar(primeiro["descricao"]) != normalizar(segundo["descricao"]))
            if segundo["valor_centavos"] == primeiro["valor_centavos"] and diferentes:
                repetido = primeiro if primeiro["tipo_operacao"] == "PAGAMENTO_FATURA" else segundo
                duplicados.add(repetido["id"])
                usados.update((primeiro["id"], segundo["id"]))
                break

    # Segunda forma: pagamentos iguais no cartão em número maior que as saídas do mesmo valor, nas
    # contas do mesmo banco, no mesmo período (o cartão registrou duas vezes o que saiu uma vez da
    # conta). Sem saída correspondente nas contas, como num pagamento em espécie, nada é marcado.
    saidas = defaultdict(list)
    for linha in linhas:
        if linha["conta_tipo"] == "BANK" and linha["valor_centavos"] < 0:
            saidas[(linha["instituicao"], -linha["valor_centavos"])].append(linha)
    restantes = [p for p in pagamentos if p["id"] not in duplicados]
    vistos: set[str] = set()
    for pagamento in restantes:
        if pagamento["id"] in vistos:
            continue
        grupo = [q for q in restantes if q["id"] not in vistos and q["conta_id"] == pagamento["conta_id"]
                 and q["valor_centavos"] == pagamento["valor_centavos"] and _dias(pagamento, q) <= _DIAS_DUPLICADO]
        vistos.update(q["id"] for q in grupo)
        na_conta = [s for s in saidas.get((pagamento["instituicao"], pagamento["valor_centavos"]), [])
                    if _dias(pagamento, s) <= _DIAS_DUPLICADO]
        sobra = len(grupo) - len(na_conta)
        if na_conta and sobra > 0:
            # repetidos mais prováveis: os montados pela Pluggy, depois os mais recentes
            ordem = sorted(grupo, key=lambda q: (q["tipo_operacao"] == "PAGAMENTO_FATURA", q["data_utc"]), reverse=True)
            duplicados.update(q["id"] for q in ordem[:sobra])

    # Terceira forma (padrão do Banco do Brasil): o cartão registra cada pagamento individual
    # ("PGTO. QR CODE PIX", "PGTO. CASH AG.") e também um "Pagamento recebido" com a soma deles, às
    # vezes repetido. Se há pagamentos individuais no mesmo cartão nos 40 dias anteriores, o
    # consolidado não conta.
    for pagamento in pagamentos:
        if pagamento["id"] in duplicados or not _CONSOLIDADO.search(sem_acento(pagamento["descricao"])):
            continue
        dia = date.fromisoformat(pagamento["dia"])
        if any(q["conta_id"] == pagamento["conta_id"] and _INDIVIDUAL.search(sem_acento(q["descricao"]))
               and 0 <= (dia - date.fromisoformat(q["dia"])).days <= _DIAS_CONSOLIDADO for q in pagamentos):
            duplicados.add(pagamento["id"])
    return duplicados


def encontrar_compras_estornadas(linhas) -> set[str]:
    """Compra no cartão e, depois (até 120 dias), um crédito do mesmo valor no mesmo cartão e da
    mesma loja (ou um crédito de "parcelamento"): a compra foi anulada, por devolução ou porque foi
    parcelada depois. As duas viram neutras; se houver parcelas, são elas que contam como gasto."""
    compras = defaultdict(list)
    for linha in linhas:
        if linha["conta_tipo"] == "CREDIT" and linha["valor_centavos"] < 0:
            compras[(linha["conta_id"], -linha["valor_centavos"])].append(linha)
    creditos = sorted((l for l in linhas if l["conta_tipo"] == "CREDIT" and l["valor_centavos"] > 0
                       and not _e_pagamento_cartao(l)), key=lambda l: l["data_utc"])
    marcados: set[str] = set()
    for credito in creditos:
        texto = normalizar(credito["descricao"])
        lojas = _nome_tokens(f"{credito['descricao']} {credito['comerciante'] or ''}")
        candidatas = [c for c in compras.get((credito["conta_id"], credito["valor_centavos"]), [])
                      if c["id"] not in marcados and c["dia"] <= credito["dia"] and _dias(c, credito) <= _DIAS_ESTORNO
                      and ("parcelamento" in texto
                           or lojas & _nome_tokens(f"{c['descricao']} {c['comerciante'] or ''}"))]
        if candidatas:
            compra = max(candidatas, key=lambda c: c["data_utc"])  # a mais recente antes do crédito
            marcados.update((compra["id"], credito["id"]))
    return marcados


def _fatos(linha, contexto: dict) -> dict:
    partes = (linha["descricao"], linha["descricao_bruta"], linha["comerciante"], linha["contraparte_nome"])
    contraparte = (linha["contraparte_doc"] or "").split(" ", 1)[0] or None
    return {
        "texto": normalizar(" | ".join(p for p in partes if p))[:_LIMITE_TEXTO],
        "conta": linha["conta_tipo"],
        "sentido": "entrada" if linha["valor_centavos"] > 0 else "saida",
        "operacoes": {linha["tipo_operacao"], linha["metodo_pagamento"]} - {None},
        "mesmo_titular": linha["contraparte_mesmo_titular"] == 1,
        "par_proprio": linha["id"] in contexto["pares"],
        "pix_credito_conta": linha["id"] in contexto["pix_conta"],
        "pix_credito_cartao": linha["id"] in contexto["pix_cartao"],
        "pagamento_duplicado": linha["id"] in contexto["duplicados"],
        "compra_estornada": linha["id"] in contexto["estornadas"],
        "serie_cancelada": linha["id"] in contexto.get("series_canceladas", ()),
        "contraparte": contraparte,
        "pessoa": contraparte == "CPF",
        "documento": linha["contraparte_doc"] or "",
        "instituicao": normalizar(linha["instituicao"]),
        "categoria_api": normalizar(linha["categoria_api"]),
        "parcelado": parcelas.parcela_de(linha) is not None,
    }


def _casa(regra: Regra, fatos: dict) -> bool:
    for campo, condicao in regra.condicoes.items():
        if campo == "descricao":
            ok = bool(condicao.search(fatos["texto"]))
        elif campo in ("instituicao", "categoria_api", "documento"):
            ok = bool(condicao.search(fatos[campo]))
        elif campo == "operacao":
            ok = bool(condicao & fatos["operacoes"])
        else:
            ok = fatos[campo] == condicao
        if not ok:
            return False
    return True


def classificar_um(linha, regras: Regras, ajustes: dict, contexto: dict) -> tuple:
    """Devolve (transacao_id, categoria, natureza, origem, duvidoso)."""
    ident = linha["id"]
    manual = ajustes.get(id_curto(ident))
    if manual:
        return ident, manual, regras.categorias[manual], "ajuste manual", 0
    fatos = _fatos(linha, contexto)
    for regra in regras.regras:
        if _casa(regra, fatos):
            return ident, regra.categoria, regras.categorias[regra.categoria], regra.nome, int(regra.duvidoso)
    if fatos["categoria_api"]:
        for padrao, destino in regras.mapa_api:
            if not padrao.search(fatos["categoria_api"]):
                continue
            categoria = destino.get(fatos["sentido"]) if isinstance(destino, dict) else destino
            if not categoria:
                continue
            natureza = regras.categorias[categoria]
            if natureza == "receita" and fatos["sentido"] == "saida":
                continue  # categoria de receita num lançamento de saída: não confia
            return ident, categoria, natureza, "categoria da Pluggy", 0
    for regra in regras.finais:
        if _casa(regra, fatos):
            return ident, regra.categoria, regras.categorias[regra.categoria], regra.nome, int(regra.duvidoso)
    categoria = SEM_CATEGORIA[fatos["sentido"]]
    return ident, categoria, regras.categorias[categoria], "sem regra", 1


def _chave_serie(linha):
    from .revisao import assinatura  # import tardio: revisao importa este módulo
    parcela = parcelas.parcela_de(linha)
    if not parcela:
        return None
    return (linha["conta_id"], assinatura(linha["descricao"]), parcela[1], mes_de_inicio(linha["dia"], parcela[0]))


def encontrar_series_canceladas(linhas, ja_marcados: set[str]) -> set[str]:
    """Parcelamento cancelado (devolução), detectado automaticamente: um crédito no mesmo cartão com o
    valor de uma parcela (ou da compra inteira) de uma série, a partir da primeira parcela e até 150
    dias depois da última lançada, com o nome da mesma loja ou um texto de estorno ou cancelamento.
    A série inteira e esses créditos ficam neutros ("Compra estornada"). Ainda pode errar se um
    estorno sem relação tiver, por acaso, o mesmo valor: por isso a regra marca como duvidoso."""
    series = defaultdict(list)
    for linha in linhas:
        if linha["conta_tipo"] == "CREDIT" and linha["valor_centavos"] < 0 and linha["id"] not in ja_marcados:
            chave = _chave_serie(linha)
            if chave:
                series[chave].append(linha)
    resumo = []
    for chave, itens in series.items():
        parcela = Counter(-l["valor_centavos"] for l in itens).most_common(1)[0][0]
        lojas = set().union(*(_nome_tokens(f"{l['descricao']} {l['comerciante'] or ''}") for l in itens))
        resumo.append((chave, parcela, chave[2], min(l["dia"] for l in itens), max(l["dia"] for l in itens), itens,
                       lojas))
    creditos = sorted((l for l in linhas if l["conta_tipo"] == "CREDIT" and l["valor_centavos"] > 0
                       and l["id"] not in ja_marcados and not _e_pagamento_cartao(l)), key=lambda l: l["data_utc"])
    marcados: set[str] = set()
    for credito in creditos:
        dia = date.fromisoformat(credito["dia"])
        generico = _TEXTO_CANCELAMENTO.search(normalizar(credito["descricao"]))
        lojas_credito = _nome_tokens(f"{credito['descricao']} {credito['comerciante'] or ''}")
        candidatas = []
        for chave, parcela, total, primeiro, ultimo, itens, lojas in resumo:
            distancia = (dia - date.fromisoformat(ultimo)).days
            if chave[0] != credito["conta_id"] or credito["dia"] < primeiro or distancia > _DIAS_CANCELAMENTO:
                continue
            if not (generico or lojas_credito & lojas):
                continue  # crédito sem o nome da loja nem texto de estorno: não anula a série
            valor = credito["valor_centavos"]
            if abs(valor - parcela) <= 1 or abs(valor - parcela * total) <= total:
                candidatas.append((abs(distancia), itens))
        if candidatas:
            marcados.add(credito["id"])
            marcados.update(l["id"] for l in min(candidatas, key=lambda c: c[0])[1])
    return marcados


def classificar(con, regras: Regras, ajustes: dict) -> dict:
    linhas = con.execute("SELECT * FROM lancamentos_classificados").fetchall()
    pix_conta, pix_cartao = encontrar_pix_credito(linhas)
    contexto = {"pares": encontrar_pares(linhas), "pix_conta": pix_conta, "pix_cartao": pix_cartao,
                "duplicados": encontrar_pagamentos_duplicados(linhas), "estornadas": encontrar_compras_estornadas(linhas)}
    contexto["series_canceladas"] = encontrar_series_canceladas(linhas, contexto["estornadas"])
    resultados = [classificar_um(linha, regras, ajustes, contexto) for linha in linhas]
    with con:
        con.execute("DELETE FROM classificacao")
        con.executemany(
            "INSERT INTO classificacao (transacao_id, categoria, natureza, regra, duvidoso) VALUES (?, ?, ?, ?, ?)",
            resultados,
        )
    usados = {id_curto(l["id"]) for l in linhas}
    return {"total": len(resultados), "pares": len(contexto["pares"]), "pix_credito": len(pix_conta),
            "pix_credito_cartao": len(pix_cartao), "duplicados": len(contexto["duplicados"]),
            "estornadas": len(contexto["estornadas"]), "ajustes_sem_lancamento": len(set(ajustes) - usados)}


# ---------------------------------------------------------------- comando
#
# Tudo que este comando imprime é agregado. Para nunca revelar um lançamento:
#   * não imprime descrição, contraparte, data nem valor de lançamento;
#   * categoria com menos de MINIMO_PARA_SOMA lançamentos não mostra soma: as
#     pequenas se juntam numa linha "outras", que segue a mesma regra;
#   * o total de despesa, receita ou neutro some se a parte escondida tiver 1 ou 2
#     lançamentos (senão bastaria subtrair as categorias mostradas);
#   * o --detalhar mostra só contagens, nunca somas.

MINIMO_PARA_SOMA = 3
_OMITIDA = f"soma omitida (menos de {MINIMO_PARA_SOMA} lançamentos)"
_TOTAL_OMITIDO = "total omitido (revelaria a soma de uma categoria pequena)"


def _origem(regra: str) -> str:
    if regra in ("ajuste manual", "categoria da Pluggy", "sem regra"):
        return regra
    return "regras"


def _rotulo_conta(linha) -> str:
    instituicao = (linha["instituicao"] or "?").replace(" (via MeuPluggy)", "")
    tipo = "cartão" if linha["conta_tipo"] == "CREDIT" else "conta"
    return f"{instituicao} {tipo} ••••{linha['numero_final']}"


def _linhas_so_contagens(linhas) -> list[str]:
    grupos = Counter((l["natureza"], l["categoria"]) for l in linhas)
    saida = []
    for natureza in NATUREZAS:
        categorias = sorted(((c, q) for (n, c), q in grupos.items() if n == natureza), key=lambda x: -x[1])
        if categorias:
            saida.append(f"  {natureza}: {sum(q for _, q in categorias)} lanç.")
            saida.extend(f"    {mascara.texto(categoria, 80)}: {quantidade} lanç." for categoria, quantidade in categorias)
    return saida


def _linhas_por_natureza(linhas) -> list[str]:
    grupos = defaultdict(lambda: [0, 0])
    for linha in linhas:
        grupo = grupos[(linha["natureza"], linha["categoria"])]
        grupo[0] += 1
        grupo[1] += linha["valor_centavos"]
    saida = []
    for natureza in NATUREZAS:
        categorias = [(cat, q, s) for (nat, cat), (q, s) in grupos.items() if nat == natureza]
        if not categorias:
            continue
        mostradas = [c for c in categorias if c[1] >= MINIMO_PARA_SOMA]
        pequenas = [c for c in categorias if c[1] < MINIMO_PARA_SOMA]
        qtd_pequenas = sum(q for _, q, _ in pequenas)
        qtd_total = sum(q for _, q, _ in categorias)
        if qtd_pequenas == 0 or qtd_pequenas >= MINIMO_PARA_SOMA:
            total = formato.reais(sum(s for _, _, s in categorias))
        else:
            total = _TOTAL_OMITIDO
        saida.append(f"  {natureza}: {qtd_total} lanç., {total}")
        for categoria, quantidade, soma in sorted(mostradas, key=lambda c: c[2] if natureza == "despesa" else -c[2]):
            saida.append(f"    {mascara.texto(categoria, 80)}: {quantidade} lanç., {formato.reais(soma)}")
        if pequenas:
            nomes = mascara.texto(", ".join(sorted(c[0] for c in pequenas)), 300)
            soma = formato.reais(sum(s for _, _, s in pequenas)) if qtd_pequenas >= MINIMO_PARA_SOMA else _OMITIDA
            saida.append(f"    outras, com menos de {MINIMO_PARA_SOMA} lanç. cada ({nomes}): {qtd_pequenas} lanç., {soma}")
    return saida


def _linhas_detalhamento(linhas) -> list[str]:
    """Só contagens por conta (mascarada), categoria e regra. Nenhuma soma, nenhum texto de lançamento."""
    grupos = Counter((_rotulo_conta(l), l["categoria"], l["regra"]) for l in linhas)
    saida = ["  detalhamento por conta, categoria e regra (só contagens):"]
    atual = None
    for (conta, categoria, regra), quantidade in sorted(grupos.items()):
        if conta != atual:
            saida.append(f"    {conta}:")
            atual = conta
        saida.append(f"      {mascara.texto(categoria, 80)} <- {mascara.texto(regra, 80)}: {quantidade} lanç.")
    return saida


def montar_resumo(perfil: str, linhas, estatisticas: dict, detalhar: bool = False,
                  so_contagens: bool = False) -> list[str]:
    origens = Counter(_origem(l["regra"]) for l in linhas)
    saida = [
        f"Categorização do perfil '{perfil}': {estatisticas['total']} lançamentos",
        "  decididos por: " + ", ".join(f"{nome} {origens.get(nome, 0)}"
                                        for nome in ("regras", "categoria da Pluggy", "ajuste manual", "sem regra")),
        f"  transferências entre contas próprias achadas por par de valores: {estatisticas['pares']}",
        f"  Pix no crédito reconhecidos: {estatisticas['pix_credito']} saídas na conta, "
        f"{estatisticas['pix_credito_cartao']} cobranças no cartão",
        f"  pagamentos de fatura repetidos pela API: {estatisticas['duplicados']}; "
        f"lançamentos anulados por estorno do mesmo valor: {estatisticas['estornadas']}",
    ]
    if estatisticas["ajustes_sem_lancamento"]:
        saida.append(f"  AVISO: {estatisticas['ajustes_sem_lancamento']} linha(s) de ajustes.local.toml "
                     "sem lançamento correspondente")
    saida.append(f"  duvidosos para revisar: {sum(1 for l in linhas if l['duvidoso'])}")
    saida.extend(_linhas_so_contagens(linhas) if so_contagens else _linhas_por_natureza(linhas))
    if detalhar:
        saida.extend(_linhas_detalhamento(linhas))
    return saida


def impressao_digital_regras(caminhos=None, conteudos: dict | None = None) -> str:
    """Hash dos arquivos que decidem as categorias (regras genéricas, pessoais e ajustes)."""
    conteudos = conteudos if conteudos is not None else fotografar_regras(caminhos)
    h = hashlib.sha256()
    for caminho, conteudo in conteudos.items():
        h.update(caminho.name.encode())
        h.update(conteudo if conteudo is not None else b"-")
    return h.hexdigest()


def _marca_regras(perfil: str) -> Path:
    return db.caminho(perfil).with_name("regras-confirmadas.sha256")


def regras_confirmadas(perfil: str, digital: str) -> bool:
    """Estas regras já foram confirmadas pelo dono? Mudar uma regra e comparar contagens antes e
    depois é um oráculo (dá para reconstruir descrições letra a letra); por isso, no perfil real,
    regras novas só são aplicadas depois que o dono confirma no próprio terminal."""
    marca = _marca_regras(perfil)
    return marca.exists() and marca.read_text(encoding="ascii").strip() == digital


def _gravar_marca(perfil: str, digital: str) -> None:
    marca = _marca_regras(perfil)
    marca.parent.mkdir(parents=True, exist_ok=True)
    marca.write_text(digital, encoding="ascii")


def console_do_dono() -> bool:
    """Entrada e saída ligadas a um console de verdade (o terminal do dono). sys.stdin.isatty() não
    serve: no Windows ele também é verdadeiro para o dispositivo NUL, que é a entrada dos comandos de
    um assistente de IA e da tarefa agendada (o mensal.cmd usa <nul e grava a saída no log)."""
    if sys.stdin is None or sys.stdout is None:
        return False
    if sys.platform != "win32":
        return sys.stdin.isatty() and sys.stdout.isatty()
    import ctypes
    import msvcrt
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    modo = ctypes.c_uint32()
    for arquivo in (sys.stdin, sys.stdout):
        try:
            identificador = msvcrt.get_osfhandle(arquivo.fileno())
        except (AttributeError, OSError, ValueError):
            return False
        if not kernel32.GetConsoleMode(identificador, ctypes.byref(modo)):
            return False
    return True


SAIDA_REGRAS_NAO_CONFIRMADAS = 3  # código de saída do categorizar e do relatorio quando param por isso


def liberar_regras(perfil: str, conteudos: dict) -> bool:
    """No perfil real, regras e ajustes só são aplicados se o dono já os confirmou. Se mudaram: no
    terminal do dono, pergunta (é preciso digitar CONFIRMO); fora dele (assistente de IA, tarefa
    agendada), não aplica nada, põe um aviso no painel antigo e devolve False. Como nada é
    classificado com regras não confirmadas, as contagens não servem de oráculo e uma regra lenta
    nem chega a rodar."""
    if perfil != "real":
        return True
    digital = impressao_digital_regras(conteudos=conteudos)
    if regras_confirmadas(perfil, digital):
        return True
    if console_do_dono():
        # Um erro no TOML aparece antes da pergunta: a confirmação nunca fica gravada para regras que não carregam.
        regras = carregar_regras(conteudos=conteudos)
        carregar_ajustes(regras.categorias, conteudos=conteudos)
        print("As regras ou os ajustes (regras.toml, regras.local.toml, ajustes.local.toml) ainda não foram")
        print("confirmados ou mudaram desde a última confirmação. Se foi você quem mudou, digite CONFIRMO e")
        print("Enter para aplicar. Se não foi, aperte só Enter e confira esses arquivos.")
        try:
            resposta = input("Digite CONFIRMO ou aperte Enter: ")
        except EOFError:
            resposta = ""
        if resposta.strip().upper() == "CONFIRMO":
            _gravar_marca(perfil, digital)
            return True
        print("Regras não aplicadas: nada foi classificado e o painel continua como estava.")
        return False
    print("As regras ou os ajustes ainda não foram confirmados ou mudaram desde a última confirmação: nada")
    print("foi classificado e o painel não foi atualizado. Para liberar, rode numa janela do PowerShell")
    print("aberta por você, na pasta do projeto,")
    print("  .\\financas.cmd categorizar --perfil real")
    print("e digite CONFIRMO.")
    try:
        from .relatorio import avisar_regras_nao_confirmadas
        avisar_regras_nao_confirmadas(perfil)
    except Exception:  # o aviso no painel nunca muda o código de saída 3
        print("Aviso: não foi possível marcar o painel como desatualizado.", file=sys.stderr)
    return False


def modo_so_contagens(perfil: str, so_contagens: bool, com_somas: bool) -> bool:
    """No perfil real o padrão é só contagens: comparar as somas de duas execuções (antes e depois
    de um ajuste, de uma coleta ou de uma regra nova) revelaria o valor de um lançamento.
    --com-somas é para o dono, no próprio terminal."""
    return so_contagens or (perfil == "real" and not com_somas)


def categorizar(perfil: str, detalhar: bool = False, so_contagens: bool = False, com_somas: bool = False) -> int:
    so_contagens = modo_so_contagens(perfil, so_contagens, com_somas)
    conteudos = fotografar_regras()
    if not liberar_regras(perfil, conteudos):
        return SAIDA_REGRAS_NAO_CONFIRMADAS
    regras = carregar_regras(conteudos=conteudos)
    ajustes = carregar_ajustes(regras.categorias, conteudos=conteudos)
    con = db.conectar(perfil)
    try:
        estatisticas = classificar(con, regras, ajustes)
        linhas = con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        pagina = revisao.gerar(perfil, linhas, regras.categorias)
    finally:
        con.close()
    print("\n".join(montar_resumo(perfil, linhas, estatisticas, detalhar, so_contagens)))
    print(f"Página de revisão: {pagina}")
    print(f"Próximo passo: .\\financas.cmd relatorio --perfil {perfil}")
    return 0
