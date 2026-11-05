"""Testes do coletor com dados sintéticos (nada de rede, nada de credencial).

Rodar na raiz do projeto: .\\testes.cmd
"""

import re
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from financas import coletor, config, db, pluggy

AGORA = "2026-10-31T20:00:00-03:00"
CPF_TITULAR = "123.456.789-09"
CPF_TERCEIRO = "987.654.321-00"

ITEM_OK = {"id": "item-1", "status": "UPDATED", "executionStatus": "SUCCESS",
           "connector": {"name": "Teste", "isSandbox": True}}
CONTA_BB = {"id": "conta-bb", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "name": "Conta BB",
            "number": "1234/98765-4", "balance": 1500.25, "currencyCode": "BRL", "taxNumber": CPF_TITULAR,
            "bankData": {"transferNumber": "001/1234/98765-4"}}
CONTA_NU = {"id": "conta-nu", "type": "BANK", "subtype": "CHECKING_ACCOUNT", "name": "Conta Nu",
            "number": "55443322-1", "balance": 10.0, "currencyCode": "BRL", "taxNumber": "12345678909"}
CARTAO = {"id": "cartao", "type": "CREDIT", "subtype": "CREDIT_CARD", "name": "Cartão", "number": "4321",
          "balance": 300.0, "currencyCode": "BRL", "taxNumber": CPF_TITULAR,
          "creditData": {"creditLimit": 1000, "availableCreditLimit": 700,
                         "balanceCloseDate": "2026-11-03", "balanceDueDate": "2026-11-10"}}


def tr(ident, dia, valor, tipo, descricao, **extra):
    return {"id": ident, "date": f"{dia}T03:00:00.000Z", "amount": valor, "type": tipo,
            "description": descricao, "status": "POSTED", "currencyCode": "BRL", **extra}


def pix(ident, dia, valor, documento, conta=None, nome="Fulano", descricao="PIX ENVIADO"):
    return tr(ident, dia, valor, "DEBIT", descricao, operationType="PIX", descriptionRaw=descricao, paymentData={
        "paymentMethod": "PIX",
        "payer": {"name": "Titular", "documentNumber": {"type": "CPF", "value": CPF_TITULAR},
                  "accountNumber": "98765-4", "branchNumber": "1234"},
        "receiver": {"name": nome, "documentNumber": {"type": "CPF", "value": documento} if documento else None,
                     "accountNumber": conta, "branchNumber": "0001"},
    })


class Base(unittest.TestCase):
    def setUp(self):
        self.con = db.conectar_arquivo(":memory:")
        coletor.gravar_item(self.con, ITEM_OK, AGORA)
        for conta in (CONTA_BB, CONTA_NU, CARTAO):
            coletor.gravar_conta(self.con, conta, "item-1", AGORA)
        self.con.commit()
        self.docs, self.numeros = coletor.identidades_proprias([CONTA_BB, CONTA_NU, CARTAO])

    def tearDown(self):
        self.con.close()

    def sync(self, conta_id, transacoes, data_de=None, confiavel=True):
        return coletor.sincronizar_transacoes(self.con, conta_id, transacoes, data_de, confiavel,
                                              self.docs, self.numeros, AGORA)

    def soma(self, conta_id):
        return self.con.execute("SELECT COALESCE(SUM(valor_centavos), 0) FROM lancamentos WHERE conta_id = ?",
                                (conta_id,)).fetchone()[0]


class TesteIdempotencia(Base):
    LISTA = [
        tr("t1", "2026-10-01", -100.0, "DEBIT", "MERCADO"),
        tr("t2", "2026-10-05", 2500.0, "CREDIT", "SALARIO"),
        tr("t3", "2026-10-10", -49.9, "DEBIT", "ASSINATURA"),
    ]

    def test_rodar_duas_vezes_nao_duplica(self):
        primeira = self.sync("conta-bb", self.LISTA)
        segunda = self.sync("conta-bb", self.LISTA)
        self.assertEqual(primeira["novos"], 3)
        self.assertEqual((segunda["novos"], segunda["alterados"], segunda["removidos"]), (0, 0, 0))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM transacoes").fetchone()[0], 3)

    def test_recriado_com_id_novo_nao_conta_em_dobro(self):
        self.sync("conta-bb", self.LISTA)
        antes = self.soma("conta-bb")
        recriada = [self.LISTA[0], tr("t2-novo", "2026-10-05", 2500.0, "CREDIT", "SALARIO"), self.LISTA[2]]
        resultado = self.sync("conta-bb", recriada)
        self.assertEqual((resultado["novos"], resultado["removidos"]), (1, 1))
        self.assertEqual(self.soma("conta-bb"), antes)

    def test_lancamento_que_volta_deixa_de_ser_removido(self):
        self.sync("conta-bb", self.LISTA)
        self.sync("conta-bb", self.LISTA[:2])
        resultado = self.sync("conta-bb", self.LISTA)
        self.assertEqual(resultado["alterados"], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM lancamentos").fetchone()[0], 3)

    def test_resposta_vazia_nao_remove_nada(self):
        self.sync("conta-bb", self.LISTA)
        resultado = self.sync("conta-bb", [])
        self.assertEqual(resultado["removidos"], 0)
        self.assertIsNotNone(resultado["aviso"])

    def test_item_nao_confiavel_nao_remove(self):
        self.sync("conta-bb", self.LISTA)
        resultado = self.sync("conta-bb", self.LISTA[:1], confiavel=False)
        self.assertEqual(resultado["removidos"], 0)

    def test_fora_da_janela_nao_remove(self):
        self.sync("conta-bb", self.LISTA)
        resultado = self.sync("conta-bb", self.LISTA[1:], data_de="2026-10-03")
        self.assertEqual(resultado["removidos"], 0)  # t1 é de antes da janela relida

    def test_alteracao_de_status_conta_como_alterado(self):
        self.sync("conta-bb", self.LISTA)
        mudada = [dict(self.LISTA[0], status="PENDING")] + self.LISTA[1:]
        self.assertEqual(self.sync("conta-bb", mudada)["alterados"], 1)


class TesteSinal(Base):
    def valor(self, ident):
        return self.con.execute("SELECT valor_centavos FROM lancamentos WHERE id = ?", (ident,)).fetchone()[0]

    def test_conta_corrente_usa_tipo(self):
        self.sync("conta-bb", [tr("d", "2026-10-01", -100.0, "DEBIT", "X"), tr("c", "2026-10-01", 50.0, "CREDIT", "Y")])
        self.assertEqual((self.valor("d"), self.valor("c")), (-10000, 5000))

    def test_cartao_padrao_da_documentacao(self):
        self.sync("cartao", [tr("compra", "2026-10-01", 55.9, "DEBIT", "LOJA"),
                             tr("pagto", "2026-10-02", -300.0, "CREDIT", "PAGAMENTO RECEBIDO")])
        coletor.definir_convencao(self.con, "cartao", [], "positivo_e_gasto")
        self.assertEqual((self.valor("compra"), self.valor("pagto")), (-5590, 30000))

    def test_cartao_convencao_invertida(self):
        self.sync("cartao", [tr("compra", "2026-10-01", -55.9, "CREDIT", "LOJA")])
        coletor.definir_convencao(self.con, "cartao", [], "negativo_e_gasto")
        self.assertEqual(self.valor("compra"), -5590)

    def test_detectar_convencao(self):
        compras_neg = [tr(f"n{i}", "2026-10-01", -10.0 - i, "CREDIT", "LOJA") for i in range(6)]
        compras_pos = [tr(f"p{i}", "2026-10-01", 10.0 + i, "DEBIT", "LOJA") for i in range(6)]
        self.assertEqual(coletor.detectar_convencao(compras_neg), "negativo_e_gasto")
        self.assertEqual(coletor.detectar_convencao(compras_pos), "positivo_e_gasto")
        self.assertIsNone(coletor.detectar_convencao(compras_pos[:2]))


class TesteContasProprias(Base):
    def titular(self, ident):
        return self.con.execute("SELECT contraparte_mesmo_titular FROM transacoes WHERE id = ?",
                                (ident,)).fetchone()[0]

    def test_pix_para_mim_mesmo_por_cpf(self):
        self.sync("conta-bb", [pix("p1", "2026-10-01", -200.0, CPF_TITULAR)])
        self.assertEqual(self.titular("p1"), 1)

    def test_pix_para_terceiro(self):
        self.sync("conta-bb", [pix("p2", "2026-10-01", -200.0, CPF_TERCEIRO)])
        self.assertEqual(self.titular("p2"), 0)

    def test_pix_para_minha_conta_sem_documento(self):
        self.sync("conta-bb", [pix("p3", "2026-10-01", -200.0, None, conta="55443322-1")])
        self.assertEqual(self.titular("p3"), 1)


class TesteSemDadosSensiveis(unittest.TestCase):
    def test_arquivo_nao_guarda_numeros_completos(self):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            arquivo = Path(pasta) / "teste.sqlite3"
            con = db.conectar_arquivo(arquivo)
            coletor.gravar_item(con, ITEM_OK, AGORA)
            for conta in (CONTA_BB, CONTA_NU, CARTAO):
                coletor.gravar_conta(con, conta, "item-1", AGORA)
            docs, numeros = coletor.identidades_proprias([CONTA_BB, CONTA_NU, CARTAO])
            coletor.sincronizar_transacoes(
                con, "conta-bb",
                [dict(pix("p1", "2026-10-01", -200.0, CPF_TERCEIRO, conta="77788899-0",
                          descricao="PIX ENVIADO FULANO AG 0001 CC 77788899-0 CPF 987.654.321-00"),
                      providerId="E00000000AG1234CC9988776655"),
                 pix("p2", "2026-10-02", -50.0, CPF_TITULAR, conta="55443322-1",
                     nome="Titular 55443322-1", descricao="TRANSF CONTA 55443322-1 CPF ***.456.789-**")],
                None, True, docs, numeros, AGORA)
            doc = con.execute("SELECT contraparte_doc FROM transacoes WHERE id = 'p1'").fetchone()[0]
            con.close()
            bruto = arquivo.read_bytes()
        self.assertEqual(doc, "CPF ••00")
        for proibido in ("1234/98765-4", "98765-4", "1234987654", "987654", "55443322", "77788899",
                         "123.456.789-09", "12345678909", "987.654.321-00", "98765432100", "456.789",
                         "E00000000AG1234CC9988776655", "9988776655"):
            self.assertNotIn(proibido.encode(), bruto, proibido)

    def test_migracao_mascara_banco_antigo(self):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            arquivo = Path(pasta) / "antigo.sqlite3"
            antigo = sqlite3.connect(arquivo)
            antigo.executescript(db._ESQUEMA)
            antigo.execute("INSERT INTO itens (id, conector, visto_em) VALUES ('i', 'Banco 66554433-2', 'x')")
            antigo.execute("INSERT INTO contas (id, item_id, tipo, nome, visto_em) "
                           "VALUES ('c', 'i', 'BANK', 'CONTA CORRENTE 44332211-0', 'x')")
            antigo.execute(
                """INSERT INTO transacoes (id, conta_id, dia, data_utc, descricao, descricao_bruta,
                       valor_api_centavos, contraparte_nome, primeira_vez_em, visto_em)
                   VALUES ('t', 'c', '2026-10-01', '2026-10-01', 'PIX CC 77788899-0', 'PIX CPF 987.654.321-00',
                           -100, 'Fulano 77788899-0', 'x', 'x')""")
            antigo.execute("PRAGMA user_version = 1")
            antigo.commit()
            antigo.close()
            con = db.conectar_arquivo(arquivo)  # aplica a migração v2
            descricao = con.execute("SELECT descricao FROM transacoes").fetchone()[0]
            versao = con.execute("PRAGMA user_version").fetchone()[0]
            con.close()
            bruto = arquivo.read_bytes()
        self.assertEqual(versao, db.VERSAO)
        self.assertEqual(descricao, "PIX CC ••••8990")
        for proibido in ("77788899", "987.654.321-00", "66554433", "44332211"):
            self.assertNotIn(proibido.encode(), bruto, proibido)

    def test_nome_de_conta_e_conector_mascarados_ao_gravar(self):
        con = db.conectar_arquivo(":memory:")
        coletor.gravar_item(con, dict(ITEM_OK, connector={"name": "Banco 66554433-2"}), AGORA)
        coletor.gravar_conta(con, dict(CONTA_BB, marketingName="CONTA CORRENTE 44332211-0"), "item-1", AGORA)
        nome = con.execute("SELECT nome FROM contas").fetchone()[0]
        conector = con.execute("SELECT conector FROM itens").fetchone()[0]
        con.close()
        self.assertEqual(nome, "CONTA CORRENTE ••••2110")
        self.assertEqual(conector, "Banco ••••4332")


class TesteDocumentosEFaturas(Base):
    def test_cpf_declarado_como_cnpj_nao_e_gravado_inteiro(self):
        transacao = tr("d1", "2026-10-01", -10.0, "DEBIT", "PIX ENVIADO", paymentData={
            "receiver": {"name": "Fulano", "documentNumber": {"type": "CNPJ", "value": "987.654.321-00"}}})
        self.sync("conta-bb", [transacao])
        doc = self.con.execute("SELECT contraparte_doc FROM transacoes WHERE id = 'd1'").fetchone()[0]
        self.assertEqual(doc, "CPF ••00")

    def test_datas_de_fatura_sao_de_calendario(self):
        coletor.gravar_faturas(self.con, "cartao", [{"id": "f", "dueDate": "2026-10-11T00:00:00.000Z",
                                                     "billClosingDate": "2026-10-04T00:00:00.000Z", "totalAmount": 1}], AGORA)
        fechamento, vencimento = self.con.execute("SELECT fechamento, vencimento FROM faturas").fetchone()
        self.assertEqual((fechamento, vencimento), ("2026-10-04", "2026-10-11"))


class TesteConversoes(unittest.TestCase):
    def test_centavos(self):
        self.assertEqual(coletor.centavos(1.005), 101)  # int(1.005 * 100) e round() dariam 100
        self.assertEqual(coletor.centavos(4.35), 435)   # int(4.35 * 100) daria 434
        self.assertEqual(coletor.centavos(-0.005), -1)
        self.assertIsNone(coletor.centavos(None))

    def test_cursor(self):
        self.assertEqual(pluggy._extrair_cursor("?accountId=x&after=abc%3D%3D"), "abc==")
        self.assertEqual(pluggy._extrair_cursor("accountId=x&after=xyz"), "xyz")
        with self.assertRaises(pluggy.ErroPluggy):
            pluggy._extrair_cursor("?accountId=x")

    def test_item_confiavel(self):
        self.assertTrue(coletor.item_confiavel(ITEM_OK)[0])
        self.assertFalse(coletor.item_confiavel(dict(ITEM_OK, status="OUTDATED"))[0])
        self.assertFalse(coletor.item_confiavel(dict(ITEM_OK, consentExpiresAt="2020-01-01T00:00:00.000Z"))[0])
        detalhe = {"transactions": {"isUpdated": False}}
        self.assertFalse(coletor.item_confiavel(dict(ITEM_OK, executionStatus="PARTIAL_SUCCESS",
                                                     statusDetail=detalhe))[0])


class TesteMoedaEstrangeira(Base):
    def test_compra_internacional_usa_o_valor_em_reais(self):
        self.sync("cartao", [tr("usd", "2026-10-01", 10.0, "DEBIT", "LOJA EXTERIOR", currencyCode="USD",
                                amountInAccountCurrency=55.3)])
        coletor.definir_convencao(self.con, "cartao", [], "positivo_e_gasto")
        linha = self.con.execute("SELECT valor_centavos, moeda FROM lancamentos WHERE id = 'usd'").fetchone()
        self.assertEqual(tuple(linha), (-5530, "USD"))

    def test_sinal_vem_do_valor_original(self):
        self.assertEqual(coletor.valor_na_moeda_da_conta({"amount": -10.0, "amountInAccountCurrency": 55.3}), -5530)
        self.assertEqual(coletor.valor_na_moeda_da_conta({"amount": 10.0, "amountInAccountCurrency": -55.3}), 5530)
        self.assertEqual(coletor.valor_na_moeda_da_conta({"amount": 10.0}), 1000)

    def test_sem_valor_em_reais_entra_no_aviso(self):
        sem = self.sync("cartao", [tr("usd", "2026-10-01", 10.0, "DEBIT", "LOJA", currencyCode="USD")])
        com = self.sync("cartao", [tr("brl", "2026-10-01", 10.0, "DEBIT", "LOJA")])
        self.assertEqual((sem["sem_conversao"], com["sem_conversao"]), (1, 0))


class TesteJanelaRelida(unittest.TestCase):
    HOJE = date(2026, 12, 2)

    def test_padrao_sao_60_dias(self):
        self.assertEqual(coletor.inicio_da_janela(self.HOJE, "2026-11-02T20:00:05-03:00", None), "2026-10-03")
        self.assertEqual(coletor.inicio_da_janela(self.HOJE, None, None), "2026-10-03")

    def test_coletas_que_falharam_nao_deixam_buraco(self):
        # a última coleta boa foi há três meses: relê desde 7 dias antes dela
        self.assertEqual(coletor.inicio_da_janela(self.HOJE, "2026-09-02T20:00:05-03:00", None), "2026-08-26")

    def test_banco_parado_tambem_alarga_a_janela(self):
        self.assertEqual(coletor.inicio_da_janela(self.HOJE, "2026-11-02T20:00:05-03:00", "2026-08-01T10:00:00.000Z"),
                         "2026-07-25")


class TesteDadosParados(unittest.TestCase):
    def test_banco_sem_sincronizar_ha_mais_de_uma_semana(self):
        self.assertIsNone(coletor.sincronizacao_atrasada("2026-10-30T10:00:00.000Z", AGORA))
        aviso = coletor.sincronizacao_atrasada("2026-10-01T10:00:00.000Z", AGORA)
        self.assertIn("há 30 dias", aviso)
        self.assertIn("01/10/2026", aviso)
        self.assertIsNotNone(coletor.sincronizacao_atrasada(None, AGORA))


class TesteReconexao(Base):
    """Reconexão que cria um item novo: o histórico entregue de novo não conta em dobro."""

    ANTIGO = [tr("a1", "2026-08-10", -100.0, "DEBIT", "MERCADO"), tr("a2", "2026-09-10", -50.0, "DEBIT", "FARMACIA"),
              tr("a3", "2026-10-10", -30.0, "DEBIT", "PADARIA")]
    NOVO = [tr("n2", "2026-09-10", -50.0, "DEBIT", "FARMACIA"), tr("n3", "2026-10-10", -30.0, "DEBIT", "PADARIA")]

    def reconectar(self):
        self.sync("conta-bb", self.ANTIGO)
        coletor.gravar_item(self.con, dict(ITEM_OK, id="item-2"), AGORA)
        coletor.gravar_conta(self.con, dict(CONTA_BB, id="conta-bb-2"), "item-2", AGORA)
        self.sync("conta-bb-2", self.NOVO)  # o item novo reentrega desde setembro

    def ids(self):
        return {linha[0] for linha in self.con.execute("SELECT id FROM lancamentos")}

    def test_item_novo_nao_duplica_o_historico(self):
        self.reconectar()
        self.assertEqual(coletor.marcar_contas_substituidas(self.con, ["item-2"]), 1)
        self.assertEqual(self.ids(), {"a1", "n2", "n3"})  # agosto vem da conta antiga; o resto, da nova
        self.assertEqual(coletor.marcar_contas_substituidas(self.con, ["item-2"]), 0)  # nada muda na segunda vez
        self.assertEqual(self.ids(), {"a1", "n2", "n3"})

    def test_conta_de_outro_tipo_ou_final_nao_e_ligada(self):
        self.reconectar()
        coletor.marcar_contas_substituidas(self.con, ["item-2"])
        livres = {linha[0] for linha in self.con.execute("SELECT id FROM contas WHERE substituida_a_partir IS NULL")}
        self.assertEqual(livres, {"conta-nu", "cartao", "conta-bb-2"})

    def test_item_antigo_de_volta_ao_cadastro_desfaz_a_ligacao(self):
        self.reconectar()
        coletor.marcar_contas_substituidas(self.con, ["item-2"])
        self.assertEqual(coletor.marcar_contas_substituidas(self.con, ["item-1", "item-2"]), 0)
        self.assertEqual(self.ids(), {"a1", "a2", "a3", "n2", "n3"})

    def test_migracao_v5_para_v6(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "v5.sqlite3"
            esquema_v5 = re.sub(r"\n\s*substituida_a_partir\s+TEXT,[^\n]*", "", db._ESQUEMA)
            esquema_v5 = re.sub(r" AND \(c\.substituida_a_partir[^;]*\)", "", esquema_v5)
            self.assertNotIn("substituida_a_partir", esquema_v5)
            antigo = sqlite3.connect(arquivo)
            antigo.executescript(esquema_v5)
            antigo.execute("PRAGMA user_version = 5")
            antigo.commit()
            antigo.close()
            con = db.conectar_arquivo(arquivo)
            colunas = {linha[1] for linha in con.execute("PRAGMA table_info(contas)")}
            visao = con.execute("SELECT sql FROM sqlite_master WHERE name = 'lancamentos'").fetchone()[0]
            versao = con.execute("PRAGMA user_version").fetchone()[0]
            con.close()
        self.assertIn("substituida_a_partir", colunas)
        self.assertIn("substituida_a_partir", visao)
        self.assertEqual(versao, db.VERSAO)


if __name__ == "__main__":
    unittest.main()
