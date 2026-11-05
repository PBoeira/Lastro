"""Testes da leitura de parcela (metadados da API ou texto da descrição). Dados sintéticos."""

import unittest

from financas import parcelas


def linha(descricao, tipo="CREDIT", numero=None, total=None):
    return {"descricao": descricao, "conta_tipo": tipo, "parcela_numero": numero, "parcela_total": total}


class TesteParcelaDe(unittest.TestCase):
    def test_metadados_vencem(self):
        self.assertEqual(parcelas.parcela_de(linha("LOJA", numero=3, total=10)), (3, 10, "banco"))

    def test_pela_descricao(self):
        self.assertEqual(parcelas.parcela_de(linha("LOJA EXEMPLO PARCELA 3/10")), (3, 10, "descrição"))
        self.assertEqual(parcelas.parcela_de(linha("LOJA EXEMPLO PARC 03/10 X")), (3, 10, "descrição"))
        self.assertEqual(parcelas.parcela_de(linha("FULANO DE TAL 1/2")), (1, 2, "descrição"))

    def test_recusa_o_que_nao_e_parcela(self):
        for descricao in ("UBER 31/10", "LOJA 13/12/2026", "LOJA 1/30", "LOJA 0/5", "LOJA SEM PARCELA"):
            self.assertIsNone(parcelas.parcela_de(linha(descricao)), descricao)

    def test_so_em_cartao(self):
        self.assertIsNone(parcelas.parcela_de(linha("PIX 1/2", tipo="BANK")))


if __name__ == "__main__":
    unittest.main()
