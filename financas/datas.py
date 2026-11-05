"""Datas da API (ISO 8601 em UTC) convertidas para o horário de Brasília."""

from datetime import date, datetime, timedelta, timezone

# O Brasil não tem horário de verão desde 2019: UTC-3 fixo evita depender do pacote tzdata.
BRT = timezone(timedelta(hours=-3), "BRT")


def ler_iso(texto: str) -> datetime:
    data = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return data.astimezone(BRT)


def dia(texto: str | None) -> str | None:
    """Data AAAA-MM-DD no horário de Brasília. Datas sem hora ('2026-10-31') ficam como estão."""
    if not texto:
        return None
    if "T" not in texto:
        return texto[:10]
    return ler_iso(texto).date().isoformat()


def data_de_calendario(texto: str | None) -> str | None:
    """Datas que são de calendário (vencimento e fechamento de fatura): só a parte AAAA-MM-DD,
    sem converter fuso. '2026-10-04T00:00:00Z' é dia 4, não dia 3 no horário de Brasília."""
    return texto[:10] if texto else None


def formatar(texto: str, com_hora: bool = True) -> str:
    if "T" not in texto:  # data sem hora: não converte fuso (senão vira o dia anterior)
        return date.fromisoformat(texto[:10]).strftime("%d/%m/%Y")
    return ler_iso(texto).strftime("%d/%m/%Y %H:%M" if com_hora else "%d/%m/%Y")


def agora() -> datetime:
    return datetime.now(BRT)
