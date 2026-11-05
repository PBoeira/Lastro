"""Testes do relatório com dados sintéticos em vários meses (nada de rede, nada real)."""

import unittest
from datetime import date, datetime
from pathlib import Path

from financas import categorizador, coletor, datas, db, formato, relatorio

AGORA = "2026-11-01T09:00:00-03:00"
HOJE = date(2026, 11, 1)
CPF_EU = "123.456.789-09"


def tr(ident, valor, descricao, dia, **extra):
    return {"id": ident, "date": f"{dia}T15:00:00.000Z", "amount": valor, "type": "DEBIT" if valor < 0 else "CREDIT",
            "description": descricao, "status": "POSTED", "currencyCode": "BRL", **extra}


def parcela(ident, numero, dia):
    return tr(ident, 50.0, "LOJA X", dia, creditCardMetadata={"installmentNumber": numero, "totalInstallments": 6,
                                                               "purchaseDate": "2026-08-20"})


CONTA = {"id": "conta", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "number": "55443322-1", "balance": 2000.0,
         "taxNumber": CPF_EU, "bankData": {"transferNumber": "260/0001/55443322-1"}}
CARTAO = {"id": "cartao", "type": "CREDIT", "subtype": "CREDIT_CARD", "number": "7702", "balance": 1000.0,
          "taxNumber": CPF_EU, "creditData": {"creditLimit": 5000}}

LANCAMENTOS = {
    "conta": [
        tr("s1", 1200.0, "Bolsa de estágio - CIEE", "2026-09-05"),
        tr("s2", -300.0, "SUPERMERCADO EXEMPLO", "2026-09-12"),
        tr("o1", 1200.0, "Bolsa de estágio - CIEE", "2026-10-05"),
        tr("o2", -100.0, "Transferência enviada pelo Pix - FULANO", "2026-10-08", operationType="PIX"),
        tr("o3", -80.0, "Dinheiro guardado", "2026-10-09"),
        tr("o4", 30.0, "Dinheiro resgatado", "2026-10-11"),
    ],
    "cartao": [
        tr("a1", 39.9, "NETFLIX.COM", "2026-08-10"), parcela("p1", 1, "2026-08-20"),
        tr("b1", 39.9, "NETFLIX.COM", "2026-09-10"), tr("b2", 200.1, "IFOOD *PEDIDO", "2026-09-20"),
        parcela("p2", 2, "2026-09-20"), tr("b3", 160.0, "UBER *TRIP", "2026-10-01"),
        tr("c1", 44.9, "NETFLIX.COM", "2026-10-10"), tr("c2", 100.0, "IFOOD *PEDIDO", "2026-10-15"),
        parcela("p3", 3, "2026-10-20"),
        tr("c3", -450.0, "Pagamento recebido", "2026-10-25", operationType="PAGAMENTO_FATURA"),
    ],
}
FATURAS = [
    {"id": "f1", "dueDate": "2026-09-10T03:00:00.000Z", "billClosingDate": "2026-09-03T03:00:00.000Z", "totalAmount": 500.0},
    {"id": "f2", "dueDate": "2026-10-10T03:00:00.000Z", "billClosingDate": "2026-10-03T03:00:00.000Z", "totalAmount": 450.0},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.con = db.conectar_arquivo(":memory:")
        coletor.gravar_item(self.con, {"id": "item", "status": "UPDATED", "connector": {"name": "MeuPluggy"}}, AGORA,
                            "Nubank (via MeuPluggy)")
        for conta in (CONTA, CARTAO):
            coletor.gravar_conta(self.con, conta, "item", AGORA)
        self.con.commit()
        documentos, numeros = coletor.identidades_proprias([CONTA, CARTAO])
        for conta_id, lista in LANCAMENTOS.items():
            coletor.sincronizar_transacoes(self.con, conta_id, lista, None, True, documentos, numeros, AGORA)
        with self.con:
            coletor.definir_convencao(self.con, "cartao", [], "positivo_e_gasto")
        coletor.gravar_faturas(self.con, "cartao", FATURAS, AGORA)
        regras = categorizador.carregar_regras(caminho_local=None)  # só as genéricas
        categorizador.classificar(self.con, regras, {})
        linhas = self.con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        contas = self.con.execute("SELECT c.*, i.instituicao FROM contas c JOIN itens i ON i.id = c.item_id").fetchall()
        faturas = self.con.execute("SELECT * FROM faturas").fetchall()
        self.d = relatorio.calcular(linhas, contas, faturas, "2026-10", HOJE)

    def tearDown(self):
        self.con.close()


class TesteCalculos(Base):
    def test_ultimo_mes_fechado(self):
        self.assertEqual(relatorio.ultimo_mes_fechado(HOJE), "2026-10")
        self.assertEqual(relatorio.ultimo_mes_fechado(date(2027, 1, 15)), "2026-12")

    def test_totais_do_mes(self):
        a = self.d["atual"]
        self.assertEqual(a["receitas"], 120000)
        # uber 160 + netflix 44,90 + ifood 100 + parcela 50 + pix 100 (Caixinha e fatura ficam de fora)
        self.assertEqual(a["despesas"], 16000 + 4490 + 10000 + 5000 + 10000)
        self.assertEqual(a["saldo"], 120000 - 45490)
        self.assertAlmostEqual(a["taxa"], (120000 - 45490) / 120000)

    def test_fora_das_contas(self):
        f = self.d["fora"]
        self.assertEqual((f["pago_no_cartao"], f["pago_pelas_contas"], f["especie"]), (45000, 0, 45000))
        self.assertEqual((f["aplicado"], f["resgatado"]), (8000, 3000))

    def test_recorrente_que_subiu(self):
        netflix = [r for r in self.d["recorrentes"] if "NETFLIX" in r["descricao"]]
        self.assertEqual(len(netflix), 1)
        self.assertEqual((netflix[0]["atual"], netflix[0]["anterior"], netflix[0]["subiu"]), (4490, 3990, True))
        self.assertFalse([r for r in self.d["recorrentes"] if "LOJA X" in r["descricao"]])  # parcela não é assinatura

    def test_cartao_fatura_atual_e_parcelas(self):
        cartao = self.d["cartoes"][0]
        self.assertEqual(cartao["limite_utilizado"], 100000)
        self.assertEqual(cartao["fatura_atual"], 4490 + 10000 + 5000)  # depois do fechamento de 03/10
        self.assertEqual((cartao["pagos_desde"], cartao["fechada_em_aberto"], cartao["adiantado"]), (45000, 0, 0))
        self.assertEqual(cartao["parcelas_pelo_saldo"], 100000 - 19490)
        self.assertEqual(cartao["parcelas_pelas_compras"], 3 * 5000)
        parcelada = cartao["parceladas"][0]
        self.assertEqual((parcelada["numero"], parcelada["total"], parcelada["termina"]), (3, 6, "2027-01"))

    def test_conferencia_da_fatura_fechada(self):
        conferencia = self.d["cartoes"][0]["conferencias"][0]
        # ciclo 04/09 a 03/10: netflix 39,90 + ifood 200,10 + parcela 50 + uber 160 = 450
        self.assertEqual((conferencia["compras"], conferencia["total"], conferencia["confere"]), (45000, 45000, True))
        self.assertEqual(conferencia["metodo"], "compras do ciclo")

    def test_conferencia_pela_equacao_do_extrato(self):
        # fatura anterior de 500, pagamento adiantado de 300 no ciclo, compras de 400: total 600
        lancamentos = [
            {"dia": "2026-09-10", "valor_centavos": -40000, "natureza": "despesa", "categoria": "Compras"},
            {"dia": "2026-09-15", "valor_centavos": 30000, "natureza": "neutro", "categoria": "Pagamento de fatura"},
        ]
        k = relatorio.conferir_ciclo(lancamentos, {"fechamento": "2026-09-03", "total": 50000},
                                     {"fechamento": "2026-10-03", "total": 60000})
        self.assertTrue(k["confere"])
        self.assertEqual((k["metodo"], k["diferenca"]), ("equação do extrato", 0))

    def test_conferencia_com_compra_no_dia_do_fechamento(self):
        lancamentos = [
            {"dia": "2026-09-03", "valor_centavos": -10000, "natureza": "despesa", "categoria": "Compras"},
            {"dia": "2026-09-20", "valor_centavos": -20000, "natureza": "despesa", "categoria": "Compras"},
            {"dia": "2026-10-03", "valor_centavos": -5000, "natureza": "despesa", "categoria": "Compras"},
        ]
        k = relatorio.conferir_ciclo(lancamentos, {"fechamento": "2026-09-03", "total": 0},
                                     {"fechamento": "2026-10-03", "total": 30000})
        self.assertTrue(k["confere"])  # 03/09 entra neste ciclo, 03/10 vai para o seguinte
        self.assertEqual(k["fronteira"], "fechamento no ciclo seguinte")

    def test_conferencia_pela_fatura_informada(self):
        # a parcela caiu depois do fechamento por data, mas o banco diz que é desta fatura
        lancamentos = [
            {"dia": "2026-09-20", "valor_centavos": -20000, "natureza": "despesa", "categoria": "Compras", "fatura_id": "f2"},
            {"dia": "2026-10-05", "valor_centavos": -5000, "natureza": "despesa", "categoria": "Compras", "fatura_id": "f2"},
            {"dia": "2026-09-25", "valor_centavos": -7000, "natureza": "despesa", "categoria": "Compras", "fatura_id": "f3"},
        ]
        k = relatorio.conferir_ciclo(lancamentos, {"id": "f1", "fechamento": "2026-09-03", "total": 0},
                                     {"id": "f2", "fechamento": "2026-10-03", "total": 25000})
        self.assertTrue(k["confere"])
        self.assertEqual(k["metodo"], "fatura informada pelo banco")

    def test_conferencia_com_pagamento_pela_fatura_informada(self):
        # o pagamento caiu antes do fechamento por data, mas o banco diz que ele é desta fatura
        lancamentos = [
            {"dia": "2026-09-20", "valor_centavos": -40000, "natureza": "despesa", "categoria": "Compras", "fatura_id": "f2"},
            {"dia": "2026-09-02", "valor_centavos": 30000, "natureza": "neutro", "categoria": "Pagamento de fatura",
             "fatura_id": "f2"},
        ]
        # só fecha com o pagamento atribuído pela fatura: 500 - 300 + 400 = 600
        k = relatorio.conferir_ciclo(lancamentos, {"id": "f1", "fechamento": "2026-09-03", "total": 50000},
                                     {"id": "f2", "fechamento": "2026-10-03", "total": 60000})
        self.assertTrue(k["confere"])
        self.assertEqual(k["metodo"], "equação com pagamentos pela fatura informada")

    def test_diferenca_real_continua_aparecendo(self):
        lancamentos = [{"dia": "2026-09-20", "valor_centavos": -20000, "natureza": "despesa", "categoria": "Compras"}]
        k = relatorio.conferir_ciclo(lancamentos, {"fechamento": "2026-09-03", "total": 0},
                                     {"fechamento": "2026-10-03", "total": 35000})
        self.assertFalse(k["confere"])

    def test_proxima_fatura_e_pagamentos(self):
        cartao = self.d["cartoes"][0]
        self.assertEqual(cartao["proxima"], 4490 + 10000 + 5000)
        self.assertEqual((cartao["pagos_cartao"], cartao["pagos_conta"]), (45000, 0))

    def test_taxa_ampliada(self):
        resultado = relatorio._taxa_ampliada({"receitas": 100000, "despesas": 150000},
                                             {"entradas_nao_monitoradas": 200000, "saidas_nao_monitoradas": 50000})
        self.assertEqual(resultado["recursos"], 250000)
        self.assertAlmostEqual(resultado["taxa"], 0.4)
        self.assertIsNone(relatorio._taxa_ampliada({"receitas": 0, "despesas": 1},
                                                   {"entradas_nao_monitoradas": 0, "saidas_nao_monitoradas": 0})["taxa"])

    def test_passo_do_eixo(self):
        self.assertEqual(relatorio._passo(2500000), 1000000)  # R$ 25 mil: passo de R$ 10 mil
        self.assertEqual(relatorio._passo(120000), 50000)


def _parcela(numero, total=12, descricao="LOJA EXEMPLO", dia="2026-10-20", metadados=True):
    return {"valor_centavos": -5000, "dia": dia, "mes": dia[:7], "descricao": descricao, "conta_tipo": "CREDIT",
            "parcela_numero": numero if metadados else None, "parcela_total": total if metadados else None}


class TesteParcelas(unittest.TestCase):
    def test_duas_series_iguais_em_fases_diferentes(self):
        series = relatorio._parceladas([_parcela(1), _parcela(3)], "2026-11-01")
        self.assertEqual(sorted((s["numero"], s["restante"]) for s in series), [(1, 11 * 5000), (3, 9 * 5000)])

    def test_serie_partida_conta_uma_vez(self):
        # mesma série, descrições diferentes: 1/12 lançada em agosto e 3/12 em outubro (mesmo mês final)
        series = relatorio._parceladas([_parcela(1, descricao="COMPRA EXEMPLO", dia="2026-08-20"),
                                        _parcela(3, descricao="PARCELAMENTO EXEMPLO", dia="2026-10-20")], "2026-11-01")
        self.assertEqual([(s["numero"], s["restante"]) for s in series], [(3, 9 * 5000)])

    def test_series_diferentes_no_mesmo_numero_ficam(self):
        series = relatorio._parceladas([_parcela(2, descricao="LOJA UM"), _parcela(2, descricao="LOJA DOIS")], "2026-11-01")
        self.assertEqual(len(series), 2)

    def test_serie_estornada_sai_das_parcelas_futuras(self):
        estornada = dict(_parcela(2, total=10, descricao="LOJA EXEMPLO"), categoria="Compra estornada")
        series = relatorio._parceladas([_parcela(1, total=10, descricao="LOJA EXEMPLO", dia="2026-09-20"), estornada],
                                       "2026-11-01")
        self.assertEqual(len(series), 1)
        self.assertTrue(series[0]["cancelada"])
        self.assertEqual(series[0]["restante"], 0)

    def test_parcela_lida_da_descricao(self):
        series = relatorio._parceladas([_parcela(None, descricao="LOJA EXEMPLO 2/4", metadados=False)], "2026-11-01")
        self.assertEqual((series[0]["numero"], series[0]["total"], series[0]["origem"]), (2, 4, "descrição"))
        self.assertEqual(series[0]["restante"], 2 * 5000)


class TesteProximaFatura(unittest.TestCase):
    def test_lancamento_entra_pela_fatura_informada(self):
        def linha(ident, valor, dia, fatura):
            return {"id": ident, "conta_id": "c", "conta_tipo": "CREDIT", "natureza": "despesa", "categoria": "Compras",
                    "valor_centavos": -valor, "dia": dia, "mes": dia[:7], "fatura_id": fatura, "instituicao": "X",
                    "descricao": "LOJA", "parcela_numero": None, "parcela_total": None}
        linhas = [linha("a", 10000, "2026-10-10", "aberta"), linha("b", 1000, "2026-10-11", "aberta"),
                  linha("c", 20000, "2026-11-15", "aberta"),   # data futura, mas é da fatura aberta
                  linha("d", 5000, "2026-10-20", "futura"),    # parcela de uma fatura futura
                  linha("e", 3000, "2026-10-12", None),        # sem fatura informada: vale a data
                  linha("f", 7000, "2026-09-20", "fechada")]
        contas = [{"id": "c", "tipo": "CREDIT", "instituicao": "X", "numero_final": "1234", "saldo_centavos": -50000,
                   "limite_centavos": None}]
        faturas = [{"id": "fechada", "conta_id": "c", "fechamento": "2026-10-04", "vencimento": "2026-10-11",
                    "total_centavos": 7000}]
        cartao = relatorio._cartoes(linhas, contas, faturas, date(2026, 11, 1))[0]
        self.assertEqual(cartao["fatura_atual"], 10000 + 1000 + 20000 + 3000)  # pela fatura informada
        # sem compras parceladas, o resto do limite utilizado é da próxima fatura: 500 - 70 em aberto
        self.assertEqual((cartao["proxima"], cartao["parcelas_pelo_saldo"]), (43000, 0))


class TesteSaidas(Base):
    def test_html_seguro(self):
        pagina = relatorio._montar(self.d, "teste")
        self.assertIn("Content-Security-Policy", pagina)
        self.assertNotIn("<script", pagina)
        for externo in ("http://", "https://", "src="):
            self.assertNotIn(externo, pagina, externo)
        for numero in ("55443322", "260/0001", CPF_EU):
            self.assertNotIn(numero, pagina, numero)

    def test_html_escapa_texto(self):
        self.d["recorrentes"][0]["descricao"] = "<img src=x onerror=alert(1)>"
        pagina = relatorio._montar(self.d, "teste")
        self.assertNotIn("<img", pagina)
        self.assertIn("&lt;img", pagina)

    def test_tela_sem_valores(self):
        texto = "\n".join(relatorio.resumo_tela(self.d, "painel.html"))
        self.assertNotIn("R$", texto)
        self.assertNotIn("NETFLIX", texto.upper())
        self.assertIn("confere", texto)

    def test_painel_novo_sem_script_e_com_visoes(self):
        import re
        self.assertEqual(self.d["historico"], ["2026-08", "2026-09", "2026-10"])
        pagina = relatorio._montar(self.d, "teste")
        for texto in ("Últimos 12 meses", "Visão por ano", "Detalhamento por categoria", 'id="p-12m"',
                      "#p-12m:checked ~ .visoes #v-12m", "prefers-reduced-motion"):
            self.assertIn(texto, pagina)
        for proibido in ("<script", "javascript:", "@import", "url(", "▲", "▼", "✓"):
            self.assertNotIn(proibido, pagina, proibido)
        self.assertIsNone(re.search(r"<[^>]*\son[a-z]+\s*=", pagina))  # nenhum atributo de evento (onclick...)

    def test_animacoes_sobrias(self):
        import re
        duracoes = [int(x) for x in re.findall(r"(\d+)ms", relatorio._ESTILO)]
        self.assertTrue(duracoes)
        self.assertLessEqual(max(duracoes), 200)  # transições curtas
        self.assertNotIn("infinite", relatorio._ESTILO)  # nada que pulse ou pisque
        self.assertIn("prefers-reduced-motion: reduce", relatorio._ESTILO)


class TesteAvisosDeConexao(unittest.TestCase):
    AGORA_BRT = datetime(2026, 11, 1, 9, 0, tzinfo=datas.BRT)

    def item(self, **campos):
        base = {"instituicao": "Banco Exemplo", "status": "UPDATED", "autorizacao_expira": "2027-10-31T00:00:00.000Z",
                "ultima_sincronizacao": "2026-10-31T10:00:00.000Z", "visto_em": AGORA}
        return dict(base, **campos)

    def test_conexao_em_dia_nao_avisa(self):
        self.assertEqual(relatorio.avisos_das_conexoes([self.item()], self.AGORA_BRT), [])

    def test_banco_parado_status_e_autorizacao_vencida(self):
        avisos = relatorio.avisos_das_conexoes([
            self.item(ultima_sincronizacao="2026-10-01T10:00:00.000Z"),
            self.item(instituicao="Outro Banco", status="LOGIN_ERROR", autorizacao_expira="2026-10-20T00:00:00.000Z"),
        ], self.AGORA_BRT)
        self.assertEqual(len(avisos), 2)
        self.assertIn("Banco Exemplo: o banco não sincroniza há 31 dias", avisos[0])
        self.assertIn("status LOGIN_ERROR", avisos[1])
        self.assertIn("autorização vencida em 19/10/2026", avisos[1])


class TesteAvisoNoPainel(Base):
    def test_aviso_no_painel_e_na_tela_sem_valores(self):
        self.d["avisos_conexao"] = ["<b>Banco</b>: status LOGIN_ERROR"]
        pagina = relatorio._montar(self.d, "teste")
        tela = "\n".join(relatorio.resumo_tela(self.d, Path("painel-2026-10.html"), Path("painel.html")))
        self.assertIn("Dados possivelmente parados.", pagina)
        self.assertIn("&lt;b&gt;Banco&lt;/b&gt;", pagina)
        self.assertIn("1 conexão com dados possivelmente parados", tela)
        self.assertIn("Painel: painel.html (o mesmo que painel-2026-10.html)", tela)
        self.assertNotIn("R$", tela)


def _sintetico(dia, valor, categoria, natureza):
    return {"id": dia + categoria, "dia": dia, "data_utc": f"{dia}T12:00:00Z", "valor_centavos": valor,
            "natureza": natureza, "categoria": categoria, "conta_tipo": "BANK", "conta_id": "c", "duvidoso": 0,
            "visto_em": AGORA, "descricao": f"LANCAMENTO {categoria}", "comerciante": None, "instituicao": "Banco",
            "numero_final": "1234", "parcela_numero": None, "parcela_total": None, "fatura_id": None}


class TesteHistorico(unittest.TestCase):
    def test_meses_do_historico(self):
        linhas = [{"mes": "2025-11"}, {"mes": "2026-02"}, {"mes": "2027-01"}]  # a parcela futura fica de fora
        self.assertEqual(relatorio.meses_do_historico(linhas, "2026-03"),
                         ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"])
        self.assertEqual(relatorio.meses_do_historico([], "2026-03"), ["2026-03"])
        self.assertEqual(relatorio.meses_do_historico([{"mes": "x<y"}], "2026-03"), ["2026-03"])  # mês inválido não entra

    def test_ano_contra_ano_nos_mesmos_meses(self):
        historico = relatorio.meses_do_historico([{"mes": "2025-03"}], "2026-10")  # mar/2025 a out/2026
        por_mes = {m: {"receitas": 100000, "despesas": 15000 if m.startswith("2026") else 10000} for m in historico}
        ano_2025, ano_2026 = relatorio.resumo_anual(historico, por_mes)
        self.assertFalse(ano_2025["completo"])
        self.assertIsNone(ano_2025["comparavel"])  # 2024 não está no histórico
        self.assertEqual(ano_2026["meses_comparaveis"], [f"2026-{n:02d}" for n in range(3, 11)])  # mar a out
        self.assertEqual(ano_2026["comparavel"]["despesas"], 8 * 15000)
        self.assertEqual(ano_2026["anterior_comparavel"]["despesas"], 8 * 10000)
        self.assertEqual(ano_2026["totais"]["despesas"], 10 * 15000)  # o ano inteiro tem jan e fev também

    def test_visoes_de_periodo(self):
        historico = relatorio.meses_do_historico([{"mes": "2024-11"}], "2026-10")
        visoes = relatorio.visoes_de_periodo(historico)
        self.assertEqual([v["id"] for v in visoes], ["12m", "2026", "2025", "2024", "tudo"])
        self.assertEqual((visoes[0]["meses"][0], len(visoes[-1]["meses"])), ("2025-11", 24))
        self.assertEqual([v["id"] for v in relatorio.visoes_de_periodo(["2026-09", "2026-10"])], ["12m", "2026"])

    def test_formatos_sobrios(self):
        self.assertEqual(relatorio._variacao(11000, 10000), "+10%")
        self.assertEqual(relatorio._variacao(9000, 10000), "−10%")
        self.assertEqual(relatorio._variacao(10010, 10000), "0%")
        self.assertEqual(relatorio._variacao(100, 0), "sem base")
        self.assertEqual(relatorio._r(-123456), "−R$ 1.234,56")
        self.assertEqual(relatorio._inteiro(-123456), "−1.235")
        self.assertEqual(relatorio._pontos(0.30, 0.25), "+5 p.p.")
        self.assertEqual(relatorio._r(None), "n/d")

    def test_painel_com_dois_anos(self):
        linhas = []
        for numero in range(1, 23):  # jan/2025 a out/2026
            mes = relatorio.somar_meses("2025-01", numero - 1)
            linhas.append(_sintetico(f"{mes}-05", 500000, "Salário, bolsa e renda", "receita"))
            linhas.append(_sintetico(f"{mes}-10", -(120000 if mes.startswith("2026") else 100000), "Mercado", "despesa"))
        d = relatorio.calcular(linhas, [], [], "2026-10", HOJE)
        self.assertEqual(len(d["historico"]), 22)
        self.assertEqual(d["mesmo_mes_ano_anterior"]["despesas"], 100000)
        pagina = relatorio._montar(d, "teste")
        for texto in ("Mesmos meses (jan a out)", "2026 sobre 2025, mesmos meses", "Todo o histórico", "+20%"):
            self.assertIn(texto, pagina)


if __name__ == "__main__":
    unittest.main()
