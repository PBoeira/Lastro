"""Testes das proteções de execução: Python isolado nos atalhos (G1), terminal do dono (M1),
confirmação das regras no perfil real (M2), perfil teste só com o Sandbox, --com-somas e --saldos
só no terminal do dono e avisos no painel. Só dados sintéticos, em pastas temporárias do sistema."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from financas import _iniciar, categorizador, coletor, config, db, relatorio
from financas.pluggy import ErroPluggy

INICIADOR = config.RAIZ / "financas" / "_iniciar.py"
AGORA = "2026-11-01T20:00:00-03:00"
PAINEL_FALSO = "<!doctype html><html><head></head><body><main><h1>Painel</h1></main></body></html>"


def rodar(argumentos, pasta):
    return subprocess.run([sys.executable, *argumentos], cwd=pasta, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


class TesteIniciador(unittest.TestCase):
    def test_raiz_vai_para_o_fim_e_pasta_atual_sai(self):
        raiz = str(config.RAIZ)
        caminho = _iniciar.preparar_caminho(["", raiz, r"C:\Python\Lib", raiz + "\\"], raiz)
        self.assertEqual(caminho, [r"C:\Python\Lib", raiz])

    def test_atalhos_usam_modo_isolado_e_o_iniciador(self):
        for nome in ("financas.cmd", "testes.cmd"):
            linhas = [l for l in (config.RAIZ / nome).read_text(encoding="utf-8").splitlines()
                      if l.startswith('"%PY%"')]
            self.assertEqual(len(linhas), 1, nome)
            self.assertIn(" -3.14 -I -S ", linhas[0], nome)
            self.assertIn('"%~dp0financas\\_iniciar.py"', linhas[0], nome)
            self.assertNotIn(" -m ", linhas[0], nome)

    def test_modulo_padrao_falso_na_raiz_nao_e_importado(self):
        # projeto falso: um json.py "plantado" na raiz e um pacote financas que importa json
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            (raiz / "json.py").write_text("raise SystemExit('SOMBRA')\n", encoding="utf-8")
            pacote = raiz / "financas"
            pacote.mkdir()
            (pacote / "__init__.py").write_text("", encoding="utf-8")
            (pacote / "__main__.py").write_text("import json\nprint('JSON=' + json.__file__)\n", encoding="utf-8")
            shutil.copyfile(INICIADOR, pacote / "_iniciar.py")
            antigo = rodar(["-E", "-s", "-S", "-X", "utf8", "-m", "financas"], raiz)
            novo = rodar(["-I", "-S", "-X", "utf8", str(pacote / "_iniciar.py"), "financas"], raiz)
            origem = Path(novo.stdout.strip().removeprefix("JSON=")).resolve()
            plantado = raiz.resolve()
        self.assertIn("SOMBRA", antigo.stderr)  # o jeito antigo (-m) caía na armadilha
        self.assertEqual(novo.returncode, 0, novo.stderr)
        self.assertNotIn("SOMBRA", novo.stdout + novo.stderr)
        self.assertFalse(origem.is_relative_to(plantado))

    def test_sem_modo_isolado_recusa(self):
        resultado = rodar(["-S", str(INICIADOR), "financas", "--help"], tempfile.gettempdir())
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("-I -S", resultado.stderr)

    def test_financas_pelo_iniciador(self):
        resultado = rodar(["-I", "-S", "-X", "utf8", str(INICIADOR), "financas", "--help"], tempfile.gettempdir())
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("usage: financas", resultado.stdout)


class TesteTerminalDoDono(unittest.TestCase):
    def test_entrada_nula_nao_e_terminal_do_dono(self):
        # é assim que rodam os comandos de um assistente de IA e a tarefa agendada (<nul)
        codigo = ("import sys; sys.path.append(sys.argv[1]); from financas import categorizador; "
                  "print(categorizador.console_do_dono())")
        resultado = rodar(["-I", "-S", "-c", codigo, str(config.RAIZ)], tempfile.gettempdir())
        self.assertEqual(resultado.stdout.strip(), "False", resultado.stderr)

    def test_somas_e_saldos_do_real_so_no_terminal_do_dono(self):
        from financas import __main__ as principal
        proibido = mock.Mock(side_effect=AssertionError("não podia rodar"))
        with mock.patch.object(categorizador, "console_do_dono", lambda: False), \
                mock.patch.object(coletor, "coletar", proibido), \
                mock.patch.object(categorizador, "categorizar", proibido), \
                mock.patch.object(relatorio, "avisar_coleta_falhou", proibido), \
                redirect_stderr(io.StringIO()) as tela:
            self.assertEqual(principal.main(["coletar", "--perfil", "real", "--saldos"]), 2)
            self.assertEqual(principal.main(["categorizar", "--perfil", "real", "--com-somas"]), 2)
        proibido.assert_not_called()
        self.assertIn("terminal do dono", tela.getvalue())
        with mock.patch.object(categorizador, "console_do_dono", lambda: True), \
                mock.patch.object(coletor, "coletar", lambda *argumentos: 0):
            self.assertEqual(principal.main(["coletar", "--perfil", "real", "--saldos"]), 0)  # no terminal do dono


class TesteConfirmacaoDasRegras(unittest.TestCase):
    def test_digital_igual_a_de_antes(self):
        # a mesma conta de antes: a confirmação já gravada continua valendo
        with tempfile.TemporaryDirectory() as pasta:
            regras = Path(pasta) / "regras.toml"
            regras.write_bytes(b"x = 1\n")
            ausente = Path(pasta) / "ajustes.local.toml"
            esperado = hashlib.sha256(b"regras.toml" + b"x = 1\n" + b"ajustes.local.toml" + b"-").hexdigest()
            self.assertEqual(categorizador.impressao_digital_regras([regras, ausente]), esperado)
            conteudos = categorizador.fotografar_regras([regras, ausente])
            self.assertEqual(categorizador.impressao_digital_regras(conteudos=conteudos), esperado)

    def test_regras_aplicadas_sao_as_da_mesma_leitura(self):
        # a digital e as regras aplicadas vêm dos mesmos bytes: trocar o arquivo no meio não muda nada
        with tempfile.TemporaryDirectory() as pasta:
            local = Path(pasta) / "regras.local.toml"
            ajustes = Path(pasta) / "ajustes.local.toml"
            local.write_text('[categorias]\n"Categoria Sintetica" = "despesa"\n', encoding="utf-8")
            ajustes.write_text('[lancamentos]\n"abc1234567" = "Mercado"\n', encoding="utf-8")
            conteudos = categorizador.fotografar_regras([config.CAMINHO_REGRAS, local, ajustes])
            local.write_text("# trocado depois da leitura\n", encoding="utf-8")
            ajustes.write_text("# trocado depois da leitura\n", encoding="utf-8")
            regras = categorizador.carregar_regras(caminho_local=local, conteudos=conteudos)
            lidos = categorizador.carregar_ajustes(regras.categorias, ajustes, conteudos)
        self.assertIn("Categoria Sintetica", regras.categorias)
        self.assertEqual(lidos, {"abc1234567": "Mercado"})

    def test_dono_confirma_digitando(self):
        with tempfile.TemporaryDirectory() as pasta:
            marca = Path(pasta) / "regras-confirmadas.sha256"
            conteudos = {Path(pasta) / "regras.local.toml": b"# v2\n"}
            with mock.patch.object(categorizador, "_marca_regras", lambda perfil: marca), \
                    mock.patch.object(categorizador, "console_do_dono", lambda: True), \
                    redirect_stdout(io.StringIO()):
                with mock.patch("builtins.input", lambda _: ""):
                    self.assertFalse(categorizador.liberar_regras("real", conteudos))  # Enter: nada muda
                self.assertFalse(marca.exists())
                with mock.patch("builtins.input", lambda _: "confirmo "):
                    self.assertTrue(categorizador.liberar_regras("real", conteudos))
                self.assertEqual(marca.read_text(encoding="ascii"),
                                 categorizador.impressao_digital_regras(conteudos=conteudos))
                with mock.patch("builtins.input", side_effect=AssertionError("não devia perguntar de novo")):
                    self.assertTrue(categorizador.liberar_regras("real", conteudos))

    def test_regras_com_erro_nao_chegam_a_ser_confirmadas(self):
        with tempfile.TemporaryDirectory() as pasta:
            marca = Path(pasta) / "regras-confirmadas.sha256"
            conteudos = {Path(pasta) / "regras.local.toml": b"[[regra]\n"}
            erro = categorizador.ErroRegras("regras.local.toml: erro de formato")
            with mock.patch.object(categorizador, "_marca_regras", lambda perfil: marca), \
                    mock.patch.object(categorizador, "console_do_dono", lambda: True), \
                    mock.patch.object(categorizador, "carregar_regras", side_effect=erro), \
                    mock.patch("builtins.input", side_effect=AssertionError("não devia perguntar")), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(categorizador.ErroRegras):
                    categorizador.liberar_regras("real", conteudos)
            self.assertFalse(marca.exists())

    def test_aviso_que_falha_nao_muda_o_codigo_de_saida(self):
        with tempfile.TemporaryDirectory() as pasta:
            marca = Path(pasta) / "regras-confirmadas.sha256"
            with mock.patch.object(categorizador, "_marca_regras", lambda perfil: marca), \
                    mock.patch.object(categorizador, "console_do_dono", lambda: False), \
                    mock.patch.object(relatorio, "avisar_regras_nao_confirmadas", side_effect=OSError("disco")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as tela:
                self.assertFalse(categorizador.liberar_regras("real", {Path(pasta) / "regras.toml": b"x = 1\n"}))
        self.assertIn("não foi possível marcar o painel", tela.getvalue())


class TesteRotinaMensal(unittest.TestCase):
    TEXTO = (config.RAIZ / "mensal.cmd").read_bytes()

    def test_toda_chamada_com_entrada_nula_e_saida_no_log(self):
        linhas = self.TEXTO.decode("ascii").splitlines()
        chamadas = [l for l in linhas if l.lstrip().lower().startswith("call ")]
        self.assertEqual(len(chamadas), 3)
        for comando, linha in zip(("coletar", "categorizar", "relatorio"), chamadas):
            self.assertIn(f'financas.cmd" {comando} --perfil real <nul >> "%LOG%" 2>&1', linha)
        codigo = "\n".join(l for l in linhas if not l.lstrip().lower().startswith("rem "))
        for proibido in ("--com-somas", "--saldos", "--detalhar", "--desde", "goto", "\n:"):
            self.assertNotIn(proibido, codigo, proibido)  # sem rótulos: o cmd erra rótulos em arquivo LF

    def test_codigos_de_saida(self):
        texto = self.TEXTO.decode("ascii")
        self.assertIn('if "%RC%"=="3"', texto)  # só o 3 é "regras não confirmadas" (o 130 do Ctrl+C não)
        self.assertNotIn("if errorlevel", texto)  # errorlevel não pega códigos negativos
        self.assertEqual(texto.count("ERRO "), 3)  # coleta, categorização e relatório

    def test_atalhos_em_crlf(self):
        for nome in ("financas.cmd", "testes.cmd", "mensal.cmd"):
            dados = (config.RAIZ / nome).read_bytes()
            self.assertNotIn(b"\n", dados.replace(b"\r\n", b""), nome)


class TestePermissoesDoAssistente(unittest.TestCase):
    """As regras allow dos settings do assistente de IA (o .claude/settings.local.json fica só no computador
    de cada pessoa) nunca liberam sem pedido um comando com curinga ou que mostre valores do perfil real."""

    def test_allow_exato_e_sem_opcoes_que_mostram_valores(self):
        regras = []
        for nome in ("settings.json", "settings.local.json"):
            arquivo = config.RAIZ / ".claude" / nome
            if arquivo.exists():
                regras += json.loads(arquivo.read_text(encoding="utf-8")).get("permissions", {}).get("allow", [])
        for numero, regra in enumerate(regras, 1):
            if not regra.startswith(("Bash(", "PowerShell(")):
                continue
            self.assertNotIn("*", regra, f"regra allow {numero} com curinga")
            if "real" in regra:
                for opcao in ("--com-somas", "--saldos", "--detalhar", "mensal.cmd"):
                    self.assertNotIn(opcao, regra, f"regra allow {numero} com {opcao} no real")


class TestePerfilTesteSoComSandbox(unittest.TestCase):
    def _banco(self, pasta, sandbox):
        arquivo = Path(pasta) / "financas.sqlite3"
        con = db.conectar_arquivo(arquivo)
        coletor.gravar_item(con, {"id": "item-x", "connector": {"name": "Banco", "isSandbox": sandbox}}, AGORA)
        con.commit()
        con.close()
        return arquivo

    def test_banco_com_item_que_nao_e_do_sandbox_e_recusado(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = self._banco(pasta, False)  # como um banco real copiado para dados\teste
            with mock.patch.object(db, "caminho", lambda perfil: arquivo), \
                    mock.patch.object(db, "_verificar_pasta_teste", lambda caminho: None):
                with self.assertRaises(config.LocalInseguro):
                    db.conectar("teste")

    def test_banco_do_sandbox_abre(self):
        with tempfile.TemporaryDirectory() as pasta:
            arquivo = self._banco(pasta, True)
            with mock.patch.object(db, "caminho", lambda perfil: arquivo), \
                    mock.patch.object(db, "_verificar_pasta_teste", lambda caminho: None):
                db.conectar("teste").close()

    def test_pasta_do_projeto_passa(self):
        db._verificar_pasta_teste(db.caminho("teste"))

    @unittest.skipUnless(sys.platform == "win32", "junção só existe no Windows")
    def test_pasta_que_e_link_e_recusada(self):
        import _winapi
        with tempfile.TemporaryDirectory() as raiz_falsa, tempfile.TemporaryDirectory() as outro_lugar:
            raiz = Path(raiz_falsa).resolve()
            _winapi.CreateJunction(str(Path(outro_lugar).resolve()), str(raiz / "dados"))
            try:
                with mock.patch.object(config, "RAIZ", raiz):
                    with self.assertRaises(config.LocalInseguro):
                        db._verificar_pasta_teste(raiz / "dados" / "teste" / "financas.sqlite3")
            finally:
                os.rmdir(raiz / "dados")  # tira só a junção, não o destino

    def test_coleta_do_teste_recusa_item_que_nao_e_do_sandbox(self):
        class ClienteFalso:
            def item(self, item_id):
                return {"id": item_id, "status": "UPDATED", "connector": {"name": "Banco", "isSandbox": False}}

            def contas(self, item_id):
                raise AssertionError("não devia chegar às contas")

        with tempfile.TemporaryDirectory() as pasta:
            arquivo = Path(pasta) / "financas.sqlite3"
            with mock.patch.object(config, "itens", lambda perfil: ["item-x"]), \
                    mock.patch.object(config, "carregar", lambda: {"perfis": {}}), \
                    mock.patch.object(coletor, "cliente_do_perfil", lambda perfil: ClienteFalso()), \
                    mock.patch.object(db, "conectar", lambda perfil: db.conectar_arquivo(arquivo)), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(config.LocalInseguro):
                    coletor.coletar("teste")
            con = db.conectar_arquivo(arquivo)
            itens = con.execute("SELECT COUNT(*) FROM itens").fetchone()[0]
            con.close()
        self.assertEqual(itens, 0)  # nada foi gravado


class TesteAvisoNoPainel(unittest.TestCase):
    def _painel(self, pasta):
        painel = Path(pasta) / "painel.html"
        painel.write_text(PAINEL_FALSO, encoding="utf-8")
        return painel

    def test_aviso_no_topo_uma_vez(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            with mock.patch.object(config, "pasta_relatorios", lambda perfil: Path(pasta)):
                relatorio.avisar_regras_nao_confirmadas("real")
                relatorio.avisar_regras_nao_confirmadas("real")  # a segunda vez troca, não repete
            texto = painel.read_text(encoding="utf-8")
            sobrou_temporario = (Path(pasta) / "painel.tmp").exists()
        self.assertEqual(texto.count("Painel desatualizado"), 1)
        self.assertLess(texto.index("Painel desatualizado"), texto.index("<h1>"))
        self.assertIn("CONFIRMO", texto)
        self.assertFalse(sobrou_temporario)

    def test_aviso_de_coleta_que_falhou(self):
        with tempfile.TemporaryDirectory() as pasta:
            painel = self._painel(pasta)
            with mock.patch.object(config, "pasta_relatorios", lambda perfil: Path(pasta)):
                relatorio.avisar_coleta_falhou("real")
                relatorio.avisar_regras_nao_confirmadas("real")
                relatorio.avisar_coleta_falhou("real")  # troca o aviso antigo de coleta pelo novo
            texto = painel.read_text(encoding="utf-8")
        self.assertEqual(texto.count("a coleta falhou em"), 1)
        self.assertIn("mensal.log", texto)
        self.assertEqual(texto.count("Painel desatualizado"), 2)  # um de coleta e um de regras
        self.assertLess(texto.index("Painel desatualizado"), texto.index("<h1>"))

    def test_coleta_do_real_que_falha_marca_o_painel(self):
        from financas import __main__ as principal

        def falha(*argumentos):
            raise ErroPluggy("sem rede")

        avisos = []
        with mock.patch.object(coletor, "coletar", falha), \
                mock.patch.object(relatorio, "avisar_coleta_falhou", avisos.append), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(principal.main(["coletar", "--perfil", "real"]), 1)
            self.assertEqual(principal.main(["coletar", "--perfil", "teste"]), 1)  # no teste, o painel real não muda
        self.assertEqual(avisos, ["real"])

    def test_sem_painel_nao_cria_nada(self):
        with tempfile.TemporaryDirectory() as pasta:
            with mock.patch.object(config, "pasta_relatorios", lambda perfil: Path(pasta)):
                relatorio.avisar_regras_nao_confirmadas("real")
                relatorio.avisar_coleta_falhou("real")
            self.assertEqual(list(Path(pasta).iterdir()), [])
