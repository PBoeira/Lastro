"""Testes do nome da instituição por item (dados sintéticos)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from financas import coletor, config, db, instituicoes

MEUPLUGGY = {"id": "i", "connector": {"name": "MeuPluggy"}}


def conta_banco(transfer):
    return {"type": "BANK", "bankData": {"transferNumber": transfer}, "name": "Conta"}


class TesteNome(unittest.TestCase):
    def test_codigo_do_banco(self):
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [conta_banco("260/0001/12345678-9")]), "Nubank (via MeuPluggy)")
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [conta_banco("001/1234/98765-4")]),
                         "Banco do Brasil (via MeuPluggy)")
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [conta_banco("1/1234/98765-4")]),
                         "Banco do Brasil (via MeuPluggy)")

    def test_codigo_desconhecido(self):
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [conta_banco("999/1/2")]), "banco 999 (via MeuPluggy)")

    def test_resultado_nunca_tem_agencia_ou_conta(self):
        resultado = instituicoes.nome(MEUPLUGGY, [conta_banco("260/0001/12345678-9")])
        self.assertNotIn("0001", resultado)
        self.assertNotIn("12345678", resultado)

    def test_pelo_nome_do_produto(self):
        cartao_bb = {"type": "CREDIT", "marketingName": "OUROCARD EXEMPLO"}
        cartao_nu = {"type": "CREDIT", "marketingName": "Nubank Cartao Exemplo"}
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [cartao_bb]), "Banco do Brasil (via MeuPluggy)")
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [cartao_nu]), "Nubank (via MeuPluggy)")

    def test_sem_pista(self):
        self.assertEqual(instituicoes.nome(MEUPLUGGY, [{"type": "BANK", "name": "Conta"}]),
                         "MeuPluggy (instituição não identificada)")
        self.assertEqual(instituicoes.nome(MEUPLUGGY, []), "MeuPluggy (instituição não identificada)")

    def test_conexao_direta_usa_o_conector(self):
        item = {"id": "i", "connector": {"name": "Pluggy Bank\x1b[2J"}}
        self.assertEqual(instituicoes.nome(item, []), "Pluggy Bank [2J")


class TesteGravacao(unittest.TestCase):
    def test_grava_e_mantem_instituicao(self):
        con = db.conectar_arquivo(":memory:")
        coletor.gravar_item(con, dict(MEUPLUGGY, status="UPDATED"), "agora", "Nubank (via MeuPluggy)")
        coletor.gravar_item(con, dict(MEUPLUGGY, status="UPDATED"), "depois")  # sem nome: mantém o anterior
        self.assertEqual(con.execute("SELECT instituicao FROM itens").fetchone()[0], "Nubank (via MeuPluggy)")
        con.close()

    def test_migracao_v3_para_v4(self):
        with tempfile.TemporaryDirectory(dir=config.RAIZ) as pasta:
            arquivo = Path(pasta) / "v3.sqlite3"
            antigo = sqlite3.connect(arquivo)
            antigo.execute("CREATE TABLE itens (id TEXT PRIMARY KEY, conector TEXT, sandbox INTEGER, "
                           "open_finance INTEGER, status TEXT, status_execucao TEXT, ultima_sincronizacao TEXT, "
                           "autorizacao_expira TEXT, visto_em TEXT NOT NULL)")
            antigo.execute("INSERT INTO itens (id, conector, visto_em) VALUES ('i', 'MeuPluggy', 'x')")
            antigo.execute("PRAGMA user_version = 3")
            antigo.commit()
            antigo.close()
            con = db.conectar_arquivo(arquivo)
            colunas = {linha[1] for linha in con.execute("PRAGMA table_info(itens)")}
            versao = con.execute("PRAGMA user_version").fetchone()[0]
            con.close()
        self.assertIn("instituicao", colunas)
        self.assertEqual(versao, db.VERSAO)


if __name__ == "__main__":
    unittest.main()
