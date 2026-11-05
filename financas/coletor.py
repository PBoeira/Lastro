"""Comando 'coletar': lê itens, contas, lançamentos e faturas da Pluggy e grava no SQLite.

Idempotente: cada registro é gravado pela id da Pluggy (insere ou atualiza),
então rodar duas vezes não duplica nada. Lançamentos que somem da API dentro da
janela relida (no fechamento da fatura o banco às vezes apaga e recria um
lançamento com id novo) são marcados como removidos, sem apagar a linha, e
saem das somas. Isso só acontece quando o item está sincronizado e a resposta
parece completa; caso contrário nada é marcado.

A saída na tela mostra só contagens e números mascarados. Saldos só aparecem
com --saldos.
"""

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import config, datas, db, formato, instituicoes, mascara
from .verificar import cliente_do_perfil

JANELA_DIAS = 60
DIAS_SEM_SINCRONIZAR = 7
CONVENCOES = ("positivo_e_gasto", "negativo_e_gasto")
_ENTRADAS_CARTAO = {"PAGAMENTO_FATURA", "ESTORNO", "CASHBACK"}
_TEXTO_ENTRADA_CARTAO = re.compile(r"(?i)pagamento|estorno|cashback|cr[eé]dito")
_AMOSTRA_MINIMA_SINAL = 5

COLUNAS_TRANSACAO = (
    "conta_id", "dia", "data_utc", "descricao", "descricao_bruta", "valor_api_centavos", "moeda",
    "tipo_api", "status", "tipo_operacao", "metodo_pagamento", "categoria_api", "categoria_api_id",
    "parcela_numero", "parcela_total", "parcela_valor_total_centavos", "data_compra", "fatura_id",
    "contraparte_nome", "contraparte_doc", "contraparte_mesmo_titular", "comerciante",
    "comerciante_cnpj", "comerciante_categoria", "saldo_apos_centavos", "provider_id",
    "criado_api", "atualizado_api",
)


# ---------------------------------------------------------------- conversões

def centavos(valor) -> int | None:
    if valor is None:
        return None
    return int((Decimal(str(valor)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def so_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def _conta_sem_agencia(numero) -> str:
    """'0001/12345-0' vira '123450' (só a conta, sem agência)."""
    return so_digitos(str(numero or "").rsplit("/", 1)[-1])


def identidades_proprias(contas: list[dict]) -> tuple[set[str], set[str]]:
    """CPFs/CNPJs dos titulares e números das contas próprias (só em memória, nunca gravados)."""
    documentos = {so_digitos(c.get("taxNumber")) for c in contas} - {""}
    numeros: set[str] = set()
    for conta in contas:
        if conta.get("type") != "BANK":
            continue
        for bruto in (conta.get("number"), (conta.get("bankData") or {}).get("transferNumber")):
            for candidato in (so_digitos(bruto), _conta_sem_agencia(bruto)):
                if len(candidato) >= 5:
                    numeros.add(candidato)
    return documentos, numeros


def _contraparte(transacao: dict, documentos: set[str], numeros: set[str]):
    dados = transacao.get("paymentData") or {}
    lado = dados.get("receiver") if transacao.get("type") == "DEBIT" else dados.get("payer")
    if not isinstance(lado, dict):
        return None, None, None
    documento = lado.get("documentNumber") or {}
    tipo_doc = str(documento.get("type") or "").upper()
    digitos = so_digitos(documento.get("value"))
    mesmo_titular = None
    if digitos:
        mesmo_titular = 1 if digitos in documentos else 0
    conta = _conta_sem_agencia(lado.get("accountNumber"))
    if conta and conta in numeros:
        mesmo_titular = 1
    # CNPJ (empresa) fica completo; o resto, só os 2 últimos dígitos. O tipo vem do número de
    # dígitos, não do que a API declara: um CPF declarado como "CNPJ" não pode ser gravado inteiro.
    if len(digitos) == 14:
        texto_doc = f"CNPJ {digitos}"
    elif digitos:
        texto_doc = f"{'CPF' if len(digitos) == 11 else 'DOC'} ••{digitos[-2:]}"
    else:
        texto_doc = None
    return lado.get("name"), texto_doc, mesmo_titular


def valor_na_moeda_da_conta(transacao: dict) -> int:
    """Compra internacional: 'amount' vem na moeda da compra e 'amountInAccountCurrency', na moeda da
    conta (reais). Usa o valor em reais quando vier, com o sinal de 'amount'."""
    original = centavos(transacao["amount"])
    convertido = centavos(transacao.get("amountInAccountCurrency"))
    if convertido is None:
        return original
    return -abs(convertido) if original < 0 else abs(convertido)


def sem_conversao(transacao: dict) -> bool:
    """Lançamento em moeda estrangeira sem o valor em reais: entra com o valor da moeda original."""
    moeda = transacao.get("currencyCode")
    return bool(moeda) and moeda != "BRL" and transacao.get("amountInAccountCurrency") is None


def linha_transacao(transacao: dict, conta_id: str, documentos: set[str], numeros: set[str]) -> dict:
    cartao = transacao.get("creditCardMetadata") or {}
    comerciante = transacao.get("merchant") or {}
    nome, doc, mesmo = _contraparte(transacao, documentos, numeros)
    # Textos livres do banco passam por mascara.texto ANTES de gravar: descrições de Pix e TED
    # costumam trazer agência, conta e parte do CPF da contraparte.
    return {
        "conta_id": conta_id,
        "dia": datas.dia(transacao["date"]),
        "data_utc": transacao["date"],
        "descricao": mascara.texto(transacao.get("description")),
        "descricao_bruta": mascara.texto(transacao.get("descriptionRaw")),
        "valor_api_centavos": valor_na_moeda_da_conta(transacao),
        "moeda": transacao.get("currencyCode"),
        "tipo_api": transacao.get("type"),
        "status": transacao.get("status"),
        "tipo_operacao": transacao.get("operationType"),
        "metodo_pagamento": (transacao.get("paymentData") or {}).get("paymentMethod"),
        "categoria_api": transacao.get("category"),
        "categoria_api_id": transacao.get("categoryId"),
        "parcela_numero": cartao.get("installmentNumber"),
        "parcela_total": cartao.get("totalInstallments"),
        "parcela_valor_total_centavos": centavos(cartao.get("totalAmount")),
        "data_compra": datas.dia(cartao.get("purchaseDate") or cartao.get("transactionDateTime")),
        "fatura_id": cartao.get("billId"),
        "contraparte_nome": mascara.texto(nome),
        "contraparte_doc": doc,
        "contraparte_mesmo_titular": mesmo,
        "comerciante": mascara.texto(comerciante.get("businessName") or comerciante.get("name")),
        "comerciante_cnpj": so_digitos(comerciante.get("cnpj")) if len(so_digitos(comerciante.get("cnpj"))) == 14 else None,
        "comerciante_categoria": comerciante.get("category"),
        "saldo_apos_centavos": centavos(transacao.get("balance")),
        "provider_id": None,  # não usado pelo programa: por minimização, não é gravado
        "criado_api": transacao.get("createdAt"),
        "atualizado_api": transacao.get("updatedAt"),
    }


def detectar_convencao(transacoes: list[dict]) -> str | None:
    """Cartões: descobre se compra vem positiva (o que diz a documentação) ou negativa."""
    positivos = negativos = 0
    for t in transacoes:
        if t.get("operationType") in _ENTRADAS_CARTAO or _TEXTO_ENTRADA_CARTAO.search(t.get("description") or ""):
            continue
        valor = t.get("amount") or 0
        positivos += valor > 0
        negativos += valor < 0
    if positivos + negativos < _AMOSTRA_MINIMA_SINAL:
        return None
    return "negativo_e_gasto" if negativos > positivos else "positivo_e_gasto"


def item_confiavel(item: dict) -> tuple[bool, str | None]:
    """Só marcamos lançamentos como removidos se o item acabou de sincronizar com sucesso."""
    expira = item.get("consentExpiresAt")
    if expira and datas.ler_iso(expira) <= datas.agora():
        return False, "autorização vencida: renove no dashboard (Demo, Connect Account, MeuPluggy)"
    if item.get("status") != "UPDATED":
        return False, f"item com status {mascara.texto(item.get('status'), 40)}"
    if item.get("executionStatus") not in ("SUCCESS", "PARTIAL_SUCCESS"):
        return False, f"última execução {mascara.texto(item.get('executionStatus'), 40)}"
    detalhe = item.get("statusDetail") or {}
    if isinstance(detalhe.get("transactions"), dict) and detalhe["transactions"].get("isUpdated") is False:
        return False, "lançamentos não atualizados na última sincronização"
    return True, None


def sincronizacao_atrasada(ultima_sincronizacao: str | None, referencia: str) -> str | None:
    """Aviso quando o banco não sincroniza há mais de DIAS_SEM_SINCRONIZAR dias antes da coleta: a
    coleta dá certo, mas os dados ficam parados naquela data."""
    if not ultima_sincronizacao:
        return "o banco ainda não informou nenhuma sincronização"
    dias = (datas.ler_iso(referencia) - datas.ler_iso(ultima_sincronizacao)).days
    if dias > DIAS_SEM_SINCRONIZAR:
        return f"o banco não sincroniza há {dias} dias (desde {datas.formatar(ultima_sincronizacao, com_hora=False)})"
    return None


def inicio_da_janela(hoje: date, *datas_anteriores: str | None) -> str:
    """Relê os últimos JANELA_DIAS dias ou, se a última coleta boa (ou a última sincronização do banco)
    for mais antiga, desde 7 dias antes dela: coletas que falham em sequência não deixam buraco."""
    inicio = hoje - timedelta(days=JANELA_DIAS)
    for texto in datas_anteriores:
        if texto:
            inicio = min(inicio, date.fromisoformat(datas.dia(texto)) - timedelta(days=7))
    return inicio.isoformat()


def marcar_contas_substituidas(con, ids_itens: list[str]) -> int:
    """Uma reconexão pode criar um item novo, com contas novas e o histórico entregue de novo. A conta de
    um item que saiu do config.local.json e tem uma equivalente num item ativo (mesma instituição, tipo,
    subtipo e final) ganha a data do primeiro lançamento da nova: a view 'lancamentos' usa a antiga só
    antes dessa data, e o histórico não conta em dobro. Devolve quantas contas foram marcadas agora."""
    marcas = ", ".join("?" * len(ids_itens))
    contar = "SELECT COUNT(*) FROM contas WHERE substituida_a_partir IS NOT NULL"
    with con:
        con.execute(f"UPDATE contas SET substituida_a_partir = NULL WHERE item_id IN ({marcas})", ids_itens)
        antes = con.execute(contar).fetchone()[0]
        con.execute(
            f"""UPDATE contas SET substituida_a_partir = (
                    SELECT MIN(t.dia) FROM contas n
                    JOIN itens ni ON ni.id = n.item_id
                    JOIN transacoes t ON t.conta_id = n.id AND t.removido_em IS NULL
                    WHERE n.item_id IN ({marcas}) AND n.tipo = contas.tipo AND n.subtipo IS contas.subtipo
                      AND n.numero_final IS contas.numero_final
                      AND ni.instituicao IS (SELECT i.instituicao FROM itens i WHERE i.id = contas.item_id))
                WHERE item_id NOT IN ({marcas}) AND substituida_a_partir IS NULL""",
            ids_itens * 2,
        )
        return con.execute(contar).fetchone()[0] - antes


# ---------------------------------------------------------------- gravação

def gravar_item(con, item: dict, agora: str, instituicao: str | None = None) -> None:
    conector = item.get("connector") or {}
    con.execute(
        """INSERT INTO itens (id, conector, instituicao, sandbox, open_finance, status, status_execucao,
                              ultima_sincronizacao, autorizacao_expira, visto_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET conector=excluded.conector,
               instituicao=COALESCE(excluded.instituicao, itens.instituicao), sandbox=excluded.sandbox,
               open_finance=excluded.open_finance, status=excluded.status,
               status_execucao=excluded.status_execucao, ultima_sincronizacao=excluded.ultima_sincronizacao,
               autorizacao_expira=excluded.autorizacao_expira, visto_em=excluded.visto_em""",
        (item["id"], mascara.texto(conector.get("name"), 80), mascara.texto(instituicao, 80),
         int(bool(conector.get("isSandbox"))),
         int(bool(conector.get("isOpenFinance"))), item.get("status"), item.get("executionStatus"),
         item.get("lastUpdatedAt"), item.get("consentExpiresAt"), agora),
    )


def gravar_conta(con, conta: dict, item_id: str, agora: str) -> None:
    credito = conta.get("creditData") or {}
    digitos = so_digitos(conta.get("number"))
    con.execute(
        """INSERT INTO contas (id, item_id, tipo, subtipo, nome, numero_final, moeda, saldo_centavos,
                               limite_centavos, limite_disponivel_centavos, fechamento, vencimento, visto_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id, tipo=excluded.tipo,
               subtipo=excluded.subtipo, nome=excluded.nome, numero_final=excluded.numero_final,
               moeda=excluded.moeda, saldo_centavos=excluded.saldo_centavos,
               limite_centavos=excluded.limite_centavos,
               limite_disponivel_centavos=excluded.limite_disponivel_centavos,
               fechamento=excluded.fechamento, vencimento=excluded.vencimento, visto_em=excluded.visto_em""",
        (conta["id"], item_id, conta.get("type"), conta.get("subtype"),
         mascara.texto(conta.get("marketingName") or conta.get("name"), 80), digitos[-4:] if digitos else None,
         conta.get("currencyCode"), centavos(conta.get("balance")), centavos(credito.get("creditLimit")),
         centavos(credito.get("availableCreditLimit")), datas.dia(credito.get("balanceCloseDate")),
         datas.dia(credito.get("balanceDueDate")), agora),
    )
    con.execute(
        """INSERT INTO saldos_diarios (conta_id, dia, saldo_centavos) VALUES (?, ?, ?)
           ON CONFLICT(conta_id, dia) DO UPDATE SET saldo_centavos=excluded.saldo_centavos""",
        (conta["id"], agora[:10], centavos(conta.get("balance"))),
    )


def definir_convencao(con, conta_id: str, transacoes: list[dict], forcada: str | None) -> str:
    """Convenção de sinal de um cartão: a do config.local.json, senão a já gravada, senão a detectada."""
    if forcada:
        convencao = forcada
    else:
        gravada = con.execute("SELECT convencao_sinal FROM contas WHERE id = ?", (conta_id,)).fetchone()[0]
        convencao = gravada or detectar_convencao(transacoes)
    if convencao:
        con.execute("UPDATE contas SET convencao_sinal = ? WHERE id = ?", (convencao, conta_id))
    return convencao or "positivo_e_gasto (padrão da documentação; amostra pequena)"


def sincronizar_transacoes(con, conta_id: str, transacoes: list[dict], data_de: str | None,
                           confiavel: bool, documentos: set[str], numeros: set[str], agora: str) -> dict:
    atuais = {
        linha["id"]: linha
        for linha in con.execute(
            f"SELECT id, removido_em, {', '.join(COLUNAS_TRANSACAO)} FROM transacoes WHERE conta_id = ?",
            (conta_id,),
        )
    }
    novos = alterados = removidos = 0
    aviso = None
    estrangeiros = sum(1 for t in transacoes if sem_conversao(t))
    recebidos: set[str] = set()
    colunas = ", ".join(("id",) + COLUNAS_TRANSACAO + ("primeira_vez_em", "visto_em"))
    marcadores = ", ".join("?" * (len(COLUNAS_TRANSACAO) + 3))
    atualizacoes = ", ".join(f"{c}=excluded.{c}" for c in COLUNAS_TRANSACAO)
    sql = (
        f"INSERT INTO transacoes ({colunas}) VALUES ({marcadores}) "
        f"ON CONFLICT(id) DO UPDATE SET {atualizacoes}, visto_em=excluded.visto_em, removido_em=NULL"
    )
    with con:
        for transacao in transacoes:
            linha = linha_transacao(transacao, conta_id, documentos, numeros)
            ident = transacao["id"]
            recebidos.add(ident)
            antiga = atuais.get(ident)
            if antiga is None:
                novos += 1
            elif antiga["removido_em"] is not None or any(antiga[c] != linha[c] for c in COLUNAS_TRANSACAO):
                alterados += 1
            con.execute(sql, (ident, *(linha[c] for c in COLUNAS_TRANSACAO), agora, agora))

        if confiavel:
            limite = None if data_de is None else (date.fromisoformat(data_de) + timedelta(days=1)).isoformat()
            sumidos = [
                ident for ident, linha in atuais.items()
                if linha["removido_em"] is None and ident not in recebidos
                and (limite is None or linha["data_utc"] >= limite)
            ]
            if sumidos and not recebidos:
                aviso = "a API não devolveu lançamentos; nada foi marcado como removido"
            elif len(sumidos) > max(10, len(recebidos)):
                aviso = f"{len(sumidos)} lançamentos sumiram de uma vez; nada foi marcado como removido"
            else:
                for ident in sumidos:
                    con.execute("UPDATE transacoes SET removido_em = ? WHERE id = ?", (agora, ident))
                removidos = len(sumidos)
    return {"recebidos": len(recebidos), "novos": novos, "alterados": alterados,
            "removidos": removidos, "aviso": aviso, "sem_conversao": estrangeiros}


def gravar_faturas(con, conta_id: str, faturas: list[dict], agora: str) -> int:
    with con:
        for fatura in faturas:
            encargos = fatura.get("financeCharges") or []
            iof = sum(centavos(e.get("amount")) or 0 for e in encargos if e.get("type") == "IOF")
            outros = sum(centavos(e.get("amount")) or 0 for e in encargos if e.get("type") != "IOF")
            pagos = sum(centavos(p.get("amount")) or 0 for p in fatura.get("payments") or [])
            con.execute(
                """INSERT INTO faturas (id, conta_id, vencimento, fechamento, total_centavos, moeda,
                                        minimo_centavos, iof_centavos, encargos_centavos,
                                        pagamentos_centavos, visto_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET conta_id=excluded.conta_id, vencimento=excluded.vencimento,
                       fechamento=excluded.fechamento, total_centavos=excluded.total_centavos,
                       moeda=excluded.moeda, minimo_centavos=excluded.minimo_centavos,
                       iof_centavos=excluded.iof_centavos, encargos_centavos=excluded.encargos_centavos,
                       pagamentos_centavos=excluded.pagamentos_centavos, visto_em=excluded.visto_em""",
                (fatura["id"], conta_id, datas.data_de_calendario(fatura.get("dueDate")),
                 datas.data_de_calendario(fatura.get("billClosingDate")),
                 centavos(fatura.get("totalAmount")), fatura.get("totalAmountCurrencyCode"),
                 centavos(fatura.get("minimumPaymentAmount")), iof, outros, pagos, agora),
            )
    return len(faturas)


def conferir_saldo(con, conta_id: str, saldo_centavos: int | None) -> str:
    """Compara o saldo da conta com o 'saldo após' do lançamento mais recente, quando o banco informa."""
    linha = con.execute(
        """SELECT saldo_apos_centavos FROM transacoes
           WHERE conta_id = ? AND removido_em IS NULL AND status = 'POSTED' AND saldo_apos_centavos IS NOT NULL
           ORDER BY data_utc DESC LIMIT 1""",
        (conta_id,),
    ).fetchone()
    if linha is None or saldo_centavos is None:
        return "sem conferência automática (o banco não informa saldo por lançamento)"
    return "confere com o último lançamento" if linha[0] == saldo_centavos else "DIFERE do último lançamento"


# ---------------------------------------------------------------- comando

_NOMES_TIPO = {"CHECKING_ACCOUNT": "Conta corrente", "SAVINGS_ACCOUNT": "Poupança", "CREDIT_CARD": "Cartão"}


def coletar(perfil: str, desde: str | None = None, mostrar_saldos: bool = False) -> int:
    ids_itens = config.itens(perfil)
    if not ids_itens:
        print(f"Nenhum item cadastrado no perfil '{perfil}'. Rode: .\\financas.cmd itens --perfil {perfil}")
        return 1
    if desde:
        date.fromisoformat(desde)  # valida AAAA-MM-DD
    sinais_forcados = config.carregar()["perfis"].get(perfil, {}).get("sinal_cartao", {})
    cliente = cliente_do_perfil(perfil)
    con = db.conectar(perfil)
    agora = datas.agora().isoformat(timespec="seconds")
    hoje = datas.agora().date()
    ultima_boa = con.execute("SELECT MAX(inicio) FROM execucoes WHERE ok = 1").fetchone()[0]
    execucao = con.execute("INSERT INTO execucoes (inicio) VALUES (?)", (agora,)).lastrowid
    con.commit()
    totais = {"novos": 0, "alterados": 0, "removidos": 0, "avisos": 0}
    try:
        itens, contas_por_item = {}, {}
        for item_id in ids_itens:
            itens[item_id] = cliente.item(item_id)
            if perfil == "teste" and (itens[item_id].get("connector") or {}).get("isSandbox") is not True:
                # o perfil teste não tem as proteções do real: só aceita o Sandbox da Pluggy
                raise config.LocalInseguro(f"O item {mascara.final(item_id)} não é do Sandbox da Pluggy; "
                                           "o perfil teste só aceita itens do Sandbox. Nada foi gravado.")
            contas_por_item[item_id] = cliente.contas(item_id)
        documentos, numeros = identidades_proprias([c for cs in contas_por_item.values() for c in cs])

        for item_id, contas in contas_por_item.items():
            item = itens[item_id]
            instituicao = instituicoes.nome(item, contas)
            anterior = con.execute("SELECT ultima_sincronizacao FROM itens WHERE id = ?", (item_id,)).fetchone()
            sincronizado_antes = anterior[0] if anterior else None
            with con:
                gravar_item(con, item, agora, instituicao)
            confiavel, motivo = item_confiavel(item)
            status = f"{mascara.texto(item.get('status'), 40)} ({mascara.texto(item.get('executionStatus'), 40)})"
            print(f"Item {mascara.final(item_id)}  {instituicao}  {status}")
            if motivo:
                print(f"  AVISO: {motivo}. Lançamentos sumidos não serão marcados como removidos.")
                totais["avisos"] += 1
            atraso = sincronizacao_atrasada(item.get("lastUpdatedAt"), agora)
            if atraso:
                print(f"  AVISO: {atraso}; os dados desse banco podem estar parados. Veja \"Se der erro\" "
                      "no README.")
                totais["avisos"] += 1

            for conta in contas:
                conta_id = conta["id"]
                with con:
                    gravar_conta(con, conta, item_id, agora)
                ja_tem = con.execute("SELECT COUNT(*) FROM transacoes WHERE conta_id = ?", (conta_id,)).fetchone()[0]
                data_de = desde or (inicio_da_janela(hoje, ultima_boa, sincronizado_antes) if ja_tem else None)
                transacoes = cliente.transacoes(conta_id, data_de)
                resultado = sincronizar_transacoes(con, conta_id, transacoes, data_de, confiavel,
                                                   documentos, numeros, agora)
                for chave in ("novos", "alterados", "removidos"):
                    totais[chave] += resultado[chave]

                rotulo = _NOMES_TIPO.get(conta.get("subtype")) or mascara.texto(
                    conta.get("subtype") or conta.get("type"), 40)
                janela = f"desde {datas.formatar(data_de, com_hora=False)}" if data_de else "histórico completo"
                linha = (f"  {rotulo} {mascara.numero(conta.get('number'))}: {resultado['recebidos']} lançamentos "
                         f"({janela}): {resultado['novos']} novos, {resultado['alterados']} alterados, "
                         f"{resultado['removidos']} removidos")
                if conta.get("type") == "CREDIT":
                    numero_final = so_digitos(conta.get("number"))[-4:]
                    forcada = sinais_forcados.get(numero_final)
                    if forcada and forcada not in CONVENCOES:
                        raise ValueError(f"sinal_cartao inválido no config.local.json: use {' ou '.join(CONVENCOES)}")
                    with con:
                        convencao = definir_convencao(con, conta_id, transacoes, forcada)
                    faturas = gravar_faturas(con, conta_id, cliente.faturas(conta_id), agora)
                    linha += f"; {faturas} faturas; sinal: {convencao}"
                else:
                    linha += f"; saldo {conferir_saldo(con, conta_id, centavos(conta.get('balance')))}"
                if mostrar_saldos:
                    linha += f"; saldo atual {formato.reais(centavos(conta.get('balance')))}"
                print(linha)
                if resultado["aviso"]:
                    print(f"  AVISO: {resultado['aviso']}.")
                    totais["avisos"] += 1
                if resultado["sem_conversao"]:
                    print(f"  AVISO: {resultado['sem_conversao']} lançamentos em moeda estrangeira vieram sem o "
                          "valor em reais e entram com o valor da moeda original.")
                    totais["avisos"] += 1

        substituidas = marcar_contas_substituidas(con, ids_itens)
        if substituidas:
            print(f"{substituidas} contas de itens antigos foram ligadas às contas equivalentes dos itens novos: "
                  "o histórico não conta em dobro.")

        resumo =(f"{totais['novos']} novos, {totais['alterados']} alterados, {totais['removidos']} removidos, "
                  f"{totais['avisos']} avisos")
        with con:
            con.execute("UPDATE execucoes SET fim = ?, ok = 1, resumo = ? WHERE id = ?",
                        (datas.agora().isoformat(timespec="seconds"), resumo, execucao))
        print(f"Coleta concluída: {resumo}.")
        return 0
    except BaseException:
        with con:
            con.execute("UPDATE execucoes SET fim = ?, ok = 0 WHERE id = ?",
                        (datas.agora().isoformat(timespec="seconds"), execucao))
        raise
    finally:
        con.close()
