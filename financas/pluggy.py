"""Cliente mínimo e SOMENTE LEITURA da API Pluggy.

Únicas chamadas que este módulo consegue fazer (qualquer outra é recusada
antes de sair da máquina, em Cliente._enviar):
    POST https://api.pluggy.ai/auth                         troca Client ID + Secret por uma apiKey (vale 2 h)
    GET  https://api.pluggy.ai/items/{id}                   status de uma conexão
    GET  https://api.pluggy.ai/accounts?itemId=             contas de uma conexão
    GET  https://api.pluggy.ai/v2/transactions?accountId=   lançamentos de uma conta (cursor)
    GET  https://api.pluggy.ai/bills?accountId=             faturas de um cartão

Regra do projeto: a autorização do MeuPluggy libera também identidade,
investimentos e empréstimos, mas este código NUNCA lê esses produtos
(/identity, /investments, /loans). Não acrescente esses caminhos à lista.

Proteções:
  * host fixo (api.pluggy.ai), só HTTPS com certificado verificado;
  * o abridor de URLs só tem o tratador HTTPS: sem http, file, ftp, data e sem proxy;
  * redirecionamentos recusados (evita mandar a apiKey para outro endereço);
  * SSLKEYLOGFILE ignorado: as chaves TLS nunca vão para arquivo;
  * mensagens de erro sem segredo, sem apiKey, sem corpo bruto da resposta,
    sem caracteres de controle e com IDs e números longos mascarados.
"""

import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, mascara

HOST = "api.pluggy.ai"
BASE = f"https://{HOST}"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(UUID)

_GET_PERMITIDOS = (
    re.compile(rf"/items/{UUID}"),
    re.compile(r"/accounts"),
    re.compile(r"/v2/transactions"),
    re.compile(r"/bills"),
)
_API_KEY_RE = re.compile(r"[A-Za-z0-9._\-]{10,4096}")
_CODIGOS_AUTH_CONHECIDOS = {"CLIENT_KEYS_UNAUTHORIZED", "CLIENT_DISABLED"}

_TIMEOUT_S = 30
_MAX_TENTATIVAS = 4
_VALIDADE_CHAVE_S = 2 * 60 * 60 - 5 * 60  # 2 h, menos 5 min de folga
_MAX_PAGINAS = 2000  # trava contra laço infinito de paginação


class ErroPluggy(Exception):
    def __init__(self, mensagem: str, status=None, codigo=None):
        super().__init__(mensagem)
        self.status = status
        self.codigo = codigo


class _RecusarRedirecionamento(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ErroPluggy(
            f"A API respondeu com redirecionamento (HTTP {code}); recusado por segurança.",
            status=code,
            codigo="REDIRECIONAMENTO",
        )


def _criar_abridor() -> urllib.request.OpenerDirector:
    contexto = ssl.create_default_context()  # verifica certificado e nome do host
    contexto.keylog_filename = None          # nunca grava chaves TLS, mesmo com SSLKEYLOGFILE definido
    abridor = urllib.request.OpenerDirector()
    for tratador in (
        urllib.request.HTTPSHandler(context=contexto),
        _RecusarRedirecionamento(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        abridor.add_handler(tratador)
    return abridor


def _espera(cabecalhos) -> int:
    for nome in ("RateLimit-Reset", "Retry-After"):
        try:
            return min(max(int(cabecalhos.get(nome)), 1), 120)
        except (TypeError, ValueError):
            continue
    return 60


def mascarar_ids(texto: str) -> str:
    return _UUID_RE.sub(lambda m: mascara.final(m.group(0)), texto)


def _extrair_cursor(proximo: str) -> str:
    """O campo 'next' vem como query string pronta ('?accountId=...&after=...'); usamos só o 'after'."""
    consulta = proximo.split("?", 1)[-1]
    valores = urllib.parse.parse_qs(consulta).get("after")
    if not valores or not valores[0]:
        raise ErroPluggy("Paginação inesperada: campo 'next' sem cursor 'after'.")
    return valores[0]


class Cliente:
    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_key = None
        self._chave_expira_em = 0.0
        self._abridor = _criar_abridor()

    def __repr__(self) -> str:  # nunca mostrar segredos
        return "<pluggy.Cliente>"

    # ---------- chamadas públicas ----------

    def autenticar(self) -> None:
        resposta = self._enviar(
            "POST",
            "/auth",
            corpo={"clientId": self._client_id, "clientSecret": self._client_secret},
            autenticado=False,
        )
        chave = resposta.get("apiKey") if isinstance(resposta, dict) else None
        if not isinstance(chave, str) or not _API_KEY_RE.fullmatch(chave):
            raise ErroPluggy("A autenticação não devolveu uma apiKey em formato válido.")
        self._api_key = chave
        self._chave_expira_em = time.monotonic() + _VALIDADE_CHAVE_S

    def item(self, item_id: str) -> dict:
        return self._get(f"/items/{item_id}")

    def contas(self, item_id: str) -> list[dict]:
        return self._paginas_numeradas("/accounts", {"itemId": item_id})

    def faturas(self, conta_id: str) -> list[dict]:
        return self._paginas_numeradas("/bills", {"accountId": conta_id})

    def transacoes(self, conta_id: str, data_de: str | None = None) -> list[dict]:
        """Todos os lançamentos da conta com data >= data_de (AAAA-MM-DD), seguindo o cursor."""
        filtros = {"accountId": conta_id}
        if data_de:
            filtros["dateFrom"] = data_de
        params = dict(filtros)
        resultados: list[dict] = []
        cursores: set[str] = set()
        for _ in range(_MAX_PAGINAS):
            resposta = self._get("/v2/transactions", params)
            resultados.extend(resposta.get("results") or [])
            proximo = resposta.get("next")
            if not proximo:
                return resultados
            cursor = _extrair_cursor(proximo)
            if cursor in cursores:
                raise ErroPluggy("Paginação inesperada: a API repetiu o mesmo cursor.")
            cursores.add(cursor)
            params = {**filtros, "after": cursor}
        raise ErroPluggy("Paginação de lançamentos passou do limite de segurança.")

    # ---------- internas ----------

    def _paginas_numeradas(self, caminho: str, filtros: dict) -> list[dict]:
        resultados: list[dict] = []
        pagina = 1
        for _ in range(_MAX_PAGINAS):
            params = dict(filtros) if pagina == 1 else {**filtros, "page": pagina}
            resposta = self._get(caminho, params)
            resultados.extend(resposta.get("results") or [])
            total_paginas = int(resposta.get("totalPages") or 1)
            if pagina >= total_paginas:
                return resultados
            pagina += 1
        raise ErroPluggy(f"Paginação de {caminho} passou do limite de segurança.")

    def _chave(self) -> str:
        if not self._api_key or time.monotonic() >= self._chave_expira_em:
            self.autenticar()
        return self._api_key

    def _get(self, caminho: str, params=None):
        return self._enviar("GET", caminho, params=params)

    def _enviar(self, metodo: str, caminho: str, params=None, corpo=None, autenticado: bool = True):
        # A lista de chamadas permitidas fica aqui, no único ponto que acessa a rede.
        if metodo == "POST":
            permitido = caminho == "/auth"
        elif metodo == "GET":
            permitido = any(padrao.fullmatch(caminho) for padrao in _GET_PERMITIDOS)
        else:
            permitido = False
        if not permitido:
            raise ErroPluggy(f"Chamada recusada (somente leitura): {metodo} {mascarar_ids(caminho)}",
                             codigo="SOMENTE_LEITURA")
        url = BASE + caminho
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if urllib.parse.urlsplit(url).hostname != HOST:
            raise ErroPluggy("Endereço inesperado; chamada recusada.")
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None

        for tentativa in range(1, _MAX_TENTATIVAS + 1):
            cabecalhos = {"Accept": "application/json", "User-Agent": f"financas-local/{__version__}"}
            if dados is not None:
                cabecalhos["Content-Type"] = "application/json"
            if autenticado:
                cabecalhos["X-API-KEY"] = self._chave()
            pedido = urllib.request.Request(url, data=dados, headers=cabecalhos, method=metodo)
            try:
                with self._abridor.open(pedido, timeout=_TIMEOUT_S) as resposta:
                    bruto = resposta.read()
                try:
                    return json.loads(bruto.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ErroPluggy(f"Resposta inválida da API em {metodo} {mascarar_ids(caminho)}.") from None
            except urllib.error.HTTPError as erro:
                if erro.code == 429 and tentativa < _MAX_TENTATIVAS:
                    time.sleep(_espera(erro.headers))
                    continue
                if erro.code in (500, 502, 503, 504) and tentativa < _MAX_TENTATIVAS:
                    time.sleep(5 * tentativa)
                    continue
                if erro.code == 401 and autenticado and tentativa == 1:
                    self._api_key = None  # apiKey vencida: renova uma vez e tenta de novo
                    continue
                raise self._traduzir(erro, metodo, caminho) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as erro:
                if tentativa < _MAX_TENTATIVAS:
                    time.sleep(5 * tentativa)
                    continue
                motivo = self._limpar(str(getattr(erro, "reason", None) or type(erro).__name__))[:120]
                raise ErroPluggy(f"Falha de rede ao acessar {HOST}: {motivo}", codigo="REDE") from None
        raise ErroPluggy(f"Sem resposta da API em {metodo} {mascarar_ids(caminho)}.")

    def _limpar(self, texto: str) -> str:
        """Remove segredos ANTES de qualquer corte, depois mascara IDs, números longos e controles."""
        for segredo in (self._client_secret, self._api_key, self._client_id):
            if segredo:
                texto = texto.replace(segredo, "***")
        return mascara.texto(mascarar_ids(texto)) or ""

    def _traduzir(self, erro: urllib.error.HTTPError, metodo: str, caminho: str) -> ErroPluggy:
        codigo, mensagem = None, ""
        try:
            corpo = json.loads(erro.read().decode("utf-8", "replace"))
            if isinstance(corpo, dict):
                codigo = corpo.get("codeDescription") or corpo.get("code")
                mensagem = str(corpo.get("message", ""))
        except Exception:
            pass
        finally:
            erro.close()
        # No login, a mensagem do servidor nunca é mostrada: um eco parcial do secret
        # não seria reconhecido pela remoção. Ficam só o status e o código.
        mensagem = "" if caminho == "/auth" else self._limpar(mensagem)[:200]
        if codigo is not None:
            codigo = re.sub(r"[^A-Za-z0-9_]", "", self._limpar(str(codigo)))[:50] or None
        if caminho == "/auth" and codigo not in _CODIGOS_AUTH_CONHECIDOS:
            codigo = None  # no login, só códigos conhecidos: nada que possa ecoar o secret
        texto = f"HTTP {erro.code} em {metodo} {mascarar_ids(caminho)}"
        if codigo:
            texto += f" ({codigo})"
        if mensagem:
            texto += f": {mensagem}"
        if erro.code == 401 and caminho == "/auth":
            texto += ". Confira o Client ID e o Client Secret, e se a aplicação está ativa no dashboard."
        return ErroPluggy(texto, status=erro.code, codigo=codigo)
