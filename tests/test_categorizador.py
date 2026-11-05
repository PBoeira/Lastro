"""Testes do categorizador com dados sintéticos no estilo Nubank e BB, usando o regras.toml do projeto."""

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from financas import categorizador, coletor, config, db, formato, revisao

AGORA = "2026-10-31T20:00:00-03:00"
CPF_EU = "123.456.789-09"
CPF_OUTRO = "987.654.321-00"
CNPJ_NU = "18.236.120/0001-58"

CONTAS = {
    "conta-bb": ("item-bb", {"id": "conta-bb", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "number": "1234/98765-4",
                             "taxNumber": CPF_EU, "bankData": {"transferNumber": "001/1234/98765-4"}}),
    "cartao-bb": ("item-bb", {"id": "cartao-bb", "type": "CREDIT", "subtype": "CREDIT_CARD", "number": "5501",
                              "taxNumber": CPF_EU}),
    "conta-nu": ("item-nu", {"id": "conta-nu", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "number": "55443322-1",
                             "taxNumber": CPF_EU, "bankData": {"transferNumber": "260/0001/55443322-1"}}),
    "cartao-nu": ("item-nu", {"id": "cartao-nu", "type": "CREDIT", "subtype": "CREDIT_CARD", "number": "7702",
                              "taxNumber": CPF_EU}),
}
INSTITUICOES = {"item-bb": "Banco do Brasil (via MeuPluggy)", "item-nu": "Nubank (via MeuPluggy)"}


def tr(ident, valor, descricao, tipo=None, dia="2026-10-10", **extra):
    tipo = tipo or ("DEBIT" if valor < 0 else "CREDIT")
    return {"id": ident, "date": f"{dia}T15:00:00.000Z", "amount": valor, "type": tipo, "description": descricao,
            "status": "POSTED", "currencyCode": "BRL", **extra}


def pix(ident, valor, descricao, documento, tipo_doc="CPF", nome="Fulano", dia="2026-10-10"):
    lado = {"name": nome, "documentNumber": {"type": tipo_doc, "value": documento}}
    eu = {"name": "Titular", "documentNumber": {"type": "CPF", "value": CPF_EU}}
    dados = {"paymentMethod": "PIX", "payer": eu, "receiver": lado} if valor < 0 else \
            {"paymentMethod": "PIX", "payer": lado, "receiver": eu}
    return tr(ident, valor, descricao, dia=dia, operationType="PIX", paymentData=dados)


LANCAMENTOS = {
    "conta-nu": [
        tr("n1", -500.0, "Dinheiro guardado"),
        tr("n2", 200.0, "Dinheiro resgatado"),
        tr("n3", -1000.0, "Pagamento de fatura"),
        tr("n4", 300.0, "Transferência recebida pelo Pix", dia="2026-10-11"),
        tr("n5", -45.9, "Compra no débito - IFOOD *RESTAURANTE"),
        pix("n6", -80.0, "Transferência enviada pelo Pix - JOAO RENNER", CPF_OUTRO, nome="JOAO RENNER"),
        pix("n7", 100.0, "Transferência recebida pelo Pix - MARIA", CPF_OUTRO, nome="MARIA"),
        tr("n8", 1200.0, "Bolsa de estágio - CIEE"),
        tr("n9", 150.0, "Resgate limite garantido"),
        tr("n10", -175.0, "Aplicação limite garantido"),
        pix("n11", 400.0, "Transferência recebida pelo Pix - Titular", CPF_EU, nome="Titular", dia="2026-10-20"),
        pix("n12", 800.0, "Transferência recebida pelo Pix - Titular", CPF_EU, nome="Titular", dia="2026-10-22"),
    ],
    "conta-bb": [
        pix("b1", -250.0, "Pix - Enviado 31/10 Titular", CPF_EU, nome="Titular"),
        tr("b2", -700.0, "PAGTO CARTAO CRED OUROCARD"),
        tr("b3", -300.0, "TED TRANSF.ELETR.DISPONIV"),
        tr("b4", -100.0, "BB RENDE FACIL"),
        tr("b5", 50.0, "RESGATE BB RENDE FACIL"),
        tr("b6", -30.0, "TARIFA PACOTE DE SERVICOS"),
        dict(tr("b7", -900.0, "PAGAMENTO DE BOLETO - NU PAGAMENTOS"), operationType="BOLETO",
             paymentData={"paymentMethod": "BOLETO", "receiver": {"name": "NU PAGAMENTOS S.A.",
                                                                  "documentNumber": {"type": "CNPJ", "value": CNPJ_NU}}}),
        pix("b8", -60.0, "PIX ENVIADO - CARLOS NU PAGAMENTOS", CPF_OUTRO, nome="CARLOS"),
        tr("b9", -150.0, "ENEL DISTRIBUICAO"),
        pix("b10", -400.0, "Pix - Enviado Titular", CPF_EU, nome="Titular", dia="2026-10-20"),
    ],
    "cartao-nu": [
        tr("c1", 60.0, "IFOOD *PEDIDO"),
        tr("c2", 25.0, "UBER *TRIP"),
        tr("c3", 40.0, "UBER EATS"),
        tr("c4", 120.0, "MERCADO LIVRE*LOJA", creditCardMetadata={"installmentNumber": 2, "totalInstallments": 6}),
        tr("c5", 200.0, "SUPERMERCADO EXEMPLO"),
        tr("c6", 3.5, "IOF COMPRA INTERNACIONAL"),
        tr("c7", -1000.0, "Pagamento recebido", operationType="PAGAMENTO_FATURA"),
        tr("c8", -60.0, "Estorno - IFOOD"),
        tr("c9", 55.9, "NETFLIX.COM"),
        tr("c10", 30.0, "PAG*XYZ COMERCIO"),
        tr("c11", 33.0, "LOJA ALEATORIA", category="Food delivery"),
        tr("c12", 15.0, "BOM DIA PADARIA"),
        tr("c13", 10.0, "COMPRA 99 UNIDADES"),
    ],
    "cartao-bb": [
        tr("o1", -700.0, "PAGAMENTO OUROCARD"),
        tr("o2", 99.9, "SMARTFIT MENSALIDADE"),
        tr("o3", 35.0, "DROGARIA EXEMPLO"),
    ],
}

ESPERADO = {
    "n1": "Poupança (Caixinha e aplicações)", "n2": "Resgate de poupança", "n3": "Pagamento de fatura",
    "n4": "Transferência entre contas próprias", "n5": "Restaurantes e delivery",
    "n6": "Pix e transferências enviadas", "n7": "Pix e transferências recebidas", "n8": "Salário, bolsa e renda",
    "n9": "Resgate de poupança", "n10": "Poupança (Caixinha e aplicações)",
    "n11": "Transferência entre contas próprias", "n12": "Entrada de conta própria não monitorada",
    "b1": "Saída para conta própria não monitorada", "b2": "Pagamento de fatura",
    "b3": "Transferência entre contas próprias", "b4": "Poupança (Caixinha e aplicações)",
    "b5": "Resgate de poupança", "b6": "Tarifas, juros e IOF", "b7": "Pagamento de fatura",
    "b8": "Pix e transferências enviadas", "b9": "Moradia e contas", "b10": "Transferência entre contas próprias",
    # c1 e c8: compra e estorno do mesmo valor e da mesma loja
    "c1": "Compra estornada", "c2": "Transporte", "c3": "Restaurantes e delivery", "c4": "Compras",
    "c5": "Mercado", "c6": "Tarifas, juros e IOF", "c7": "Pagamento de fatura", "c8": "Compra estornada",
    "c9": "Assinaturas e serviços digitais", "c10": "Sem categoria (saída)", "c11": "Restaurantes e delivery",
    "c12": "Restaurantes e delivery", "c13": "Sem categoria (saída)",
    "o1": "Pagamento de fatura", "o2": "Academia e bem-estar", "o3": "Saúde",
}


class Base(unittest.TestCase):
    DADOS = LANCAMENTOS

    def setUp(self):
        self.con = db.conectar_arquivo(":memory:")
        for item, nome in INSTITUICOES.items():
            coletor.gravar_item(self.con, {"id": item, "status": "UPDATED", "connector": {"name": "MeuPluggy"}},
                                AGORA, nome)
        for conta_id, (item, conta) in CONTAS.items():
            coletor.gravar_conta(self.con, conta, item, AGORA)
        self.con.commit()
        documentos, numeros = coletor.identidades_proprias([c for _, c in CONTAS.values()])
        for conta_id, lista in self.DADOS.items():
            coletor.sincronizar_transacoes(self.con, conta_id, lista, None, True, documentos, numeros, AGORA)
        for cartao in ("cartao-nu", "cartao-bb"):
            with self.con:
                coletor.definir_convencao(self.con, cartao, [], "positivo_e_gasto")
        # só as regras genéricas: os testes não dependem do regras.local.toml de ninguém
        self.regras = categorizador.carregar_regras(caminho_local=None)

    def tearDown(self):
        self.con.close()

    def resultado(self, ajustes=None):
        categorizador.classificar(self.con, self.regras, ajustes or {})
        return {l["id"]: l for l in self.con.execute("SELECT * FROM lancamentos_classificados")}


class TesteCategorias(Base):
    def test_cada_lancamento(self):
        linhas = self.resultado()
        erros = {i: (linhas[i]["categoria"], esperado, linhas[i]["regra"])
                 for i, esperado in ESPERADO.items() if linhas[i]["categoria"] != esperado}
        self.assertEqual(erros, {})

    def test_neutros_fora_de_receitas_e_despesas(self):
        linhas = self.resultado()
        for ident in ("n1", "n2", "n3", "n4", "n9", "n10", "n11", "n12", "b1", "b2", "b3", "b4", "b5", "b7", "b10",
                      "c7", "o1"):
            self.assertEqual(linhas[ident]["natureza"], "neutro", ident)
        despesas = sum(l["valor_centavos"] for l in linhas.values() if l["natureza"] == "despesa")
        receitas = sum(l["valor_centavos"] for l in linhas.values() if l["natureza"] == "receita")
        # Gastos de verdade: n5 n6 b6 b8 b9 + cartão (c1..c13 sem c7) + o2 o3; o estorno c8 reduz.
        self.assertEqual(despesas, -(4590 + 8000 + 3000 + 6000 + 15000 + 6000 + 2500 + 4000 + 12000 + 20000 + 350
                                     + 5590 + 3000 + 3300 + 1500 + 1000 + 9990 + 3500 - 6000))
        self.assertEqual(receitas, 10000 + 120000)

    def test_estorno_reduz_gasto(self):
        self.assertGreater(self.resultado()["c8"]["valor_centavos"], 0)

    def test_duvidosos(self):
        linhas = self.resultado()
        duvidosos = {i for i, l in linhas.items() if l["duvidoso"]}
        self.assertEqual(duvidosos, {"n6", "b8", "c10", "c13"})  # Pix recebido é receita normal

    def test_ajuste_manual_vence(self):
        ajustes = {categorizador.id_curto("c10"): "Compras"}
        linha = self.resultado(ajustes)["c10"]
        self.assertEqual((linha["categoria"], linha["regra"], linha["duvidoso"]), ("Compras", "ajuste manual", 0))

    def test_rodar_de_novo_da_o_mesmo(self):
        primeira = {i: l["categoria"] for i, l in self.resultado().items()}
        segunda = {i: l["categoria"] for i, l in self.resultado().items()}
        self.assertEqual(primeira, segunda)


class TesteResumoSoAgregados(Base):
    """A saída do 'categorizar' (que o Claude lê no perfil real) nunca mostra um lançamento."""

    def resumo(self):
        estatisticas = categorizador.classificar(self.con, self.regras, {})
        linhas = self.con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        return linhas, "\n".join(categorizador.montar_resumo("teste", linhas, estatisticas, detalhar=True))

    def test_sem_descricoes_nem_contrapartes(self):
        _, texto = self.resumo()
        for trecho in ("ifood", "renner", "maria", "carlos", "exemplo", "netflix", "uber", "smartfit", "enel",
                       "mercado livre", "xyz", "aleatoria", "ciee", "padaria", "rende facil", "unidades"):
            self.assertNotIn(trecho, texto.lower(), trecho)

    def test_sem_valor_de_categoria_pequena(self):
        linhas, texto = self.resumo()
        tamanho = Counter(l["categoria"] for l in linhas)
        pequenas = [l for l in linhas if tamanho[l["categoria"]] < categorizador.MINIMO_PARA_SOMA]
        self.assertTrue(pequenas)  # o conjunto sintético tem categorias pequenas de propósito
        for linha in pequenas:
            self.assertNotIn(formato.reais(linha["valor_centavos"]), texto, linha["id"])
        self.assertIn("soma omitida", texto)

    def test_total_some_quando_a_parte_escondida_e_pequena(self):
        linhas, texto = self.resumo()
        receitas = [l for l in linhas if l["natureza"] == "receita"]  # 2 lançamentos, em 2 categorias de 1
        self.assertEqual(len(receitas), 2)
        self.assertNotIn(formato.reais(sum(l["valor_centavos"] for l in receitas)), texto)
        self.assertIn("total omitido", texto)

    def test_detalhamento_so_contagens(self):
        _, texto = self.resumo()
        detalhamento = texto.split("detalhamento por conta", 1)[1]
        self.assertNotIn("R$", detalhamento)
        self.assertIn("••••7702", detalhamento)
        for numero in ("98765", "55443322", "1234/"):
            self.assertNotIn(numero, detalhamento)


CNPJ_BB = "00.000.000/0001-91"  # CNPJ público do Banco do Brasil

NOVOS = {
    "conta-bb": [
        dict(tr("x1", -500.0, "PIX ENVIADO BANCO DO BRASIL SA"), operationType="PIX",
             paymentData={"paymentMethod": "PIX", "receiver": {"name": "BANCO DO BRASIL SA",
                                                               "documentNumber": {"type": "CNPJ", "value": CNPJ_BB}}}),
    ],
    "conta-nu": [
        pix("x2", -98.0, "Transferência enviada pelo Pix - JOSE SOUZA", CPF_OUTRO, nome="JOSE SOUZA", dia="2026-10-12"),
        pix("x3", -190.0, "Transferência enviada pelo Pix - ANA COSTA", CPF_OUTRO, nome="ANA COSTA", dia="2026-10-14"),
        pix("x9", -60.0, "Transferência enviada pelo Pix - LUIS PRADO", CPF_OUTRO, nome="LUIS PRADO", dia="2026-10-15"),
    ],
    "cartao-nu": [
        tr("x4", 100.0, "JOSE SOUZA", dia="2026-10-12"),
        tr("x5", 100.0, "ANA COSTA 1/2", dia="2026-10-14"),
        tr("x6", 100.0, "ANA COSTA 2/2", dia="2026-11-14"),
        tr("x7", 45.0, "BARBEARIA DO BAIRRO"),
        tr("x10", 200.0, "LUIS PRADO", dia="2026-10-15"),  # valor muito acima: não é o mesmo Pix
        tr("y1", 300.0, "LOJA EXEMPLO MARKETPLACE", dia="2026-07-10"),
        tr("y2", -300.0, "Estorno - LOJA EXEMPLO MARKETPLACE", dia="2026-08-05"),
        tr("y3", 600.0, "SERVICO DIGITAL EXEMPLO", dia="2026-08-30"),
        tr("y4", -600.0, "Parcelamento de compra", dia="2026-09-02"),
        tr("y7", -100.0, "Pagamento recebido", dia="2026-09-10"),
        tr("y8", -100.0, "Pagamento recebido", dia="2026-09-10"),  # dois pagamentos iguais em espécie: valem os dois
        tr("v1", 50.0, "LOJA PARCELADA 1/3", dia="2026-08-10"),
        tr("v2", 50.0, "LOJA PARCELADA 2/3", dia="2026-09-10"),
        tr("v3", -50.0, "Estorno de compra", dia="2026-10-05"),  # valor de uma parcela: série cancelada
        tr("u1", 80.0, "OUTRA LOJA 1/2", dia="2026-08-12"),
        tr("u2", -33.0, "Credito diverso", dia="2026-09-01"),    # valor sem relação: não cancela nada
    ],
    "cartao-bb": [
        tr("y5", -500.0, "PAGAMENTO RECEBIDO", dia="2026-08-20"),
        tr("y6", -500.0, "Pagamento de fatura", dia="2026-08-21", operationType="PAGAMENTO_FATURA"),
        tr("z2", -700.0, "PAGAMENTO RECEBIDO", dia="2026-09-20"),
        tr("z3", -700.0, "PAGAMENTO RECEBIDO", dia="2026-09-21"),  # igual ao z2: o cartão contou duas vezes
        tr("w1", -300.0, "PGTO. QR CODE PIX", dia="2026-10-01"),
        tr("w2", -200.0, "PGTO. CASH AG.", dia="2026-10-05"),
        tr("w3", -500.0, "PAGAMENTO RECEBIDO", dia="2026-10-05"),  # soma dos individuais: não conta
    ],
}
NOVOS["conta-bb"].append(
    dict(tr("z1", -700.0, "PIX ENVIADO BANCO DO BRASIL SA", dia="2026-09-19"), operationType="PIX",
         paymentData={"paymentMethod": "PIX", "receiver": {"name": "BANCO DO BRASIL SA",
                                                           "documentNumber": {"type": "CNPJ", "value": CNPJ_BB}}}))

ESPERADO_NOVOS = {
    "x1": "Pagamento de fatura",
    "x2": "Pix no crédito (repasse pela conta)", "x3": "Pix no crédito (repasse pela conta)",
    "x4": "Pix no crédito", "x5": "Pix no crédito", "x6": "Pix no crédito",
    "x7": "Cuidados pessoais",
    "x9": "Pix e transferências enviadas",
    "y1": "Compra estornada", "y2": "Compra estornada", "y3": "Compra estornada", "y4": "Compra estornada",
    "y5": "Pagamento de fatura", "y6": "Pagamento de fatura (duplicado pela API)",
    "y7": "Pagamento de fatura", "y8": "Pagamento de fatura",
    "z1": "Pagamento de fatura", "z2": "Pagamento de fatura", "z3": "Pagamento de fatura (duplicado pela API)",
    "w1": "Pagamento de fatura", "w2": "Pagamento de fatura", "w3": "Pagamento de fatura (duplicado pela API)",
    "v1": "Compra estornada", "v2": "Compra estornada", "v3": "Compra estornada",
}


class TesteParcelamentoCanceladoExigeLoja(Base):
    # crédito com o valor exato da parcela de u1, mas sem o nome da loja nem texto de estorno
    DADOS = {**NOVOS, "cartao-nu": NOVOS["cartao-nu"] + [tr("u3", -80.0, "CREDITO PROMOCIONAL", dia="2026-09-05")]}

    def test_credito_sem_loja_nem_estorno_nao_anula_a_serie(self):
        linhas = self.resultado()
        self.assertNotEqual(linhas["u1"]["categoria"], "Compra estornada")
        self.assertNotEqual(linhas["u3"]["categoria"], "Compra estornada")
        self.assertEqual(linhas["v1"]["categoria"], "Compra estornada")  # a série com estorno continua detectada


class TesteRegrasNovas(Base):
    DADOS = NOVOS

    def test_cada_lancamento(self):
        linhas = self.resultado()
        erros = {i: (linhas[i]["categoria"], esperado, linhas[i]["regra"])
                 for i, esperado in ESPERADO_NOVOS.items() if linhas[i]["categoria"] != esperado}
        self.assertEqual(erros, {})

    def test_parcelamento_cancelado_automatico(self):
        linhas = self.resultado()
        self.assertEqual(linhas["v1"]["regra"], "Parcelamento cancelado (detectado automaticamente)")
        self.assertEqual(linhas["v1"]["duvidoso"], 1)  # é um palpite: aparece na página de revisão
        self.assertNotEqual(linhas["u1"]["categoria"], "Compra estornada")
        self.assertNotEqual(linhas["u2"]["categoria"], "Compra estornada")

    def test_pix_no_credito_conta_so_uma_vez(self):
        linhas = self.resultado()
        self.assertEqual(linhas["x2"]["natureza"], "neutro")  # a saída da conta não é gasto
        self.assertEqual(linhas["x4"]["natureza"], "despesa")  # o gasto é a cobrança no cartão
        self.assertNotEqual(linhas["x10"]["categoria"], "Pix no crédito")

    def test_resumo_so_contagens(self):
        estatisticas = categorizador.classificar(self.con, self.regras, {})
        linhas = self.con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        texto = "\n".join(categorizador.montar_resumo("teste", linhas, estatisticas, so_contagens=True))
        self.assertNotIn("R$", texto)
        self.assertIn("Pix no crédito reconhecidos: 2 saídas na conta, 3 cobranças no cartão", texto)
        self.assertIn("pagamentos de fatura repetidos pela API: 3; lançamentos anulados por estorno do mesmo valor: 4",
                      texto)

    def test_regra_pessoal_de_loja_nao_passa_por_cima_da_estrutural(self):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            local = Path(pasta) / "regras.local.toml"
            local.write_text("[[regra]]\nnome = \"loja\"\npessoa = false\ndescricao = 'loja exemplo'\n"
                             'categoria = "Mercado"\n', encoding="utf-8")
            regras = categorizador.carregar_regras(caminho_local=local)
        categorizador.classificar(self.con, regras, {})
        linhas = {l["id"]: l for l in self.con.execute("SELECT * FROM lancamentos_classificados")}
        self.assertEqual((linhas["y1"]["categoria"], linhas["y2"]["categoria"]), ("Compra estornada", "Compra estornada"))

    def test_regras_nao_confirmadas_param_o_real_fora_do_terminal(self):
        # Os ajustes mudaram depois da confirmação do dono e o comando roda fora do terminal dele
        # (assistente de IA, tarefa agendada). No real, categorizar e relatorio param antes de abrir
        # o banco e de classificar, devolvem o código de regras não confirmadas e não mostram contagem.
        # No perfil teste, nada muda.
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from financas import relatorio
        confirmadas = {config.CAMINHO_REGRAS: config.CAMINHO_REGRAS.read_bytes(),
                       config.CAMINHO_REGRAS_LOCAL: None, config.CAMINHO_AJUSTES: None}
        atuais = {**confirmadas, config.CAMINHO_AJUSTES: b"# mudou depois da confirmacao\n"}
        digital_confirmada = categorizador.impressao_digital_regras(conteudos=confirmadas)
        avisos, tela = [], io.StringIO()
        proibido = mock.Mock(side_effect=AssertionError("não podia abrir o banco nem classificar"))
        with tempfile.TemporaryDirectory() as pasta:
            marca = Path(pasta) / "regras-confirmadas.sha256"
            marca.write_text(digital_confirmada, encoding="ascii")
            with mock.patch.object(categorizador, "_marca_regras", lambda perfil: marca), \
                    mock.patch.object(categorizador, "fotografar_regras", lambda caminhos=None: dict(atuais)), \
                    mock.patch.object(categorizador, "console_do_dono", lambda: False), \
                    mock.patch.object(relatorio, "avisar_regras_nao_confirmadas", avisos.append), \
                    redirect_stdout(tela):
                with mock.patch.object(db, "conectar", proibido), \
                        mock.patch.object(categorizador, "classificar", proibido):
                    self.assertEqual(categorizador.categorizar("real"), categorizador.SAIDA_REGRAS_NAO_CONFIRMADAS)
                    self.assertEqual(relatorio.relatorio("real"), categorizador.SAIDA_REGRAS_NAO_CONFIRMADAS)
                parado = tela.getvalue()
                with mock.patch.object(db, "conectar", lambda perfil: self.con), \
                        mock.patch.object(revisao, "gerar", lambda *argumentos: "revisao.html"):
                    self.assertEqual(categorizador.categorizar("teste"), 0)  # no teste, nada muda
            self.assertEqual(marca.read_text(encoding="ascii"), digital_confirmada)  # nada foi confirmado
        proibido.assert_not_called()
        self.assertEqual(avisos, ["real", "real"])  # aviso no painel antigo, uma vez por comando
        self.assertIn("CONFIRMO", parado)
        self.assertNotIn("lanç", parado)  # nenhuma contagem na tela
        self.assertIn("Categorização do perfil 'teste'", tela.getvalue())

    def test_so_contagens_e_padrao_no_real(self):
        self.assertTrue(categorizador.modo_so_contagens("real", False, False))
        self.assertFalse(categorizador.modo_so_contagens("real", False, True))   # --com-somas, só para o dono
        self.assertFalse(categorizador.modo_so_contagens("teste", False, False))
        self.assertTrue(categorizador.modo_so_contagens("teste", True, False))

    def test_duas_execucoes_so_contagens_nao_revelam_valor(self):
        # cenário do achado M1: uma decisão manual muda 1 lançamento de categoria entre duas execuções
        primeira = "\n".join(categorizador.montar_resumo(
            "real", self.con.execute("SELECT * FROM lancamentos_classificados").fetchall(),
            categorizador.classificar(self.con, self.regras, {}), so_contagens=True))
        estatisticas = categorizador.classificar(self.con, self.regras, {categorizador.id_curto("x9"): "Compras"})
        segunda = "\n".join(categorizador.montar_resumo(
            "real", self.con.execute("SELECT * FROM lancamentos_classificados").fetchall(), estatisticas,
            so_contagens=True))
        self.assertNotEqual(primeira, segunda)
        for texto in (primeira, segunda):
            self.assertNotIn("R$", texto)


class TesteRegrasLocais(unittest.TestCase):
    def test_local_vem_antes_e_pode_criar_categoria(self):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            local = Path(pasta) / "regras.local.toml"
            local.write_text('[categorias]\n"Categoria Sintetica" = "despesa"\n'
                             "[[regra]]\nnome = \"Loja sintetica\"\ndescricao = 'netflix'\n"
                             'categoria = "Categoria Sintetica"\n', encoding="utf-8")
            regras = categorizador.carregar_regras(caminho_local=local)
        self.assertEqual(regras.categorias["Categoria Sintetica"], "despesa")
        # ordem: estruturais genéricas, depois as pessoais, depois o resto das genéricas
        estruturais = [r for r in regras.regras if set(r.condicoes) & categorizador._FLAGS_ESTRUTURAIS]
        self.assertTrue(estruturais)
        self.assertEqual(regras.regras[len(estruturais)].nome, "local: Loja sintetica")
        self.assertIn("Mercado", regras.categorias)  # as genéricas continuam lá

    def test_sem_arquivo_local(self):
        regras = categorizador.carregar_regras(caminho_local=None)
        self.assertFalse(any(r.nome.startswith("local: ") for r in regras.regras))


class TesteRegrasInvalidas(unittest.TestCase):
    def carregar(self, conteudo):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            caminho = Path(pasta) / "regras.toml"
            caminho.write_text(conteudo, encoding="utf-8")
            return categorizador.carregar_regras(caminho, caminho_local=None)

    def test_erros_claros(self):
        base = '[categorias]\n"Mercado" = "despesa"\n'
        casos = {
            "categoria inexistente": base + '[[regra]]\ndescricao = "x"\ncategoria = "Nada"\n',
            "expressão inválida": base + '[[regra]]\ndescricao = "(abc"\ncategoria = "Mercado"\n',
            "campo desconhecido": base + '[[regra]]\ndescricoes = "x"\ncategoria = "Mercado"\n',
            "sem condição": base + '[[regra]]\ncategoria = "Mercado"\n',
            "natureza inválida": '[categorias]\n"Mercado" = "gasto"\n',
            "toml quebrado": '[categorias\n',
            "alternativa repetida": base + "[[regra]]\ndescricao = '(a|aa)+$'\ncategoria = \"Mercado\"\n",
            "repetição repetida": base + "[[regra]]\ndescricao = '(a+)*b'\ncategoria = \"Mercado\"\n",
            "grupo aninhado repetido": base + "[[regra]]\ndescricao = '((a|aa))+$'\ncategoria = \"Mercado\"\n",
            "repetições seguidas": base + "[[regra]]\ndescricao = 'a*a*a*a*a*b'\ncategoria = \"Mercado\"\n",
            "padrão longo demais": base + "[[regra]]\ndescricao = '" + "x" * 401 + "'\ncategoria = \"Mercado\"\n",
        }
        for nome, conteudo in casos.items():
            with self.assertRaises(categorizador.ErroRegras, msg=nome):
                self.carregar(conteudo)

    def test_acento_no_padrao_casa_texto_sem_acento(self):
        regras = self.carregar('[categorias]\n"Mercado" = "despesa"\n'
                               "[[regra]]\ndescricao = 'hortifrúti'\ncategoria = \"Mercado\"\n")
        self.assertTrue(regras.regras[0].condicoes["descricao"].search("HORTIFRUTI DA ESQUINA"))


class TestePaginaDeRevisao(Base):
    def test_pagina_segura_e_completa(self):
        self.resultado()
        with self.con:
            self.con.execute("UPDATE transacoes SET descricao = '<script>alert(1)</script>' WHERE id = 'c10'")
        linhas = self.con.execute("SELECT * FROM lancamentos_classificados").fetchall()
        pagina = revisao._montar("teste", linhas, self.regras.categorias)
        self.assertIn("Content-Security-Policy", pagina)
        self.assertNotIn("<script>", pagina)
        self.assertIn("&lt;script&gt;", pagina)
        self.assertIn(f'&quot;{categorizador.id_curto("c13")}&quot;', pagina)  # linha pronta para ajustes
        for proibido in ("98765-4", "55443322", "987.654.321-00", "123.456.789-09", "http://", "https://"):
            self.assertNotIn(proibido, pagina, proibido)


if __name__ == "__main__":
    unittest.main()
