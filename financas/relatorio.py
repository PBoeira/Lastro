"""Comando 'relatorio': painel HTML local do último mês fechado (ou do mês pedido).

Gera painel-AAAA-MM.html e painel.html (o do último mês fechado) em
%USERPROFILE%\\financas-dados\\relatorios\\ (perfil real) ou dados\\teste\\relatorios\\ (perfil teste). HTML autocontido: sem script, sem fonte,
imagem ou folha externa; a Content-Security-Policy bloqueia tudo que não é estilo inline.

Consolidado sem contar em dobro: só despesas e receitas entram nos totais. Pagamento de
fatura, transferências entre contas próprias e Caixinha são neutros e aparecem à parte.

A tela mostra só o caminho do arquivo, contagens e o resultado das conferências
(confere ou difere), nunca valores.
"""

import html
import os
import re
import shutil
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from . import categorizador, coletor, config, datas, db, formato, parcelas
from .revisao import assinatura

_MESES = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
_MESES_LONGOS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
                 "setembro", "outubro", "novembro", "dezembro")
_FATURA = "Pagamento de fatura"
_ENTRADA_NM = "Entrada de conta própria não monitorada"
_SAIDA_NM = "Saída para conta própria não monitorada"
_POUPANCA = "Poupança (Caixinha e aplicações)"
_RESGATE = "Resgate de poupança"
_ESTORNADA = "Compra estornada"
_FATURA_DUPLICADA = "Pagamento de fatura (duplicado pela API)"
_TOLERANCIA_FATURA = 100  # centavos


# ---------------------------------------------------------------- datas

def quantos(numero: int, singular: str, plural: str) -> str:
    return f"{numero} {singular if numero == 1 else plural}"


def avisos_das_conexoes(itens, agora=None) -> list[str]:
    """Conexões com dados possivelmente parados: autorização vencida, status diferente de UPDATED ou
    banco sem sincronizar há mais de uma semana antes da última coleta. Sem nenhum valor."""
    agora = agora or datas.agora()
    avisos = []
    for item in itens:
        problemas = []
        expira = item["autorizacao_expira"]
        if expira and datas.ler_iso(expira) <= agora:
            problemas.append(f"autorização vencida em {datas.formatar(expira, com_hora=False)}")
        if item["status"] and item["status"] != "UPDATED":
            problemas.append(f"status {item['status']}")
        atraso = coletor.sincronizacao_atrasada(item["ultima_sincronizacao"], item["visto_em"])
        if atraso:
            problemas.append(atraso)
        if problemas:
            avisos.append(f"{item['instituicao'] or 'conexão sem nome'}: {'; '.join(problemas)}")
    return avisos


def mes_anterior(mes: str) -> str:
    ano, numero = map(int, mes.split("-"))
    return f"{ano - 1}-12" if numero == 1 else f"{ano}-{numero - 1:02d}"


def somar_meses(mes: str, quantidade: int) -> str:
    ano, numero = map(int, mes.split("-"))
    total = ano * 12 + (numero - 1) + quantidade
    return f"{total // 12}-{total % 12 + 1:02d}"


def meses_ate(mes: str, quantidade: int) -> list[str]:
    return [somar_meses(mes, -i) for i in range(quantidade - 1, -1, -1)]


def ultimo_mes_fechado(hoje: date) -> str:
    return (hoje.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def rotulo_mes(mes: str, longo: bool = False) -> str:
    ano, numero = mes.split("-")
    return f"{_MESES_LONGOS[int(numero) - 1]}/{ano}" if longo else f"{_MESES[int(numero) - 1]}/{ano[2:]}"


def _conta_rotulo(instituicao, tipo, numero_final) -> str:
    nome = (instituicao or "?").replace(" (via MeuPluggy)", "")
    return f"{nome} {'cartão' if tipo == 'CREDIT' else 'conta'} ••••{numero_final or ''}"


# ---------------------------------------------------------------- cálculos

def _totais(linhas, mes: str) -> dict:
    receitas = sum(l["valor_centavos"] for l in linhas if l["mes"] == mes and l["natureza"] == "receita")
    despesas = -sum(l["valor_centavos"] for l in linhas if l["mes"] == mes and l["natureza"] == "despesa")
    saldo = receitas - despesas
    taxa = saldo / receitas if receitas > 0 else None
    return {"receitas": receitas, "despesas": despesas, "saldo": saldo, "taxa": taxa}


def _gastos_por_categoria(linhas, mes: str) -> dict:
    gastos = defaultdict(int)
    for linha in linhas:
        if linha["mes"] == mes and linha["natureza"] == "despesa":
            gastos[linha["categoria"]] -= linha["valor_centavos"]
    return dict(gastos)


def _fora_das_contas(linhas, mes: str) -> dict:
    def soma(condicao):
        return sum(l["valor_centavos"] for l in linhas if l["mes"] == mes and condicao(l))
    pago_no_cartao = soma(lambda l: l["categoria"] == _FATURA and l["conta_tipo"] == "CREDIT" and l["valor_centavos"] > 0)
    pago_pelas_contas = -soma(lambda l: l["categoria"] == _FATURA and l["conta_tipo"] == "BANK")
    return {
        "pago_no_cartao": pago_no_cartao,
        "pago_pelas_contas": pago_pelas_contas,
        "especie": max(0, pago_no_cartao - pago_pelas_contas),
        "entradas_nao_monitoradas": soma(lambda l: l["categoria"] == _ENTRADA_NM),
        "saidas_nao_monitoradas": -soma(lambda l: l["categoria"] == _SAIDA_NM),
        "aplicado": -soma(lambda l: l["categoria"] == _POUPANCA),
        "resgatado": soma(lambda l: l["categoria"] == _RESGATE),
    }


def _recorrentes(linhas, mes: str) -> list[dict]:
    """Cobranças que se repetem: mesmo padrão de descrição em 3 ou mais dos últimos 6 meses,
    no máximo uma por mês em média, com valores parecidos. Parcelas ficam de fora."""
    janela = set(meses_ate(mes, 6))
    grupos = defaultdict(list)
    for linha in linhas:
        if (linha["mes"] in janela and linha["natureza"] == "despesa" and linha["valor_centavos"] < 0
                and parcelas.parcela_de(linha) is None):
            chave = assinatura(linha["descricao"]) or assinatura(linha["comerciante"])
            if chave:
                grupos[(chave, linha["conta_id"])].append(linha)
    resultado = []
    for (chave, _), itens in grupos.items():
        meses = {l["mes"] for l in itens}
        if len(meses) < 3 or len(itens) > 1.25 * len(meses):
            continue
        valores = [-l["valor_centavos"] for l in itens]
        mediana = statistics.median(valores)
        if not all(0.65 * mediana <= v <= 1.35 * mediana for v in valores):
            continue
        itens.sort(key=lambda l: l["data_utc"])
        ultimo = itens[-1]
        anteriores = [l for l in itens if l["mes"] < ultimo["mes"]]
        valor_atual = -ultimo["valor_centavos"]
        valor_anterior = -anteriores[-1]["valor_centavos"] if anteriores else None
        subiu = valor_anterior is not None and valor_atual >= valor_anterior * 1.02 and valor_atual - valor_anterior >= 100
        resultado.append({
            "descricao": ultimo["descricao"] or ultimo["comerciante"] or chave,
            "categoria": ultimo["categoria"],
            "conta": _conta_rotulo(ultimo["instituicao"], ultimo["conta_tipo"], ultimo["numero_final"]),
            "atual": valor_atual, "anterior": valor_anterior, "meses": len(meses), "subiu": subiu,
            "ultimo_mes": ultimo["mes"], "ativa": ultimo["mes"] in (mes, mes_anterior(mes)),
        })
    resultado.sort(key=lambda r: (not r["subiu"], not r["ativa"], -r["atual"]))
    return resultado


def _parceladas(transacoes, hoje_iso: str) -> list[dict]:
    # Cada série é identificada pelo mês em que caiu a parcela 1: duas compras iguais parceladas
    # em datas diferentes (ex.: uma na 1/12 e outra na 3/12 no mesmo mês) ficam separadas.
    grupos = defaultdict(list)
    for linha in transacoes:
        parcela = parcelas.parcela_de(linha)
        if parcela and linha["valor_centavos"] < 0:
            numero, total, origem = parcela
            chave = (assinatura(linha["descricao"]), total, categorizador.mes_de_inicio(linha["dia"], numero))
            grupos[chave].append((numero, origem, linha))
    resultado = []
    for (_, total, _), itens in grupos.items():
        lancadas = [i for i in itens if i[2]["dia"] <= hoje_iso]
        if lancadas:  # a última parcela já lançada define quantas faltam
            numero_ref, origem, referencia = max(lancadas, key=lambda i: i[0])
            numero = numero_ref
        else:  # todas no futuro: falta desde a primeira que aparece
            numero_ref, origem, referencia = min(itens, key=lambda i: i[0])
            numero = numero_ref - 1
        faltam = max(0, total - numero)
        valor = abs(referencia["valor_centavos"])
        if faltam:
            # Compra devolvida: a última parcela lançada foi anulada por um estorno do mesmo valor,
            # e as parcelas futuras não existem mais.
            cancelada = referencia.get("categoria") == _ESTORNADA
            resultado.append({"descricao": referencia["descricao"], "numero": numero, "total": total, "valor": valor,
                              "restante": 0 if cancelada else faltam * valor, "cancelada": cancelada,
                              "termina": somar_meses(referencia["mes"], total - numero_ref), "origem": origem})
    resultado = _juntar_series_partidas(resultado)
    resultado.sort(key=lambda p: (p["cancelada"], -p["restante"]))
    return resultado


def _juntar_series_partidas(series: list[dict]) -> list[dict]:
    """A mesma série às vezes aparece partida em duas (descrições diferentes entre parcelas).
    Duas compras diferentes começadas no mesmo mês estariam sempre no mesmo número de parcela;
    então mesmo total, mesmo valor (até 1 centavo), mesmo mês final e números DIFERENTES indicam a
    mesma série. Fica a de número maior (a que sabe de mais parcelas já pagas)."""
    ficam: list[dict] = []
    for serie in sorted(series, key=lambda s: -s["numero"]):
        repetida = any(s["total"] == serie["total"] and s["termina"] == serie["termina"]
                       and abs(s["valor"] - serie["valor"]) <= 1 and s["numero"] != serie["numero"] for s in ficam)
        if not repetida:
            ficam.append(serie)
    return ficam


_METODOS = ("compras do ciclo", "equação do extrato", "fatura informada pelo banco",
             "fatura informada + equação do extrato", "equação com pagamentos pela fatura informada")


def conferir_ciclo(transacoes, anterior: dict, fatura: dict) -> dict:
    """Confere uma fatura fechada contra os lançamentos do cartão no ciclo dela.

    Testa dois métodos e duas fronteiras de data e fica com o que chegar mais perto:
      compras do ciclo     total = compras (inclui estornos, IOF e tarifas)
      equação do extrato   total = saldo da fatura anterior - pagamentos no ciclo + compras
    Fronteiras: dia do fechamento dentro do ciclo que fecha, ou no ciclo seguinte.
    Se o banco informa a fatura de cada lançamento (billId), testa também os dois métodos
    usando essa informação no lugar das datas, que é a forma mais precisa. Se os pagamentos
    também trazem a fatura, testa a equação com os pagamentos atribuídos por ela (um pagamento
    feito antes do fechamento pode pertencer à fatura seguinte, e vice-versa).
    """
    inicio, fim = anterior["fechamento"], fatura["fechamento"]
    total = fatura["total"] or 0
    saldo_anterior = anterior["total"] or 0
    despesas = [l for l in transacoes if l["natureza"] == "despesa"]
    pagamentos = [l for l in transacoes if l["categoria"] == _FATURA]
    candidatos = []  # (método, fronteira, compras, pagos, esperado); em empate vale o primeiro
    for fronteira, dentro in (("fechamento incluso", lambda d: inicio < d <= fim),
                              ("fechamento no ciclo seguinte", lambda d: inicio <= d < fim)):
        compras = -sum(l["valor_centavos"] for l in despesas if dentro(l["dia"]))
        pagos = sum(l["valor_centavos"] for l in pagamentos if dentro(l["dia"]))
        candidatos += [(_METODOS[0], fronteira, compras, pagos, compras),
                       (_METODOS[1], fronteira, compras, pagos, saldo_anterior - pagos + compras)]
    identificador = fatura.get("id")
    if identificador and any(l.get("fatura_id") == identificador for l in despesas):
        compras = -sum(l["valor_centavos"] for l in despesas if l.get("fatura_id") == identificador)
        pagos = sum(l["valor_centavos"] for l in pagamentos if inicio < l["dia"] <= fim)
        candidatos += [(_METODOS[2], "fatura de cada lançamento", compras, pagos, compras),
                       (_METODOS[3], "fatura de cada lançamento", compras, pagos, saldo_anterior - pagos + compras)]
        for alvo, rotulo in ((identificador, "pagamentos com esta fatura informada"),
                             (anterior.get("id"), "pagamentos com a fatura anterior informada")):
            if alvo and any(l.get("fatura_id") == alvo for l in pagamentos):
                pagos_fatura = sum(l["valor_centavos"] for l in pagamentos if l.get("fatura_id") == alvo)
                candidatos.append((_METODOS[4], rotulo, compras, pagos_fatura, saldo_anterior - pagos_fatura + compras))
    metodo, fronteira, compras, pagos, esperado = min(candidatos, key=lambda c: abs(c[4] - total))
    diferenca = esperado - total
    return {"fechamento": fim, "total": total, "esperado": esperado, "diferenca": diferenca, "compras": compras,
            "pagos": pagos, "metodo": metodo, "fronteira": fronteira, "confere": abs(diferenca) <= _TOLERANCIA_FATURA}


def _cartoes(linhas, contas, faturas, hoje: date) -> list[dict]:
    hoje_iso = hoje.isoformat()
    resultado = []
    for conta in contas:
        if conta["tipo"] != "CREDIT":
            continue
        transacoes = [l for l in linhas if l["conta_id"] == conta["id"]]
        fechadas = sorted((f for f in faturas if f["conta_id"] == conta["id"] and f["fechamento"]
                           and f["fechamento"] <= hoje_iso), key=lambda f: f["fechamento"])
        cartao = {
            "conta": _conta_rotulo(conta["instituicao"], "CREDIT", conta["numero_final"]),
            "limite_utilizado": abs(conta["saldo_centavos"] or 0),
            "limite_total": conta["limite_centavos"],
            "parceladas": _parceladas(transacoes, hoje_iso),
            "faturas": [{"fechamento": f["fechamento"], "vencimento": f["vencimento"], "total": f["total_centavos"]}
                        for f in fechadas[-6:]][::-1],
            "fatura_atual": None, "conferencias": [],
        }
        historico = [{"id": f["id"], "fechamento": f["fechamento"], "total": f["total_centavos"]} for f in fechadas[-7:]]
        cartao["conferencias"] = [conferir_ciclo(transacoes, historico[i - 1], historico[i])
                                  for i in range(1, len(historico))][::-1]
        cartao["parcelas_pelas_compras"] = sum(p["restante"] for p in cartao["parceladas"])
        cartao["parceladas_canceladas"] = sum(1 for p in cartao["parceladas"] if p["cancelada"])
        # Pagamentos de fatura dos últimos 8 meses, dos dois lados (cartão e conta do mesmo banco),
        # para o dono ver no painel se algum pagamento aparece repetido ou no ciclo errado.
        inicio_lista = somar_meses(hoje.strftime("%Y-%m"), -8) + "-01"
        cartao["pagamentos"] = sorted(
            ({"dia": l["dia"], "onde": "cartão" if l["conta_tipo"] == "CREDIT" else "conta", "descricao": l["descricao"],
              "valor": abs(l["valor_centavos"]), "categoria": l["categoria"], "fatura": l.get("fatura_id")}
             for l in linhas if l["dia"] >= inicio_lista and l["categoria"] in (_FATURA, _FATURA_DUPLICADA)
             and (l["conta_id"] == conta["id"] or (l["conta_tipo"] == "BANK" and l["instituicao"] == conta["instituicao"]))),
            key=lambda x: x["dia"], reverse=True)
        ids_faturas = {f["id"] for f in faturas if f["conta_id"] == conta["id"]}
        cartao["lancamentos"] = len(transacoes)
        cartao["com_fatura"] = sum(1 for l in transacoes if l.get("fatura_id"))
        cartao["com_fatura_conhecida"] = sum(1 for l in transacoes if l.get("fatura_id") in ids_faturas)
        if fechadas:
            ultimo = fechadas[-1]["fechamento"]
            ids_fechadas = {f["id"] for f in fechadas}
            # A fatura aberta é a que o banco informa nos lançamentos do ciclo aberto. Com ela, um
            # lançamento entra na próxima fatura pelo que o banco diz, mesmo com data futura; sem
            # a informação do banco, vale a data.
            abertas = Counter(l.get("fatura_id") for l in transacoes if l["natureza"] == "despesa"
                              and ultimo < l["dia"] <= hoje_iso and l.get("fatura_id")
                              and l.get("fatura_id") not in ids_fechadas)
            id_aberta = abertas.most_common(1)[0][0] if abertas else None

            def na_proxima(l) -> bool:
                if l["natureza"] != "despesa":
                    return False
                if l.get("fatura_id"):
                    return l["fatura_id"] == id_aberta
                return ultimo < l["dia"] <= hoje_iso

            compras = -sum(l["valor_centavos"] for l in transacoes if na_proxima(l))
            pagos_cartao = sum(l["valor_centavos"] for l in transacoes if l["categoria"] == _FATURA and l["dia"] > ultimo)
            pagos_conta = -sum(l["valor_centavos"] for l in linhas if l["conta_tipo"] == "BANK" and l["categoria"] == _FATURA
                               and l["instituicao"] == conta["instituicao"] and l["dia"] > ultimo)
            pagos = max(pagos_cartao, pagos_conta)  # o mesmo pagamento aparece dos dois lados: não soma
            total_ultima = fechadas[-1]["total_centavos"] or 0
            em_aberto = max(0, total_ultima - pagos)
            adiantado = max(0, pagos - total_ultima)
            proxima = max(0, compras - adiantado)
            if not cartao["parcelas_pelas_compras"]:
                # sem compras parceladas conhecidas, o que sobra do limite utilizado é da próxima fatura
                proxima = max(proxima, cartao["limite_utilizado"] - em_aberto)
            cartao.update({
                "ultimo_fechamento": ultimo, "ultimo_vencimento": fechadas[-1]["vencimento"], "ultima_total": total_ultima,
                "fatura_atual": compras, "proxima": proxima, "pagos_desde": pagos, "pagos_cartao": pagos_cartao,
                "pagos_conta": pagos_conta, "fechada_em_aberto": em_aberto, "adiantado": adiantado,
                "parcelas_pelo_saldo": max(0, cartao["limite_utilizado"] - em_aberto - proxima),
                "aberta_e_futuras": max(0, cartao["limite_utilizado"] - em_aberto),
            })
        resultado.append(cartao)
    return resultado


def _contas_bancarias(linhas, contas, mes: str) -> list[dict]:
    resultado = []
    for conta in contas:
        if conta["tipo"] != "BANK":
            continue
        do_mes = [l for l in linhas if l["conta_id"] == conta["id"] and l["mes"] == mes]
        resultado.append({
            "conta": _conta_rotulo(conta["instituicao"], "BANK", conta["numero_final"]),
            "entradas": sum(l["valor_centavos"] for l in do_mes if l["valor_centavos"] > 0),
            "saidas": -sum(l["valor_centavos"] for l in do_mes if l["valor_centavos"] < 0),
            "quantidade": len(do_mes), "saldo_atual": conta["saldo_centavos"],
        })
    return resultado


def _taxa_ampliada(totais: dict, fora: dict) -> dict:
    """Taxa de poupança contando como fonte de recursos o que entrou, em termos líquidos,
    de contas próprias não conectadas. Dinheiro em espécie não entra (não dá para saber de onde veio)."""
    recursos = totais["receitas"] + max(0, fora["entradas_nao_monitoradas"] - fora["saidas_nao_monitoradas"])
    return {"recursos": recursos, "taxa": (recursos - totais["despesas"]) / recursos if recursos > 0 else None}


_MES_VALIDO = re.compile(r"^(20[0-9][0-9])-(0[1-9]|1[0-2])$", re.ASCII)


def meses_do_historico(linhas, mes: str) -> list[str]:
    """Todos os meses, do primeiro com lançamento até o mês do painel. Meses sem lançamento entram
    zerados; parcelas com data futura ficam de fora."""
    primeiro = min((l["mes"] for l in linhas if _MES_VALIDO.match(l["mes"] or "") and l["mes"] <= mes), default=mes)
    meses = [primeiro]
    while meses[-1] < mes:
        meses.append(somar_meses(meses[-1], 1))
    return meses


def mesmo_mes_do_ano_anterior(mes: str) -> str:
    return somar_meses(mes, -12)


def _somar_periodo(por_mes: dict, meses) -> dict:
    receitas = sum(por_mes[m]["receitas"] for m in meses)
    despesas = sum(por_mes[m]["despesas"] for m in meses)
    saldo = receitas - despesas
    return {"receitas": receitas, "despesas": despesas, "saldo": saldo, "taxa": saldo / receitas if receitas > 0 else None}


def resumo_anual(historico: list[str], por_mes: dict) -> list[dict]:
    """Um item por ano do histórico. A comparação com o ano anterior usa só os meses que existem
    nos dois anos (ex.: jan a out de 2026 contra jan a out de 2025): igual com igual."""
    presentes = set(historico)
    anos = []
    for ano in sorted({m[:4] for m in historico}):
        meses = [m for m in historico if m[:4] == ano]
        pares = [(m, mesmo_mes_do_ano_anterior(m)) for m in meses if mesmo_mes_do_ano_anterior(m) in presentes]
        anos.append({
            "ano": ano, "meses": meses, "completo": len(meses) == 12, "totais": _somar_periodo(por_mes, meses),
            "meses_comparaveis": [m for m, _ in pares],
            "comparavel": _somar_periodo(por_mes, [m for m, _ in pares]) if pares else None,
            "anterior_comparavel": _somar_periodo(por_mes, [a for _, a in pares]) if pares else None,
        })
    return anos


def visoes_de_periodo(historico: list[str]) -> list[dict]:
    """Visões prontas do seletor de período (sem JavaScript): últimos 12 meses, cada ano e todo o
    histórico (esta só quando o histórico passa de 12 meses). Os ids só têm dígitos e letras."""
    visoes = [{"id": "12m", "rotulo": "Últimos 12 meses", "meses": historico[-12:]}]
    for ano in sorted({m[:4] for m in historico}, reverse=True):
        visoes.append({"id": ano, "rotulo": ano, "meses": [m for m in historico if m[:4] == ano]})
    if len(historico) > 12:
        visoes.append({"id": "tudo", "rotulo": "Todo o histórico", "meses": list(historico)})
    return visoes


def calcular(linhas, contas, faturas, mes: str, hoje: date) -> dict:
    linhas = [dict(l, mes=l["dia"][:7]) for l in linhas]
    anterior = mes_anterior(mes)
    ano_anterior = mesmo_mes_do_ano_anterior(mes)
    historico = meses_do_historico(linhas, mes)
    por_mes = {m: _totais(linhas, m) for m in historico}
    gasto_por_mes = {m: _gastos_por_categoria(linhas, m) for m in historico}
    categorias_atual = gasto_por_mes[mes]
    categorias_anterior = _gastos_por_categoria(linhas, anterior)
    gasto_ano_anterior = gasto_por_mes.get(ano_anterior, {})
    return {
        "mes": mes, "anterior": anterior, "ano_anterior": ano_anterior, "hoje": hoje,
        "atual": por_mes[mes], "mes_anterior": _totais(linhas, anterior),
        "mesmo_mes_ano_anterior": por_mes.get(ano_anterior),  # None quando o mês está fora do histórico
        "ampliada": _taxa_ampliada(por_mes[mes], _fora_das_contas(linhas, mes)),
        "ampliada_anterior": _taxa_ampliada(_totais(linhas, anterior), _fora_das_contas(linhas, anterior)),
        "historico": historico, "por_mes": por_mes,
        "ampliada_por_mes": {m: _taxa_ampliada(por_mes[m], _fora_das_contas(linhas, m))["taxa"] for m in historico},
        "anos": resumo_anual(historico, por_mes), "visoes": visoes_de_periodo(historico),
        "gasto_por_mes": gasto_por_mes,
        "categorias_historico": sorted({c for g in gasto_por_mes.values() for c in g},
                                       key=lambda c: -sum(g.get(c, 0) for g in gasto_por_mes.values())),
        "categorias": sorted(set(categorias_atual) | set(categorias_anterior) | set(gasto_ano_anterior),
                             key=lambda c: -categorias_atual.get(c, 0)),
        "gasto_atual": categorias_atual, "gasto_anterior": categorias_anterior, "gasto_ano_anterior": gasto_ano_anterior,
        "fora": _fora_das_contas(linhas, mes), "fora_anterior": _fora_das_contas(linhas, anterior),
        "recorrentes": _recorrentes(linhas, mes),
        "cartoes": _cartoes(linhas, contas, faturas, hoje),
        "contas": _contas_bancarias(linhas, contas, mes),
        "lancamentos_mes": sum(1 for l in linhas if l["mes"] == mes),
        "duvidosos_mes": sum(1 for l in linhas if l["mes"] == mes and l["duvidoso"]),
        "ultima_coleta": max((l["visto_em"] for l in linhas), default=None),
    }


# ---------------------------------------------------------------- HTML

_ESTILO = """
:root { color-scheme: light;
  --papel:#fbfaf8; --tinta:#16181d; --tinta-2:#4f535b; --muda:#6b6e75; --linha:#dcdad4; --forte:#16181d;
  --destaque:#1f3b5c; --destaque-claro:#e6ebf1; --neutro:#8f8c84; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark;
  --papel:#121417; --tinta:#e7e5e0; --tinta-2:#b3b1aa; --muda:#85837b; --linha:#2e3137; --forte:#e7e5e0;
  --destaque:#93b2d6; --destaque-claro:#1d2734; --neutro:#7a7870; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--papel); color:var(--tinta);
  font:14px/1.55 "Segoe UI", system-ui, -apple-system, sans-serif; font-variant-numeric:tabular-nums lining-nums; }
main { max-width:1060px; margin:0 auto; padding:32px 20px 72px; }
h1, h2, h3 { font-family:Georgia, Cambria, "Times New Roman", serif; font-weight:normal; color:var(--tinta); }
h1 { font-size:34px; line-height:1.15; margin:6px 0 8px; }
h2 { font-size:22px; margin:48px 0 12px; padding-top:12px; border-top:2px solid var(--forte); }
h3 { font-size:16px; margin:26px 0 8px; }
p { margin:6px 0; color:var(--tinta-2); }
.nota { font-size:12.5px; color:var(--tinta-2); max-width:80ch; }
a { color:var(--destaque); }
.cabecalho { border-bottom:1px solid var(--forte); padding-bottom:14px; }
.sobre { font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--tinta-2); }
.indice { font-size:13px; margin-top:12px; display:flex; flex-wrap:wrap; gap:4px 16px; }
.indice a { text-decoration:none; }
.alerta { border-left:4px solid var(--destaque); background:var(--destaque-claro); color:var(--tinta);
  padding:10px 14px; margin:10px 0; }
.numeros { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); border-top:1px solid var(--linha);
  border-bottom:1px solid var(--linha); margin:8px 0; }
.numero { padding:14px 16px 14px 0; }
.numero + .numero { padding-left:16px; border-left:1px solid var(--linha); }
.numero .rotulo { font-size:11.5px; letter-spacing:.05em; text-transform:uppercase; color:var(--tinta-2); }
.numero .valor { font-size:23px; margin:4px 0 2px; white-space:nowrap; }
.numero .compara { font-size:12px; color:var(--tinta-2); }
.tabela { overflow-x:auto; margin:6px 0; }
table { border-collapse:collapse; width:100%; }
th, td { padding:6px 10px; border-bottom:1px solid var(--linha); text-align:left; vertical-align:baseline; }
th { font-size:11.5px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; color:var(--tinta-2);
  border-bottom:1px solid var(--forte); }
td.n, th.n { text-align:right; white-space:nowrap; }
tr.total td { border-top:1px solid var(--forte); border-bottom:none; font-weight:600; }
table.matriz td, table.matriz th { padding:5px 7px; font-size:12.5px; }
.barra-fundo { width:100%; min-width:90px; height:6px; background:var(--destaque-claro); }
.barra { height:6px; background:var(--destaque); }
.legenda { display:flex; gap:20px; font-size:12px; color:var(--tinta-2); margin:10px 0 4px; }
.legenda span::before { content:""; display:inline-block; width:10px; height:10px; margin-right:6px; vertical-align:-1px; }
.legenda .receitas::before { background:var(--neutro); } .legenda .despesas::before { background:var(--destaque); }
svg { display:block; }
svg text { fill:var(--muda); font-size:11px; font-family:"Segoe UI", system-ui, sans-serif; }
svg .grade { stroke:var(--linha); stroke-width:1; } svg .eixo { stroke:var(--forte); stroke-width:1; }
svg .receitas { fill:var(--neutro); } svg .despesas { fill:var(--destaque); }
.alerta { color:var(--destaque); font-weight:600; }
details { border-bottom:1px solid var(--linha); }
summary { cursor:pointer; padding:8px 0; }
summary .extra { color:var(--tinta-2); font-size:12.5px; margin-left:12px; }
details[open] > summary { color:var(--destaque); }
.periodo > input { position:absolute; opacity:0; width:1px; height:1px; margin:0; }
.rotulos { display:flex; flex-wrap:wrap; border:1px solid var(--forte); width:fit-content; margin:6px 0 16px; }
.rotulos label { padding:5px 14px; font-size:13px; cursor:pointer; }
.rotulos label + label { border-left:1px solid var(--forte); }
.visao { display:none; animation:aparecer 180ms ease-out; }
@keyframes aparecer { from { opacity:0; } to { opacity:1; } }
details[open] > :not(summary) { animation:aparecer 180ms ease-out; }
details::details-content { opacity:0; transition:opacity 180ms ease, content-visibility 180ms allow-discrete; }
details[open]::details-content { opacity:1; }
tr td { transition:background-color 150ms ease; }
tr:hover td { background:var(--destaque-claro); }
svg rect { transition:opacity 150ms ease; }
svg rect:hover { opacity:.7; }
.rotulos label, summary { transition:background-color 150ms ease, color 150ms ease; }
.rotulos label:hover { background:var(--destaque-claro); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation:none !important; transition:none !important; } }
@media (prefers-reduced-motion: reduce) { details::details-content { transition:none !important; } }
@media print { body { background:#fff; } .rotulos, .indice { display:none; } h2 { break-after:avoid; }
  tr, .numero { break-inside:avoid; } }
"""


def _estilo_das_visoes(visoes) -> str:
    """Seletor de período só com CSS: o botão de opção marcado mostra a sua visão."""
    regras = []
    for visao in visoes:
        i = visao["id"]
        if not i.isalnum():  # os ids vêm de visoes_de_periodo; nenhum texto livre entra no CSS
            continue
        regras.append(f'#p-{i}:checked ~ .visoes #v-{i} {{ display:block; }}\n'
                      f'#p-{i}:checked ~ .rotulos label[for="p-{i}"] {{ background:var(--tinta); color:var(--papel); }}\n'
                      f'#p-{i}:focus-visible ~ .rotulos label[for="p-{i}"] '
                      "{ outline:2px solid var(--destaque); outline-offset:2px; }\n")
    return "".join(regras)


_MENOS = "−"  # sinal de menos tipográfico
_ND = "n/d"


def _e(valor) -> str:
    return html.escape("" if valor is None else str(valor))


def _reais(centavos) -> str:
    return _ND if centavos is None else formato.reais(centavos).replace("-", _MENOS, 1)


def _r(centavos) -> str:
    return _e(_reais(centavos))


def _inteiro(centavos: int) -> str:
    """Reais inteiros, sem R$ nem centavos (tabelas ano por mês)."""
    reais = round(centavos / 100)
    texto = f"{abs(reais):,}".replace(",", ".")
    return _MENOS + texto if reais < 0 else texto


def _pct(fracao) -> str:
    if fracao is None:
        return _ND
    texto = f"{abs(fracao) * 100:.0f}%"
    return _MENOS + texto if round(fracao * 100) < 0 else texto


def _variacao(atual: int, anterior: int) -> str:
    if not anterior:
        return "sem base"
    mudanca = (atual - anterior) / abs(anterior) * 100
    if round(mudanca) == 0:
        return "0%"
    return f"{'+' if mudanca > 0 else _MENOS}{abs(mudanca):.0f}%"


def _diferenca(atual: int, anterior: int) -> str:
    diferenca = atual - anterior
    return ("+" if diferenca > 0 else "") + _reais(diferenca)


def _pontos(atual, anterior) -> str:
    """Diferença entre duas taxas, em pontos percentuais."""
    if atual is None or anterior is None:
        return _ND
    pontos = round((atual - anterior) * 100)
    return f"{'+' if pontos > 0 else (_MENOS if pontos < 0 else '')}{abs(pontos)} p.p."


def _compacto(centavos: int) -> str:
    reais = centavos / 100
    if abs(reais) >= 1000:
        return f"R$ {reais / 1000:.1f} mil".replace(".", ",").replace(",0 mil", " mil")
    return f"R$ {reais:.0f}"


def _passo(maximo: int) -> int:
    """Menor passo 'redondo' (1, 2, 2,5 ou 5 vezes potência de 10, em centavos) com até 4 faixas."""
    candidatos = sorted(base * escala for base in (100, 200, 250, 500)
                        for escala in (1, 10, 100, 1000, 10000, 100000, 1000000))
    for passo in candidatos:
        if maximo / passo <= 4:
            return passo
    return max(1, maximo // 4)


def _barra_svg(x: float, y_base: float, largura: float, altura: float, classe: str, dica: str) -> str:
    if altura <= 0:
        return ""
    return (f'<rect class="{classe}" x="{x:.1f}" y="{y_base - altura:.1f}" width="{largura:.1f}" '
            f'height="{altura:.1f}"><title>{_e(dica)}</title></rect>')


def _grafico_barras(meses: list[str], series, rotulo: str, altura: int = 230) -> str:
    """Barras por mês, lado a lado por série. series: [(classe CSS, nome, {mês: centavos})]."""
    largura, esquerda, topo_y = 720, 66, 12
    base_y = altura - 28
    maximo = max([valores.get(m, 0) for _, _, valores in series for m in meses] + [1])
    passo = _passo(maximo)
    teto = passo * -(-maximo // passo)
    escala = (base_y - topo_y) / teto
    partes = [f'<svg viewBox="0 0 {largura} {altura}" width="100%" role="img" aria-label="{_e(rotulo)}">']
    for i in range(0, teto + 1, passo):
        y = base_y - i * escala
        partes.append(f'<line class="{"eixo" if i == 0 else "grade"}" x1="{esquerda}" x2="{largura - 4}" '
                      f'y1="{y:.1f}" y2="{y:.1f}"/><text x="{esquerda - 8}" y="{y + 4:.1f}" text-anchor="end">'
                      f'{_e(_compacto(i))}</text>')
    grupo = (largura - esquerda - 4) / max(1, len(meses))
    barra = max(1.0, min(16.0, (grupo - 4) / len(series)))
    a_cada = max(1, -(-len(meses) // 12))  # no máximo uns 12 rótulos no eixo, sempre com o mês mais recente
    for indice, mes in enumerate(meses):
        x = esquerda + indice * grupo + (grupo - barra * len(series)) / 2
        for classe, nome, valores in series:
            valor = valores.get(mes, 0)
            partes.append(_barra_svg(x, base_y, barra, valor * escala, classe, f"{rotulo_mes(mes)}: {nome} {_reais(valor)}"))
            x += barra
        if (len(meses) - 1 - indice) % a_cada == 0:
            partes.append(f'<text x="{esquerda + indice * grupo + grupo / 2:.1f}" y="{base_y + 18}" '
                          f'text-anchor="middle">{_e(rotulo_mes(mes))}</text>')
    partes.append("</svg>")
    return "".join(partes)


def _matriz_ano_mes(historico: list[str], valores: dict) -> str:
    """Tabela ano por mês, em reais inteiros, do ano mais recente para o mais antigo."""
    presentes = set(historico)
    partes = ['<div class="tabela"><table class="matriz"><tr><th>Ano</th>'
              + "".join(f'<th class="n">{nome}</th>' for nome in _MESES) + '<th class="n">Total</th></tr>']
    for ano in sorted({m[:4] for m in historico}, reverse=True):
        total, celulas = 0, []
        for numero in range(1, 13):
            mes = f"{ano}-{numero:02d}"
            if mes in presentes:
                total += valores.get(mes, 0)
                celulas.append(f'<td class="n">{_e(_inteiro(valores.get(mes, 0)))}</td>')
            else:
                celulas.append('<td class="n"></td>')
        soma = f'<td class="n"><b>{_e(_inteiro(total))}</b></td>'
        partes.append(f"<tr><td>{_e(ano)}</td>{''.join(celulas)}{soma}</tr>")
    partes.append("</table></div>")
    return "".join(partes)


def _numero(rotulo: str, valor_html: str, comparacoes=()) -> str:
    return (f'<div class="numero"><div class="rotulo">{_e(rotulo)}</div><div class="valor">{valor_html}</div>'
            + "".join(f'<div class="compara">{_e(c)}</div>' for c in comparacoes) + "</div>")


def _mes_por_extenso(mes: str) -> str:
    ano, numero = mes.split("-")
    return f"{_MESES_LONGOS[int(numero) - 1].capitalize()} de {ano}"


def _faixa(meses: list[str]) -> str:
    """'jan a out' para meses seguidos do mesmo ano; senão, a quantidade de meses."""
    if not meses:
        return ""
    numeros = [int(m[5:]) for m in meses]
    if numeros != list(range(numeros[0], numeros[0] + len(numeros))):
        return f"{len(meses)} meses"
    inicio, fim = _MESES[numeros[0] - 1], _MESES[numeros[-1] - 1]
    return inicio if len(numeros) == 1 else f"{inicio} a {fim}"


def _visao(d: dict, visao: dict) -> str:
    """Uma visão do seletor de período: números, gráfico, mês a mês e categorias do período."""
    meses, por_mes = visao["meses"], d["por_mes"]
    total = _somar_periodo(por_mes, meses)
    media = round(total["despesas"] / len(meses))
    q = ['<div class="numeros">', _numero("Receitas no período", _r(total["receitas"])),
         _numero("Despesas no período", _r(total["despesas"]), [f"média de {_reais(media)} por mês"]),
         _numero("Receitas menos despesas", _r(total["saldo"])),
         _numero("Taxa de poupança", _e(_pct(total["taxa"])),
                 [f"{len(meses)} meses, de {rotulo_mes(meses[0])} a {rotulo_mes(meses[-1])}"]), "</div>",
         '<div class="legenda"><span class="receitas">Receitas</span><span class="despesas">Despesas</span></div>',
         _grafico_barras(meses, [("receitas", "receitas", {m: por_mes[m]["receitas"] for m in meses}),
                                 ("despesas", "despesas", {m: por_mes[m]["despesas"] for m in meses})],
                         f"Receitas e despesas por mês: {visao['rotulo']}")]
    q.append('<h3>Mês a mês</h3><div class="tabela"><table><tr><th>Mês</th><th class="n">Receitas</th>'
             '<th class="n">Despesas</th><th class="n">Receitas menos despesas</th><th class="n">Taxa de poupança</th>'
             '<th class="n">Taxa com contas não conectadas</th><th class="n">Despesas sobre o mês anterior</th>'
             '<th class="n">Despesas sobre o mesmo mês do ano anterior</th></tr>')
    for m in reversed(meses):
        t = por_mes[m]
        antes, ano = por_mes.get(mes_anterior(m)), por_mes.get(mesmo_mes_do_ano_anterior(m))
        q.append(f'<tr><td>{_e(rotulo_mes(m))}</td><td class="n">{_r(t["receitas"])}</td>'
                 f'<td class="n">{_r(t["despesas"])}</td><td class="n">{_r(t["saldo"])}</td>'
                 f'<td class="n">{_e(_pct(t["taxa"]))}</td><td class="n">{_e(_pct(d["ampliada_por_mes"][m]))}</td>'
                 f'<td class="n">{_e(_variacao(t["despesas"], antes["despesas"]) if antes else _ND)}</td>'
                 f'<td class="n">{_e(_variacao(t["despesas"], ano["despesas"]) if ano else _ND)}</td></tr>')
    q.append(f'<tr class="total"><td>Período</td><td class="n">{_r(total["receitas"])}</td>'
             f'<td class="n">{_r(total["despesas"])}</td><td class="n">{_r(total["saldo"])}</td>'
             f'<td class="n">{_e(_pct(total["taxa"]))}</td><td></td><td></td><td></td></tr></table></div>')
    gastos = defaultdict(int)
    for m in meses:
        for categoria, valor in d["gasto_por_mes"][m].items():
            gastos[categoria] += valor
    if gastos:
        maximo = max([v for v in gastos.values() if v > 0] + [1])
        q.append('<h3>Despesas por categoria no período</h3><div class="tabela"><table><tr><th>Categoria</th><th></th>'
                 '<th class="n">Total</th><th class="n">Parte das despesas</th><th class="n">Média por mês</th></tr>')
        for categoria, valor in sorted(gastos.items(), key=lambda item: -item[1]):
            parte = valor / total["despesas"] if total["despesas"] > 0 else None
            largura = max(0.0, valor / maximo * 100)
            q.append(f'<tr><td>{_e(categoria)}</td><td><div class="barra-fundo"><div class="barra" '
                     f'style="width:{largura:.1f}%"></div></div></td><td class="n">{_r(valor)}</td>'
                     f'<td class="n">{_e(_pct(parte))}</td><td class="n">{_r(round(valor / len(meses)))}</td></tr>')
        q.append(f'<tr class="total"><td>Total</td><td></td><td class="n">{_r(total["despesas"])}</td>'
                 f'<td class="n">{"100%" if total["despesas"] > 0 else _ND}</td><td class="n">{_r(media)}</td>'
                 "</tr></table></div>")
    return "".join(q)


def _montar(d: dict, perfil: str) -> str:
    mes, anterior, ano_anterior = d["mes"], d["anterior"], d["ano_anterior"]
    a, b, passado = d["atual"], d["mes_anterior"], d["mesmo_mes_ano_anterior"]
    nome_mes, nome_anterior = rotulo_mes(mes, longo=True), rotulo_mes(anterior, longo=True)
    nome_passado = rotulo_mes(ano_anterior, longo=True)
    historico = d["historico"]
    p = ['<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
         "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
         '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">'
         '<meta name="viewport" content="width=device-width, initial-scale=1">'
         f"<title>Relatório financeiro: {_e(nome_mes)}</title>"
         f"<style>{_ESTILO}{_estilo_das_visoes(d['visoes'])}</style></head><body><main>"]
    coleta = datas.formatar(d["ultima_coleta"]) if d["ultima_coleta"] else "?"
    p.append(f'<header class="cabecalho"><div class="sobre">Relatório financeiro pessoal · perfil {_e(perfil)}</div>'
             f"<h1>{_e(_mes_por_extenso(mes))}</h1><p>Contas conectadas consolidadas, sem contar em dobro pagamento de "
             f"fatura, transferências entre suas contas e Caixinha. Dados até a coleta de {_e(coleta)}. Histórico no banco "
             f"local: {quantos(len(historico), 'mês', 'meses')}, de {_e(rotulo_mes(historico[0], longo=True))} a "
             f"{_e(nome_mes)}. Arquivo só deste computador: não compartilhe.</p>")
    if d.get("avisos_conexao"):
        p.append(f'<p class="nota alerta"><b>Dados possivelmente parados.</b> {_e("; ".join(d["avisos_conexao"]))}. '
                 f"Rode <code>.\\financas.cmd verificar --perfil {_e(perfil)}</code> no seu terminal e veja "
                 "\"Se der erro\" no README.</p>")
    expira = d.get("autorizacao_expira")
    if expira:
        dias = (date.fromisoformat(datas.dia(expira)) - d["hoje"]).days
        validade = _e(datas.formatar(expira, com_hora=False))
        if dias < 0:
            p.append(f'<p class="nota"><b>Autorização do Meu Pluggy vencida desde {validade}.</b> Renove no '
                     "dashboard da Pluggy (veja \"Renovação anual\" no README).</p>")
        else:
            aviso = ("<b>Renove a autorização no dashboard da Pluggy</b> (veja \"Renovação anual\" no README). "
                     if dias < 30 else "")
            p.append(f'<p class="nota">{aviso}Autorização do Meu Pluggy válida até {validade} '
                     f"({quantos(dias, 'dia', 'dias')}).</p>")
    p.append('<nav class="indice" aria-label="Seções">\n<a href="#resumo">Resumo</a>\n<a href="#periodos">Períodos</a>\n'
             '<a href="#anos">Anos</a>\n<a href="#categorias">Categorias</a>\n<a href="#assinaturas">Assinaturas</a>\n'
             '<a href="#cartoes">Cartões</a>\n<a href="#fora">Por fora das contas</a>\n<a href="#extratos">Extratos</a>\n'
             "</nav></header>")

    # Resumo do mês: contra o mês anterior e contra o mesmo mês do ano anterior
    def comparar(chave, formatar, com_variacao=True):
        itens = [f"{nome_anterior}: {formatar(b[chave])}" + (f" ({_variacao(a[chave], b[chave])})" if com_variacao else "")]
        if passado is not None:
            itens.append(f"{nome_passado}: {formatar(passado[chave])}"
                         + (f" ({_variacao(a[chave], passado[chave])})" if com_variacao else ""))
        return itens

    ampliada = [f"{nome_anterior}: {_pct(d['ampliada_anterior']['taxa'])}"]
    if ano_anterior in d["ampliada_por_mes"]:
        ampliada.append(f"{nome_passado}: {_pct(d['ampliada_por_mes'][ano_anterior])}")
    p.append('<section id="resumo"><h2>Resumo do mês</h2><div class="numeros">')
    p.append(_numero("Receitas", _r(a["receitas"]), comparar("receitas", _reais)))
    p.append(_numero("Despesas", _r(a["despesas"]), comparar("despesas", _reais)))
    p.append(_numero("Receitas menos despesas", _r(a["saldo"]), comparar("saldo", _reais, False)))
    p.append(_numero("Taxa de poupança", _e(_pct(a["taxa"])), comparar("taxa", _pct, False)))
    p.append(_numero("Taxa contando contas não conectadas", _e(_pct(d["ampliada"]["taxa"])), ampliada))
    p.append('</div><p class="nota">A primeira taxa é (receitas menos despesas) dividido pelas receitas, só das contas '
             "monitoradas. Se a sua renda chega principalmente por contas que não estão conectadas, ela sai distorcida: "
             "por isso a segunda soma às receitas o que entrou, em termos líquidos, vindo de contas suas não conectadas. "
             "Dinheiro em espécie não entra em nenhuma das duas (veja \"Por fora das contas monitoradas\").</p>")
    maximo = max([v for v in d["gasto_atual"].values() if v > 0] + [1])
    cabecalho = (f'<tr><th>Categoria</th><th></th><th class="n">{_e(nome_mes)}</th><th class="n">{_e(nome_anterior)}</th>'
                 '<th class="n">Variação</th>')
    if passado is not None:
        cabecalho += f'<th class="n">{_e(nome_passado)}</th><th class="n">Variação</th>'
    p.append(f'<h3>Despesas por categoria no mês</h3><div class="tabela"><table>{cabecalho}</tr>')
    for categoria in d["categorias"]:
        atual, antes = d["gasto_atual"].get(categoria, 0), d["gasto_anterior"].get(categoria, 0)
        largura = max(0.0, atual / maximo * 100)
        linha = (f'<tr><td>{_e(categoria)}</td><td><div class="barra-fundo"><div class="barra" '
                 f'style="width:{largura:.1f}%"></div></div></td><td class="n">{_r(atual)}</td>'
                 f'<td class="n">{_r(antes)}</td><td class="n">{_e(_variacao(atual, antes))}</td>')
        if passado is not None:
            ano = d["gasto_ano_anterior"].get(categoria, 0)
            linha += f'<td class="n">{_r(ano)}</td><td class="n">{_e(_variacao(atual, ano))}</td>'
        p.append(linha + "</tr>")
    total = (f'<tr class="total"><td>Total</td><td></td><td class="n">{_r(a["despesas"])}</td>'
             f'<td class="n">{_r(b["despesas"])}</td><td class="n">{_e(_variacao(a["despesas"], b["despesas"]))}</td>')
    if passado is not None:
        total += (f'<td class="n">{_r(passado["despesas"])}</td>'
                  f'<td class="n">{_e(_variacao(a["despesas"], passado["despesas"]))}</td>')
    p.append(total + '</tr></table></div><p class="nota">"Estornos e cashback" aparece negativo porque reduz os gastos. '
             "Pix enviados a pessoas ficam em \"Pix e transferências enviadas\" até você categorizar na página de "
             "revisão.</p></section>")

    # Períodos: visões prontas, trocadas só com CSS
    p.append('<section id="periodos"><h2>Períodos</h2><p class="nota">Escolha o período nos botões abaixo. Todas as '
             'visões já estão nesta página: nada é calculado nem buscado ao clicar.</p><div class="periodo">')
    for indice, visao in enumerate(d["visoes"]):
        p.append(f'<input type="radio" name="periodo" id="p-{_e(visao["id"])}" aria-label="{_e(visao["rotulo"])}"'
                 f'{" checked" if indice == 0 else ""}>')
    p.append('<div class="rotulos">' + "".join(f'<label for="p-{_e(v["id"])}">{_e(v["rotulo"])}</label>'
                                               for v in d["visoes"]) + "</div>")
    p.append('<div class="visoes">' + "".join(f'<section class="visao" id="v-{_e(v["id"])}">{_visao(d, v)}</section>'
                                              for v in d["visoes"]) + "</div></div></section>")

    # Anos
    p.append('<section id="anos"><h2>Visão por ano</h2><div class="tabela"><table><tr><th>Ano</th>'
             '<th>Meses no histórico</th><th class="n">Receitas</th><th class="n">Despesas</th>'
             '<th class="n">Receitas menos despesas</th><th class="n">Taxa de poupança</th>'
             '<th class="n">Despesas por mês, em média</th></tr>')
    for ano in reversed(d["anos"]):
        t = ano["totais"]
        meses_texto = "12" if ano["completo"] else f"{len(ano['meses'])} ({_faixa(ano['meses'])})"
        p.append(f'<tr><td>{_e(ano["ano"])}</td><td>{_e(meses_texto)}</td><td class="n">{_r(t["receitas"])}</td>'
                 f'<td class="n">{_r(t["despesas"])}</td><td class="n">{_r(t["saldo"])}</td>'
                 f'<td class="n">{_e(_pct(t["taxa"]))}</td><td class="n">{_r(round(t["despesas"] / len(ano["meses"])))}</td></tr>')
    p.append('</table></div><p class="nota">Ano incompleto aparece só com os meses que estão no histórico. O primeiro '
             f'mês ({_e(rotulo_mes(historico[0], longo=True))}) pode estar incompleto: os bancos só entregam os '
             "lançamentos a partir de uma data.</p><h3>Ano contra ano, nos mesmos meses</h3>")
    comparaveis = [ano for ano in d["anos"] if ano["comparavel"]]
    for ano in reversed(comparaveis):
        atual, antes = ano["comparavel"], ano["anterior_comparavel"]
        p.append(f'<div class="tabela"><table><tr><th>Mesmos meses ({_e(_faixa(ano["meses_comparaveis"]))})</th>'
                 f'<th class="n">{int(ano["ano"]) - 1}</th><th class="n">{_e(ano["ano"])}</th><th class="n">Variação</th></tr>')
        for rotulo, chave in (("Receitas", "receitas"), ("Despesas", "despesas")):
            p.append(f'<tr><td>{rotulo}</td><td class="n">{_r(antes[chave])}</td><td class="n">{_r(atual[chave])}</td>'
                     f'<td class="n">{_e(_variacao(atual[chave], antes[chave]))}</td></tr>')
        p.append(f'<tr><td>Receitas menos despesas</td><td class="n">{_r(antes["saldo"])}</td>'
                 f'<td class="n">{_r(atual["saldo"])}</td><td class="n">{_e(_diferenca(atual["saldo"], antes["saldo"]))}</td></tr>'
                 f'<tr><td>Taxa de poupança</td><td class="n">{_e(_pct(antes["taxa"]))}</td>'
                 f'<td class="n">{_e(_pct(atual["taxa"]))}</td><td class="n">{_e(_pontos(atual["taxa"], antes["taxa"]))}</td>'
                 "</tr></table></div>")
    if comparaveis:
        p.append('<p class="nota">Cada comparação usa só os meses que existem nos dois anos.</p>')
    else:
        p.append('<p class="nota">Ainda não há no histórico o mesmo mês em dois anos seguidos para comparar.</p>')
    p.append("<h3>Despesas mês a mês (R$)</h3>" + _matriz_ano_mes(historico, {m: d["por_mes"][m]["despesas"] for m in historico}))
    p.append("<h3>Receitas mês a mês (R$)</h3>" + _matriz_ano_mes(historico, {m: d["por_mes"][m]["receitas"] for m in historico}))
    p.append("</section>")

    # Categorias: por ano e detalhamento de cada uma
    anos, ultimo = d["anos"], d["anos"][-1]

    def soma(categoria, meses):
        return sum(d["gasto_por_mes"][m].get(categoria, 0) for m in meses)

    anteriores = [mesmo_mes_do_ano_anterior(m) for m in ultimo["meses_comparaveis"]]
    comparar_ultimo = ultimo["comparavel"] is not None
    cabecalho = "<tr><th>Categoria</th>" + "".join(
        f'<th class="n">{_e(ano["ano"])}{"" if ano["completo"] else "*"}</th>' for ano in reversed(anos))
    if comparar_ultimo:
        cabecalho += f'<th class="n">{_e(ultimo["ano"])} sobre {int(ultimo["ano"]) - 1}, mesmos meses</th>'
    p.append(f'<section id="categorias"><h2>Categorias</h2><h3>Despesas por categoria e ano</h3><div class="tabela">'
             f"<table>{cabecalho}</tr>")
    for categoria in d["categorias_historico"]:
        linha = f"<tr><td>{_e(categoria)}</td>" + "".join(
            f'<td class="n">{_r(soma(categoria, ano["meses"]))}</td>' for ano in reversed(anos))
        if comparar_ultimo:
            linha += (f'<td class="n">{_e(_variacao(soma(categoria, ultimo["meses_comparaveis"]), soma(categoria, anteriores)))}'
                      "</td>")
        p.append(linha + "</tr>")
    total = '<tr class="total"><td>Total</td>' + "".join(
        f'<td class="n">{_r(ano["totais"]["despesas"])}</td>' for ano in reversed(anos))
    if comparar_ultimo:
        total += (f'<td class="n">{_e(_variacao(ultimo["comparavel"]["despesas"], ultimo["anterior_comparavel"]["despesas"]))}'
                  "</td>")
    p.append(total + "</tr></table></div>")
    if any(not ano["completo"] for ano in anos):
        p.append('<p class="nota">* ano incompleto no histórico.</p>')
    p.append('<h3>Detalhamento por categoria</h3><p class="nota">Abra uma categoria para ver a série de todo o '
             "histórico, mês a mês.</p>")
    for categoria in d["categorias_historico"]:
        valores = {m: d["gasto_por_mes"][m].get(categoria, 0) for m in historico}
        total_categoria = sum(valores.values())
        p.append(f'<details><summary>{_e(categoria)}<span class="extra">{_r(total_categoria)} no histórico, média de '
                 f"{_r(round(total_categoria / len(historico)))} por mês</span></summary>")
        p.append(_grafico_barras(historico, [("despesas", categoria, valores)], f"{categoria}: despesas por mês", altura=140))
        p.append(_matriz_ano_mes(historico, valores) + "</details>")
    p.append("</section>")

    # Recorrentes
    p.append('<section id="assinaturas"><h2>Assinaturas e cobranças recorrentes</h2>')
    if d["recorrentes"]:
        p.append('<div class="tabela"><table><tr><th>Cobrança</th><th>Categoria</th><th>Conta</th>'
                 '<th class="n">Último valor</th><th class="n">Anterior</th><th class="n">Meses (de 6)</th><th>Situação</th></tr>')
        for r in d["recorrentes"]:
            if r["subiu"]:
                situacao = f'<span class="alerta">subiu {_r(r["atual"] - r["anterior"])}</span>'
            elif not r["ativa"]:
                situacao = f"sem cobrança desde {_e(rotulo_mes(r['ultimo_mes']))}"
            else:
                situacao = "estável"
            p.append(f'<tr><td>{_e(r["descricao"])}</td><td>{_e(r["categoria"])}</td><td>{_e(r["conta"])}</td>'
                     f'<td class="n">{_r(r["atual"])}</td><td class="n">{_r(r["anterior"])}</td>'
                     f'<td class="n">{r["meses"]}</td><td>{situacao}</td></tr>')
        p.append('</table></div><p class="nota">Recorrente: o mesmo padrão de descrição em 3 ou mais dos últimos '
                 "6 meses, com valores parecidos. Parcelas ficam na seção de cartões.</p>")
    else:
        p.append("<p>Nenhuma cobrança recorrente encontrada nos últimos 6 meses.</p>")
    p.append("</section>")

    # Cartões
    p.append('<section id="cartoes"><h2>Cartões: fatura atual e parcelas futuras</h2>')
    if not d["cartoes"]:
        p.append("<p>Nenhum cartão de crédito conectado.</p>")
    for c in d["cartoes"]:
        p.append(f"<h3>{_e(c['conta'])}</h3>")
        if c["fatura_atual"] is None:
            p.append('<p class="nota">Sem fatura fechada registrada: não dá para separar a fatura atual.</p>')
            continue
        fechou = datas.formatar(c["ultimo_fechamento"], com_hora=False)
        vence = datas.formatar(c["ultimo_vencimento"], com_hora=False) if c["ultimo_vencimento"] else "?"
        p.append('<div class="numeros">')
        p.append(_numero("Limite utilizado (saldo do banco)", _r(c["limite_utilizado"])))
        p.append(_numero(f"Fatura fechada em {fechou} (vence {vence}): falta pagar", _r(c["fechada_em_aberto"]),
                         [f"total {_reais(c['ultima_total'])}, pago desde o fechamento {_reais(c['pagos_desde'])}"]))
        p.append(_numero("Fatura aberta + parcelas futuras", _r(c["aberta_e_futuras"]),
                         ["limite utilizado menos a fatura fechada que falta pagar"]))
        p.append("</div>")
        p.append('<p class="nota">Os números acima vêm do banco: o limite utilizado e a fatura fechada. A divisão entre a '
                 "fatura aberta e as parcelas futuras não é confiável sem conferência manual, por isso as duas aparecem "
                 "somadas. A lista de compras parceladas abaixo é só uma estimativa.</p>")
        p.append('<details><summary>Detalhes informativos: conferência das faturas e pagamentos</summary>')
        if c["conferencias"]:
            p.append('<h3>Conferência das faturas fechadas (informativa)</h3><div class="tabela"><table><tr><th>Fechamento</th>'
                     '<th class="n">Total da fatura</th><th class="n">Calculado</th><th class="n">Diferença</th>'
                     '<th class="n">Compras no ciclo</th><th class="n">Pagamentos no ciclo</th><th>Método que chegou mais perto</th>'
                     "<th>Resultado</th></tr>")
            for k in c["conferencias"]:
                resultado = "confere" if k["confere"] else '<span class="alerta">difere</span>'
                p.append(f'<tr><td>{_e(datas.formatar(k["fechamento"], com_hora=False))}</td><td class="n">{_r(k["total"])}</td>'
                         f'<td class="n">{_r(k["esperado"])}</td><td class="n">{_r(k["diferenca"])}</td>'
                         f'<td class="n">{_r(k["compras"])}</td><td class="n">{_r(k["pagos"])}</td>'
                         f'<td>{_e(k["metodo"])}, {_e(k["fronteira"])}</td><td>{resultado}</td></tr>')
            p.append('</table></div><p class="nota">"Equação do extrato": total = saldo da fatura anterior menos os '
                     "pagamentos feitos no ciclo mais as compras do ciclo. Diferenças podem vir de juros, multa ou IOF que "
                     "não aparecem como lançamento, de compras internacionais convertidas depois, ou de lançamentos "
                     "que mudaram de data.</p>")
        if c["pagamentos"]:
            p.append('<h3>Pagamentos de fatura nos últimos 8 meses (cartão e conta do mesmo banco)</h3>'
                     '<div class="tabela"><table><tr><th>Data</th><th>Onde</th><th>Descrição</th>'
                     '<th class="n">Valor</th><th>Categoria</th><th>Fatura informada (final)</th></tr>')
            for pg in c["pagamentos"]:
                fatura = f"…{pg['fatura'][-4:]}" if pg["fatura"] else "não informada"
                p.append(f'<tr><td>{_e(datas.formatar(pg["dia"], com_hora=False))}</td><td>{_e(pg["onde"])}</td>'
                         f'<td>{_e(pg["descricao"])}</td><td class="n">{_r(pg["valor"])}</td>'
                         f'<td>{_e(pg["categoria"])}</td><td>{_e(fatura)}</td></tr>')
            p.append("</table></div>")
        p.append("</details>")
        if c["parceladas"]:
            p.append('<h3>Compras parceladas (estimativa)</h3><div class="tabela"><table><tr><th>Compra parcelada</th>'
                     '<th class="n">Parcela</th><th class="n">Valor da parcela</th><th class="n">Falta pagar</th>'
                     '<th class="n">Última parcela</th><th>Situação</th></tr>')
            for parcela in c["parceladas"]:
                situacao = "cancelada (detectado automaticamente)" if parcela["cancelada"] else "em andamento"
                p.append(f'<tr><td>{_e(parcela["descricao"])}</td><td class="n">{parcela["numero"]}/{parcela["total"]}</td>'
                         f'<td class="n">{_r(parcela["valor"])}</td><td class="n">{_r(parcela["restante"])}</td>'
                         f'<td class="n">{_e(rotulo_mes(parcela["termina"]))}</td><td>{_e(situacao)}</td></tr>')
            p.append('</table></div><p class="nota">Parcelamento cancelado é detectado automaticamente quando aparece no '
                     "mesmo cartão um crédito com o valor de uma parcela (ou da compra inteira) da série. A detecção pode "
                     "errar se um crédito sem relação tiver, por acaso, o mesmo valor.</p>")
        if c["faturas"]:
            p.append('<h3>Últimas faturas fechadas</h3><div class="tabela"><table><tr><th>Fechamento</th>'
                     '<th>Vencimento</th><th class="n">Total</th></tr>')
            for f in c["faturas"]:
                vencimento = datas.formatar(f["vencimento"], com_hora=False) if f["vencimento"] else _ND
                p.append(f'<tr><td>{_e(datas.formatar(f["fechamento"], com_hora=False))}</td><td>{_e(vencimento)}</td>'
                         f'<td class="n">{_r(f["total"])}</td></tr>')
            p.append("</table></div>")
    p.append("</section>")

    # Por fora
    f, g = d["fora"], d["fora_anterior"]
    linhas_fora = (
        ("Fatura paga por fora das contas (espécie, estimado)", f["especie"], g["especie"]),
        ("Entradas de contas suas não conectadas", f["entradas_nao_monitoradas"], g["entradas_nao_monitoradas"]),
        ("Saídas para contas suas não conectadas", f["saidas_nao_monitoradas"], g["saidas_nao_monitoradas"]),
        ("Guardado na Caixinha e aplicações", f["aplicado"], g["aplicado"]),
        ("Resgatado da Caixinha e aplicações", f["resgatado"], g["resgatado"]),
        ("Caixinha: guardado menos resgatado", f["aplicado"] - f["resgatado"], g["aplicado"] - g["resgatado"]),
    )
    p.append(f'<section id="fora"><h2>Por fora das contas monitoradas</h2><div class="tabela"><table><tr><th></th>'
             f'<th class="n">{_e(nome_mes)}</th><th class="n">{_e(nome_anterior)}</th></tr>')
    for rotulo, atual, antes in linhas_fora:
        p.append(f'<tr><td>{_e(rotulo)}</td><td class="n">{_r(atual)}</td><td class="n">{_r(antes)}</td></tr>')
    p.append('</table></div><p class="nota">Nada disso entra em receitas ou despesas. "Espécie, estimado" é o que os '
             "cartões receberam de pagamento menos o que saiu das contas para pagar fatura.</p></section>")

    # Conferência com extratos
    p.append(f'<section id="extratos"><h2>Conferência com os extratos</h2><div class="tabela"><table><tr><th>Conta</th>'
             f'<th class="n">Lançamentos em {_e(rotulo_mes(mes))}</th><th class="n">Entradas</th><th class="n">Saídas</th>'
             f'<th class="n">Saldo atual</th></tr>')
    for conta in d["contas"]:
        p.append(f'<tr><td>{_e(conta["conta"])}</td><td class="n">{conta["quantidade"]}</td><td class="n">{_r(conta["entradas"])}</td>'
                 f'<td class="n">{_r(conta["saidas"])}</td><td class="n">{_r(conta["saldo_atual"])}</td></tr>')
    p.append('</table></div><p class="nota">Entradas e saídas incluem tudo (também Caixinha, faturas e transferências), '
             "para bater com o total do extrato do mês no app do banco.</p></section>")

    p.append(f'<p class="nota">{quantos(d["lancamentos_mes"], "lançamento", "lançamentos")} no mês, '
             f'{quantos(d["duvidosos_mes"], "duvidoso", "duvidosos")}. Para '
             'refinar categorias (opcional), abra <a href="revisao.html">revisao.html</a> nesta mesma pasta. Gerado em '
             f'{_e(datas.agora().strftime("%d/%m/%Y %H:%M"))}.</p></main></body></html>\n')
    return "".join(p)


# ---------------------------------------------------------------- comando

def resumo_tela(d: dict, destino: Path, estavel: Path | None = None) -> list[str]:
    """O que vai para a tela: contagens e confere/difere, sem nenhum valor."""
    saida = [f"Relatório de {rotulo_mes(d['mes'], longo=True)}: "
             f"{quantos(d['lancamentos_mes'], 'lançamento', 'lançamentos')} no mês, "
             f"{quantos(d['duvidosos_mes'], 'duvidoso', 'duvidosos')}, "
             f"{quantos(len(d['recorrentes']), 'cobrança recorrente', 'cobranças recorrentes')}"]
    if d.get("avisos_conexao"):
        saida.append(f"  AVISO: {quantos(len(d['avisos_conexao']), 'conexão', 'conexões')} com dados possivelmente "
                     "parados (veja o topo do painel e rode verificar)")
    for cartao in d["cartoes"]:
        conferencias = cartao["conferencias"]
        if not conferencias:
            saida.append(f"  {cartao['conta']}: sem faturas fechadas suficientes para conferir")
            continue
        conferem = [k for k in conferencias if k["confere"]]
        contagem = [(m, sum(1 for k in conferem if k["metodo"] == m)) for m in _METODOS]
        metodos = ", ".join(f"{m} {q}" for m, q in contagem if q) or "nenhum"
        ultima = "confere" if conferencias[0]["confere"] else "DIFERE"
        saida.append(f"  {cartao['conta']}: {len(conferem)} de {len(conferencias)} faturas fechadas conferem "
                     f"(por método: {metodos}); a última {ultima}; {cartao['com_fatura']} de {cartao['lancamentos']} "
                     "lançamentos com fatura informada pelo banco (informativo; detalhes na seção Cartões do painel)")
    if estavel:
        saida.append(f"Painel: {estavel} (o mesmo que {destino.name})")
    else:
        saida.append(f"Painel: {destino}")
    return saida


def _avisar_no_painel(perfil: str, tipo: str, mensagem: str) -> None:
    """Põe no topo do painel.html existente um aviso (sem nenhum dado) de que ele está desatualizado.
    Um aviso do mesmo tipo que já estava lá é trocado pelo novo; o próximo painel gerado sai sem aviso."""
    painel = config.pasta_relatorios(perfil) / "painel.html"
    if not painel.exists():
        return
    texto = painel.read_text(encoding="utf-8")
    inicio, fim = f"<!-- aviso:{tipo} -->", f"<!-- /aviso:{tipo} -->"
    if inicio in texto and fim in texto[texto.index(inicio):]:
        de = texto.index(inicio)
        texto = texto[:de] + texto[texto.index(fim, de) + len(fim):]
    ancora = "<main>" if "<main>" in texto else "<body>"
    if ancora not in texto:
        return
    bloco = (f'{inicio}<div style="border:1px solid #1f3b5c;border-left:4px solid #1f3b5c;background:#eef2f7;'
             f'color:#16181d;padding:12px 14px;margin:0 0 16px">{mensagem}</div>{fim}')
    temporario = painel.with_name("painel.tmp")
    temporario.write_text(texto.replace(ancora, ancora + bloco, 1), encoding="utf-8")
    os.replace(temporario, painel)


def _agora() -> str:
    return _e(datas.formatar(datas.agora().isoformat(timespec="seconds")))


def avisar_regras_nao_confirmadas(perfil: str) -> None:
    """A atualização parou porque as regras ou os ajustes mudaram e o dono ainda não confirmou."""
    _avisar_no_painel(perfil, "regras",
                      f"<b>Painel desatualizado.</b> Em {_agora()}, a atualização parou porque as regras ou os "
                      "ajustes mudaram e ainda não foram confirmados. No seu terminal, rode "
                      "<code>.\\financas.cmd categorizar --perfil real</code>, digite CONFIRMO e depois rode "
                      "<code>.\\financas.cmd relatorio --perfil real</code> (veja o README).")


def avisar_coleta_falhou(perfil: str) -> None:
    """A coleta do perfil real falhou (sem rede, autorização vencida, credencial recusada...)."""
    _avisar_no_painel(perfil, "coleta",
                      f"<b>Painel desatualizado:</b> a coleta falhou em {_agora()}; veja o mensal.log (em "
                      "<code>%USERPROFILE%\\financas-dados\\logs</code>). Para ver o status das conexões, rode "
                      "<code>.\\financas.cmd verificar --perfil real</code> no seu terminal.")


def relatorio(perfil: str, mes: str | None = None) -> int:
    hoje = datas.agora().date()
    mes = mes or ultimo_mes_fechado(hoje)
    date.fromisoformat(f"{mes}-01")  # valida AAAA-MM
    conteudos = categorizador.fotografar_regras()
    if not categorizador.liberar_regras(perfil, conteudos):
        return categorizador.SAIDA_REGRAS_NAO_CONFIRMADAS
    regras = categorizador.carregar_regras(conteudos=conteudos)
    ajustes = categorizador.carregar_ajustes(regras.categorias, conteudos=conteudos)
    con = db.conectar(perfil)
    try:
        categorizador.classificar(con, regras, ajustes)
        # Só os itens cadastrados agora: um item antigo, trocado numa reconexão, não prende o aviso de renovação.
        cadastrados = set(config.itens(perfil))
        itens = [i for i in con.execute("SELECT * FROM itens") if not cadastrados or i["id"] in cadastrados]
        linhas = con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        contas = con.execute("SELECT c.*, i.instituicao FROM contas c JOIN itens i ON i.id = c.item_id "
                             "WHERE c.substituida_a_partir IS NULL").fetchall()
        faturas = con.execute("SELECT * FROM faturas").fetchall()
    finally:
        con.close()
    dados = calcular(linhas, contas, faturas, mes, hoje)
    expira = [i["autorizacao_expira"] for i in itens if i["autorizacao_expira"]]
    dados["autorizacao_expira"] = min(expira) if expira else None
    dados["avisos_conexao"] = avisos_das_conexoes(itens)
    pasta = config.pasta_relatorios(perfil)
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"painel-{mes}.html"
    temporario = pasta / "painel.tmp"
    temporario.write_text(_montar(dados, perfil), encoding="utf-8")
    os.replace(temporario, destino)
    estavel = None
    if mes == ultimo_mes_fechado(hoje):  # painel.html é sempre o do último mês fechado
        estavel = pasta / "painel.html"
        shutil.copyfile(destino, estavel)
    print("\n".join(resumo_tela(dados, destino, estavel)))
    return 0
