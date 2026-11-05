"""Testes das proteções básicas. Rodar na raiz do projeto: .\\testes.cmd

Nenhum teste grava no Gerenciador de Credenciais nem chama a rede.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from financas import config, credenciais, mascara, pluggy

try:
    import _winapi  # junções, para o teste de local inseguro
except ImportError:
    _winapi = None

ID_FALSO = "0123abcd-0000-4000-8000-00000000beef"


class TesteMascara(unittest.TestCase):
    def test_final(self):
        self.assertEqual(mascara.final(ID_FALSO), "…beef")
        self.assertEqual(mascara.final("abc"), "•••")
        self.assertEqual(mascara.final(None), "?")

    def test_numero(self):
        self.assertEqual(mascara.numero("12345-6"), "••••3456")
        self.assertEqual(mascara.numero("1234"), "••••1234")  # a API já manda só o final do cartão
        self.assertEqual(mascara.numero("12"), "••••")
        self.assertEqual(mascara.numero(""), "••••")
        self.assertEqual(mascara.numero("5502 0911 2233 4455"), "••••4455")

    def test_ids_em_texto(self):
        self.assertNotIn(ID_FALSO, pluggy.mascarar_ids(f"/items/{ID_FALSO}"))


class TesteTextoSeguro(unittest.TestCase):
    def test_mascara_conta_cpf_e_cartao(self):
        bruto = "PIX ENVIADO AG 0001 CC 12345-6 CPF 123.456.789-09 CARTAO 5502 0911 2233 4455"
        limpo = mascara.texto(bruto)
        for proibido in ("12345-6", "123456", "123.456.789-09", "5502 0911"):
            self.assertNotIn(proibido, limpo)
        self.assertIn("AG 0001", limpo)  # agência sozinha (4 dígitos) não é sensível
        self.assertIn("••••3456", limpo)

    def test_outros_separadores(self):
        for bruto in ("5502 0911 2233 4455", "5502_0911_2233_4455", "1234–5678",
                      "1234 - 5678", "1234−5678",
                      "１２３４５６"):  # o último: dígitos de largura total
            limpo = mascara.texto(bruto)
            self.assertIn("••••", limpo, repr(bruto))
            self.assertLessEqual(sum(c.isdigit() for c in limpo), 4, repr(bruto))

    def test_caracteres_invisiveis_nao_escondem_digitos(self):
        for invisivel in ("⁣", "⁠", "﻿", "­", "᠎", "͏", "​"):
            limpo = mascara.texto(f"CONTA 41{invisivel}111")
            self.assertIn("••••", limpo, repr(invisivel))
            self.assertNotIn("41111", limpo.replace(invisivel, ""))

    def test_cpf_parcial(self):
        self.assertNotIn("456.789", mascara.texto("PIX ***.456.789-** FULANO"))

    def test_preserva_datas_e_parcelas(self):
        self.assertEqual(mascara.texto("PARCELA 03/10 EM 31/10/2026"), "PARCELA 03/10 EM 31/10/2026")

    def test_remove_controles_do_terminal(self):
        limpo = mascara.texto("loja\x1b]52;c;ZXZpbA==\x07fim\u202eabc")
        for proibido in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(proibido, limpo)

    def test_idempotente_e_limite(self):
        uma_vez = mascara.texto("CC 12345-6")
        self.assertEqual(mascara.texto(uma_vez), uma_vez)
        self.assertEqual(mascara.texto("abcdef", 3), "abc…")
        self.assertIsNone(mascara.texto(None))


class _AbridorProibido:
    """Rede falsa: se uma chamada recusada chegasse a sair, o teste veria (e nada iria para a internet)."""

    def __init__(self):
        self.pedidos = []

    def open(self, pedido, timeout=None):
        self.pedidos.append(pedido.full_url)
        raise AssertionError("a chamada chegou à rede")


class TesteSomenteLeitura(unittest.TestCase):
    def setUp(self):
        self.cliente = pluggy.Cliente(ID_FALSO, "segredo-falso")
        self.rede = _AbridorProibido()
        self.cliente._abridor = self.rede

    def assertRecusada(self, metodo, caminho, **opcoes):
        with self.assertRaises(pluggy.ErroPluggy, msg=f"{metodo} {caminho}") as recusa:
            self.cliente._enviar(metodo, caminho, **opcoes)
        self.assertEqual(recusa.exception.codigo, "SOMENTE_LEITURA", f"{metodo} {caminho}")

    def tearDown(self):
        self.assertEqual(self.rede.pedidos, [])  # nenhuma chamada saiu, nem a de autenticação

    def test_get_fora_da_lista(self):
        for caminho in ("/connectors", "/items", "/payments/requests", f"/items/{ID_FALSO}/mfa", "/auth"):
            with self.assertRaises(pluggy.ErroPluggy, msg=caminho) as recusa:
                self.cliente._get(caminho)
            self.assertEqual(recusa.exception.codigo, "SOMENTE_LEITURA", caminho)
            self.assertRecusada("GET", caminho, autenticado=False)  # a checagem fica no ponto de rede

    def test_produtos_que_o_projeto_nao_usa(self):
        # Regra do projeto: a autorização do Demo libera identidade, investimentos e
        # empréstimos, mas o código NUNCA lê esses produtos.
        for caminho in ("/identity", f"/identity/{ID_FALSO}", "/investments", f"/investments/{ID_FALSO}",
                        f"/investments/{ID_FALSO}/transactions", "/loans", f"/loans/{ID_FALSO}",
                        "/v2/identity", "/v2/investments", "/v2/loans"):
            self.assertRecusada("GET", caminho, params={"itemId": ID_FALSO}, autenticado=False)
            self.assertRecusada("GET", caminho, params={"itemId": ID_FALSO})

    def test_metodos_de_escrita(self):
        for metodo, caminho in (
            ("POST", "/items"),
            ("PATCH", f"/items/{ID_FALSO}"),
            ("DELETE", f"/items/{ID_FALSO}"),
            ("PUT", "/auth"),
            ("POST", "/payments/requests"),
        ):
            self.assertRecusada(metodo, caminho, autenticado=False)
            self.assertRecusada(metodo, caminho)

    def test_lista_permitida_continua_fechada(self):
        self.assertEqual([p.pattern for p in pluggy._GET_PERMITIDOS],
                         [rf"/items/{pluggy.UUID}", "/accounts", "/v2/transactions", "/bills"])

    def test_repr_sem_segredo(self):
        self.assertNotIn("segredo", repr(self.cliente))
        self.assertNotIn(ID_FALSO, repr(self.cliente))


class TesteVersao(unittest.TestCase):
    def test_user_agent_usa_a_versao_do_pacote(self):
        import financas

        class Resposta(io.BytesIO):
            pass

        class RedeFalsa:
            def __init__(self):
                self.pedidos = []

            def open(self, pedido, timeout=None):
                self.pedidos.append(pedido)
                return Resposta(b'{"id": "x"}')

        cliente = pluggy.Cliente(ID_FALSO, "segredo-falso")
        cliente._abridor = rede = RedeFalsa()
        self.assertEqual(cliente._enviar("GET", f"/items/{ID_FALSO}", autenticado=False), {"id": "x"})
        self.assertEqual(rede.pedidos[0].get_header("User-agent"), f"financas-local/{financas.__version__}")
        self.assertRegex(financas.__version__, r"^\d+\.\d+\.\d+$")


class TesteAbridor(unittest.TestCase):
    def test_so_https_sem_proxy_nem_outros_protocolos(self):
        abridor = pluggy._criar_abridor()
        tipos = {type(h) for h in abridor.handlers}
        for proibido in (urllib.request.ProxyHandler, urllib.request.HTTPHandler, urllib.request.FileHandler,
                         urllib.request.FTPHandler, urllib.request.DataHandler, urllib.request.UnknownHandler):
            self.assertNotIn(proibido, tipos, proibido.__name__)
        https = [h for h in abridor.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        contexto = https[0]._context
        self.assertIsNone(contexto.keylog_filename)
        self.assertTrue(contexto.check_hostname)

    def test_redirecionamento_recusado(self):
        tratador = pluggy._RecusarRedirecionamento()
        with self.assertRaises(pluggy.ErroPluggy):
            tratador.redirect_request(None, None, 302, "Found", {}, "https://outro.exemplo/roubar")


def _erro_http(codigo, corpo: dict):
    return urllib.error.HTTPError("https://api.pluggy.ai/auth", codigo, "erro", {},
                                  io.BytesIO(json.dumps(corpo).encode()))


class TesteRedacaoDeErros(unittest.TestCase):
    SEGREDO = "SEGREDO-FALSO-123"

    def setUp(self):
        self.cliente = pluggy.Cliente(ID_FALSO, self.SEGREDO)

    def test_segredo_na_fronteira_do_corte(self):
        corpo = {"message": "x" * 183 + "clientSecret=" + self.SEGREDO, "codeDescription": self.SEGREDO}
        texto = str(self.cliente._traduzir(_erro_http(401, corpo), "POST", "/auth"))
        for pedaco in ("SEGREDO", "FALSO", "123"):
            self.assertNotIn(pedaco, texto)

    def test_eco_parcial_no_login_nao_aparece(self):
        corpo = {"message": "invalid secret SEGREDO-FA...", "codeDescription": "CLIENT_KEYS_UNAUTHORIZED"}
        erro = self.cliente._traduzir(_erro_http(401, corpo), "POST", "/auth")
        self.assertNotIn("SEGREDO", str(erro))
        self.assertEqual(erro.codigo, "CLIENT_KEYS_UNAUTHORIZED")

    def test_login_so_mostra_codigos_conhecidos(self):
        corpo = {"message": "x", "codeDescription": "SEGREDOFALSO7890"}
        erro = self.cliente._traduzir(_erro_http(401, corpo), "POST", "/auth")
        self.assertIsNone(erro.codigo)
        self.assertNotIn("SEGREDO", str(erro))

    def test_mensagem_com_conta_e_controles(self):
        corpo = {"message": "conta 12345-6 \x1b[2Jbloqueada", "codeDescription": "ERRO\x1b[31m_X"}
        texto = str(self.cliente._traduzir(_erro_http(400, corpo), "GET", f"/items/{ID_FALSO}"))
        for proibido in ("12345-6", "\x1b", ID_FALSO):
            self.assertNotIn(proibido, texto)


class _ClienteFalso(pluggy.Cliente):
    def __init__(self, respostas):
        super().__init__(ID_FALSO, "segredo-falso")
        self.respostas = respostas

    def _enviar(self, metodo, caminho, params=None, corpo=None, autenticado=True):
        return self.respostas(metodo, caminho, params)


class TesteRespostasEstranhas(unittest.TestCase):
    def test_cursor_repetido_para_logo(self):
        chamadas = []

        def resposta(metodo, caminho, params):
            chamadas.append(params)
            return {"results": [], "next": "?accountId=x&after=mesmo"}

        with self.assertRaises(pluggy.ErroPluggy):
            _ClienteFalso(resposta).transacoes("conta")
        self.assertLessEqual(len(chamadas), 3)

    def test_api_key_em_formato_estranho(self):
        for chave in ("abc\r\nX-Injetado: 1", "", None, "curta"):
            with self.assertRaises(pluggy.ErroPluggy, msg=repr(chave)):
                _ClienteFalso(lambda *a, c=chave: {"apiKey": c}).autenticar()


class TesteLocal(unittest.TestCase):
    CASA = Path(r"C:\Users\fulano")

    def test_recusa_pasta_sincronizada(self):
        nuvem = self.CASA / "OneDrive"
        with self.assertRaises(config.LocalInseguro):
            config.verificar_local(nuvem / "Desktop" / "projeto", [nuvem], casa=self.CASA)
        config.verificar_local(self.CASA / "financas", [nuvem], casa=self.CASA)

    def test_recusa_fora_da_pasta_do_usuario(self):
        with self.assertRaises(config.LocalInseguro):
            config.verificar_local(Path(r"D:\financas"), [], casa=self.CASA)

    @unittest.skipUnless(getattr(_winapi, "CreateJunction", None), "só no Windows")
    def test_juncao_para_pasta_sincronizada_e_recusada(self):
        with tempfile.TemporaryDirectory() as pasta:
            casa = Path(pasta)
            nuvem = casa / "OneDrive"
            alvo = nuvem / "dados"
            alvo.mkdir(parents=True)
            juncao = casa / "financas-dados"
            _winapi.CreateJunction(str(alvo), str(juncao))
            try:
                with self.assertRaises(config.LocalInseguro):
                    config.verificar_local(juncao, [nuvem], casa=casa)
                config.verificar_local(casa / "outra", [nuvem], casa=casa)
            finally:
                os.rmdir(juncao)  # remove só a junção, não o alvo

    def test_projeto_atual_ok(self):
        config.verificar_local()
        config.verificar_local(config.PASTA_DADOS_REAIS)

    def test_dados_reais_fora_do_projeto(self):
        from financas import db
        for caminho in (db.caminho("real"), config.pasta_relatorios("real")):
            self.assertTrue(caminho.is_relative_to(config.PASTA_DADOS_REAIS), caminho)
            self.assertFalse(caminho.is_relative_to(config.RAIZ), caminho)
        self.assertTrue(db.caminho("teste").is_relative_to(config.RAIZ / "dados" / "teste"))


class TesteOpcoes(unittest.TestCase):
    def test_vale_o_ultimo_perfil(self):
        from financas.__main__ import criar_analisador
        args = criar_analisador().parse_args(["verificar", "--perfil=real", "--perfil", "teste"])
        self.assertEqual(args.perfil, "teste")

    def test_sem_abreviacao_de_opcoes(self):
        from financas.__main__ import criar_analisador
        for argv in (["categorizar", "--perfil", "real", "--com"], ["coletar", "--perfil", "real", "--sald"],
                     ["verificar", "--perf", "real"]):
            with self.assertRaises(SystemExit, msg=argv):
                criar_analisador().parse_args(argv)

    def test_nenhuma_opcao_aponta_para_outro_lugar(self):
        from financas.__main__ import criar_analisador
        analisador = criar_analisador()
        subcomandos = next(a for a in analisador._actions if a.dest == "comando").choices
        opcoes = {o for sub in subcomandos.values() for a in sub._actions for o in a.option_strings}
        self.assertEqual(opcoes, {"-h", "--help", "--perfil", "--apagar", "--desde", "--saldos", "--detalhar",
                                  "--so-contagens", "--com-somas", "--mes"})


class TesteCredenciais(unittest.TestCase):
    def test_leitura_de_alvo_inexistente(self):
        with self.assertRaises(credenciais.CredencialAusente):
            credenciais._ler_alvo("financas-pluggy-nao-existe-autoteste")

    def test_perfil_invalido(self):
        with self.assertRaises(ValueError):
            credenciais.alvo("producao")


if __name__ == "__main__":
    unittest.main()
