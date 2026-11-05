"""Caminhos do projeto e configuração local (itemIds por perfil).

O arquivo config.local.json fica na raiz do projeto, está no .gitignore e não
guarda segredo: o Client Secret fica no Gerenciador de Credenciais do Windows.
"""

import json
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CAMINHO_CONFIG = RAIZ / "config.local.json"
CAMINHO_REGRAS = RAIZ / "regras.toml"              # genéricas: vão para o repositório
CAMINHO_REGRAS_LOCAL = RAIZ / "regras.local.toml"  # pessoais: ficam fora do repositório
CAMINHO_AJUSTES = RAIZ / "ajustes.local.toml"      # decisões por lançamento: fora do repositório
PERFIS = ("teste", "real")
# Banco e relatórios reais ficam FORA da pasta do projeto (que é a pasta aberta no assistente de IA),
# numa pasta fixa da pasta do usuário. Não há opção de linha de comando nem configuração para mudá-la.
PASTA_DADOS_REAIS = Path.home() / "financas-dados"


def pasta_relatorios(perfil: str) -> Path:
    """Relatórios reais na pasta de dados reais, fora do projeto; os de teste ficam junto dos dados de teste."""
    if perfil not in PERFIS:
        raise ValueError("perfil inválido")
    return PASTA_DADOS_REAIS / "relatorios" if perfil == "real" else RAIZ / "dados" / perfil / "relatorios"


class LocalInseguro(RuntimeError):
    pass


def _pastas_sincronizadas():
    for variavel in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        valor = os.environ.get(variavel)
        if valor:
            yield Path(valor)
    casa = Path.home()
    for nome in ("OneDrive", "Dropbox", "Google Drive", "My Drive", "iCloudDrive", "iCloud Drive",
                 "Box", "Nextcloud", "MEGA", "pCloudDrive", "Sync"):
        yield casa / nome


def verificar_local(raiz: Path = RAIZ, pastas=None, casa: Path | None = None) -> None:
    """Recusa rodar se o projeto estiver fora da pasta do usuário (que só o usuário,
    o sistema e os administradores leem) ou dentro de uma pasta sincronizada com a nuvem.
    Backups de pastas escolhidas à mão (Google Drive, Dropbox) não são detectáveis:
    não inclua esta pasta neles. Os caminhos são resolvidos antes da comparação: um link ou uma
    junção que aponte para dentro de uma pasta sincronizada também é recusado."""
    casa = (casa or Path.home()).resolve()
    raiz = raiz.resolve()
    if not raiz.is_relative_to(casa):
        raise LocalInseguro(
            f"O projeto precisa ficar dentro da sua pasta de usuário ({casa}) para herdar permissões restritas."
        )
    for pasta in pastas if pastas is not None else _pastas_sincronizadas():
        if raiz.is_relative_to(pasta) or raiz.is_relative_to(pasta.resolve()):
            raise LocalInseguro(
                f"O projeto está dentro de uma pasta sincronizada com a nuvem ({pasta}). "
                "Mova a pasta do projeto para fora dela antes de continuar."
            )


def carregar() -> dict:
    if not CAMINHO_CONFIG.exists():
        return {"perfis": {}}
    with CAMINHO_CONFIG.open(encoding="utf-8") as arquivo:
        dados = json.load(arquivo)
    dados.setdefault("perfis", {})
    return dados


def salvar(dados: dict) -> None:
    temporario = CAMINHO_CONFIG.with_name("config.local.tmp")
    with temporario.open("w", encoding="utf-8") as arquivo:
        json.dump(dados, arquivo, ensure_ascii=False, indent=2)
    os.replace(temporario, CAMINHO_CONFIG)


def itens(perfil: str) -> list[str]:
    return list(carregar()["perfis"].get(perfil, {}).get("itens", []))


def definir_itens(perfil: str, ids: list[str]) -> None:
    dados = carregar()
    dados["perfis"].setdefault(perfil, {})["itens"] = ids
    salvar(dados)


def remover_perfil(perfil: str) -> None:
    dados = carregar()
    if dados["perfis"].pop(perfil, None) is not None:
        salvar(dados)
