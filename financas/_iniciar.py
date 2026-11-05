"""Iniciador usado pelos atalhos financas.cmd e testes.cmd.

Os atalhos rodam o Python com -I -S: nem a pasta atual nem a pasta deste arquivo entram no
sys.path, variáveis PYTHON* são ignoradas e nenhum pacote instalado é carregado. Aqui a raiz do
projeto entra no FIM do sys.path, depois da biblioteca padrão. Assim, um arquivo com nome de
módulo padrão (json.py, sqlite3.py, unittest.py...) deixado na raiz do projeto nunca é importado
no lugar do original.

Uso (só pelos atalhos): _iniciar.py financas <argumentos> | _iniciar.py unittest <argumentos>
"""

import os
import runpy
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULOS = ("financas", "unittest")


def preparar_caminho(caminho: list[str], raiz: str = RAIZ) -> list[str]:
    """Tira a pasta atual ('') e a raiz do projeto de onde estiverem e põe a raiz no fim."""
    alvo = os.path.normcase(os.path.abspath(raiz))
    return [p for p in caminho if p and os.path.normcase(os.path.abspath(p)) != alvo] + [raiz]


def main() -> None:
    if not (sys.flags.isolated and sys.flags.safe_path and sys.flags.no_site):
        sys.exit("Use os atalhos financas.cmd ou testes.cmd (o Python precisa rodar com -I -S).")
    if len(sys.argv) < 2 or sys.argv[1] not in _MODULOS:
        sys.exit("Uso: _iniciar.py financas|unittest [argumentos]")
    modulo = sys.argv[1]
    sys.path[:] = preparar_caminho(sys.path)
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    runpy.run_module(modulo, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
