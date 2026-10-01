"""
Relatório diário de vendas por e-mail (simulando um cenário de uma corretora de seguros).

Lê um CSV de vendas, resume o dia (por padrão, ontem) e envia o resumo
por e-mail para o destinatário informado no arquivo .env.

Colunas esperadas no CSV:
    data (AAAA-MM-DD), vendedor, produto, quantidade, valor_unitario

Uso:
    python relatorio_vendas.py --demo               # gera vendas.csv de exemplo
    python relatorio_vendas.py --dry-run            # gera relatorio.html, sem enviar
    python relatorio_vendas.py                      # envia o e-mail de ontem
    python relatorio_vendas.py --dia 2026-09-29     # envia de um dia específico
"""
import argparse
import logging
import os
import random
import smtplib
import ssl
import sys
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv é opcional
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relatorio_vendas")

COLUNAS = {"data", "vendedor", "produto", "quantidade", "valor_unitario"}

# Comissão estimada por produto (% sobre o valor vendido).
COMISSAO_POR_PRODUTO = {
    "Seguro Auto": 0.12,
    "Seguro Residencial": 0.15,
    "Seguro Vida": 0.20,
    "Seguro Empresarial": 0.10,
}
COMISSAO_PADRAO = 0.10  # usada para produtos que não estão na tabela acima

MEDALHAS = ["🥇", "🥈", "🥉"]
CORES_POSICAO = ["#f9a825", "#90a4ae", "#ef6c00"]
COR_DEMAIS = "#42a5f5"
EMOJI_PRODUTO = {
    "Seguro Auto": "🚗",
    "Seguro Residencial": "🏠",
    "Seguro Vida": "❤️",
    "Seguro Empresarial": "🏢",
}


# dados
def gerar_dados_exemplo(caminho: Path, dias: int = 14) -> None:
    """Cria um CSV fictício para testar o projeto."""
    random.seed(42)
    produtos = {
        "Seguro Auto": 1200.0,
        "Seguro Residencial": 450.0,
        "Seguro Vida": 800.0,
        "Seguro Empresarial": 2500.0,
    }
    vendedores = ["Ana", "Bruno", "Carla", "Diego"]
    linhas = []
    for d in range(dias, 0, -1):
        dia = date.today() - timedelta(days=d)
        for _ in range(random.randint(5, 15)):
            produto = random.choice(list(produtos))
            linhas.append(
                {
                    "data": dia.isoformat(),
                    "vendedor": random.choice(vendedores),
                    "produto": produto,
                    "quantidade": random.randint(1, 3),
                    "valor_unitario": round(produtos[produto] * random.uniform(0.9, 1.1), 2),
                }
            )
    pd.DataFrame(linhas).to_csv(caminho, index=False)
    log.info("Arquivo de exemplo criado: %s", caminho)


def carregar_vendas(caminho: Path) -> pd.DataFrame:
    if not caminho.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {caminho}")
    df = pd.read_csv(caminho)
    faltando = COLUNAS - set(df.columns)
    if faltando:
        raise ValueError(f"Colunas ausentes no CSV: {sorted(faltando)}")
    df["data"] = pd.to_datetime(df["data"]).dt.date
    df["total"] = df["quantidade"] * df["valor_unitario"]
    taxa = df["produto"].map(COMISSAO_POR_PRODUTO).fillna(COMISSAO_PADRAO)
    df["comissao"] = df["total"] * taxa
    return df


def resumir(df: pd.DataFrame, dia: date) -> dict | None:
    do_dia = df[df["data"] == dia]
    if do_dia.empty:
        return None
    anterior = df[df["data"] == dia - timedelta(days=1)]
    total = do_dia["total"].sum()
    total_anterior = anterior["total"].sum()
    variacao = None if total_anterior == 0 else (total - total_anterior) / total_anterior * 100
    por_vendedor = (
        do_dia.groupby("vendedor")
        .agg(total=("total", "sum"), vendas=("total", "size"), comissao=("comissao", "sum"))
        .sort_values("total", ascending=False)
    )
    return {
        "dia": dia,
        "total": total,
        "qtd_vendas": len(do_dia),
        "ticket_medio": total / len(do_dia),
        "variacao": variacao,
        "por_vendedor": por_vendedor,
        "por_produto": do_dia.groupby("produto")["total"].sum().sort_values(ascending=False),
        "comissao_total": do_dia["comissao"].sum(),
        "comissao_media_vendedor": por_vendedor["comissao"].mean(),
        "comissao_media_venda": do_dia["comissao"].mean(),
    }


# formatação
def brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def texto_variacao(variacao: float | None) -> str:
    if variacao is None:
        return "sem dados do dia anterior"
    sinal = "+" if variacao >= 0 else ""
    return f"{sinal}{variacao:.1f}% vs. dia anterior"


def _barra(pct: float, cor: str) -> str:
    pct = max(3, min(100, pct))
    return (
        "<div style='background:#eceff1;border-radius:6px;height:8px;margin-top:6px'>"
        f"<div style='background:{cor};width:{pct:.0f}%;height:8px;border-radius:6px'></div></div>"
    )


def _cartao(emoji: str, rotulo: str, valor: str, cor: str) -> str:
    return (
        "<td style='width:33%;padding:4px;vertical-align:top'>"
        f"<div style='background:#ffffff;border-top:4px solid {cor};border-radius:8px;"
        "padding:12px 6px;text-align:center'>"
        f"<div style='font-size:22px'>{emoji}</div>"
        f"<div style='font-size:11px;color:#607d8b;text-transform:uppercase;margin:4px 0'>{rotulo}</div>"
        f"<div style='font-size:16px;font-weight:bold;color:{cor}'>{valor}</div>"
        "</div></td>"
    )


def _secao(titulo: str, linhas: str) -> str:
    return (
        "<div style='background:#ffffff;border-radius:10px;padding:16px;margin-top:12px'>"
        f"<div style='font-size:16px;font-weight:bold;margin-bottom:8px'>{titulo}</div>"
        f"<table role='presentation' style='width:100%;border-collapse:collapse'>{linhas}</table>"
        "</div>"
    )


def _linhas_ranking(df_v: pd.DataFrame) -> str:
    maior = df_v["total"].max() or 1
    linhas = ""
    for pos, (nome, v) in enumerate(df_v.iterrows()):
        medalha = MEDALHAS[pos] if pos < len(MEDALHAS) else f"{pos + 1}º"
        cor = CORES_POSICAO[pos] if pos < len(CORES_POSICAO) else COR_DEMAIS
        fundo = "#fffde7" if pos == 0 else "#ffffff"
        linhas += (
            f"<tr style='background:{fundo}'>"
            f"<td style='padding:10px 8px;font-size:22px;width:40px'>{medalha}</td>"
            f"<td style='padding:10px 4px'><strong>{nome}</strong>"
            f"<div style='font-size:12px;color:#78909c'>{int(v['vendas'])} vendas · "
            f"comissão est. {brl(v['comissao'])}</div>"
            f"{_barra(v['total'] / maior * 100, cor)}</td>"
            f"<td style='padding:10px 8px;text-align:right;white-space:nowrap'>"
            f"<strong>{brl(v['total'])}</strong></td>"
            "</tr>"
        )
    return linhas


def _linhas_produtos(serie: pd.Series) -> str:
    cores = ["#1565c0", "#6a1b9a", "#00897b", "#ef6c00", "#c2185b"]
    maior = serie.max() or 1
    linhas = ""
    for i, (nome, valor) in enumerate(serie.items()):
        cor = cores[i % len(cores)]
        linhas += (
            "<tr>"
            f"<td style='padding:8px;font-size:20px;width:36px'>{EMOJI_PRODUTO.get(nome, '📦')}</td>"
            f"<td style='padding:8px 4px'>{nome}{_barra(valor / maior * 100, cor)}</td>"
            f"<td style='padding:8px;text-align:right;white-space:nowrap;color:{cor}'>"
            f"<strong>{brl(valor)}</strong></td>"
            "</tr>"
        )
    return linhas


def montar_texto(r: dict) -> str:
    linhas = [
        f"📊 Vendas de {r['dia'].strftime('%d/%m/%Y')}",
        f"💵 Total: {brl(r['total'])} ({texto_variacao(r['variacao'])})",
        f"🧾 Nº de vendas: {r['qtd_vendas']}",
        f"🎯 Ticket médio: {brl(r['ticket_medio'])}",
        "",
        "🏆 Ranking de vendedores:",
    ]
    for pos, (nome, v) in enumerate(r["por_vendedor"].iterrows()):
        marca = MEDALHAS[pos] if pos < len(MEDALHAS) else f"{pos + 1}º"
        linhas.append(f"  {marca} {nome}: {brl(v['total'])} (comissão est. {brl(v['comissao'])})")
    linhas += ["", "📦 Por produto:"]
    linhas += [f"  {EMOJI_PRODUTO.get(n, '📦')} {n}: {brl(v)}" for n, v in r["por_produto"].items()]
    linhas += [
        "",
        "💰 Comissão estimada a receber:",
        f"  Total do dia: {brl(r['comissao_total'])}",
        f"  Média por vendedor: {brl(r['comissao_media_vendedor'])}",
        f"  Média por venda: {brl(r['comissao_media_venda'])}",
    ]
    return "\n".join(linhas)


def montar_html(r: dict) -> str:
    subiu = (r["variacao"] or 0) >= 0
    cor_var = "#a5d6a7" if subiu else "#ef9a9a"
    seta = "📈" if subiu else "📉"
    return (
        "<div style='background:#eef2f7;padding:16px;font-family:Arial,Helvetica,sans-serif;color:#263238'>"
        "<div style='max-width:600px;margin:auto'>"
        # cabeçalho
        "<div style='background:#1565c0;background-image:linear-gradient(135deg,#1565c0,#42a5f5);"
        "border-radius:12px;padding:24px;text-align:center;color:#ffffff'>"
        "<div style='font-size:14px;letter-spacing:1px'>📊 RELATÓRIO DE VENDAS</div>"
        f"<div style='font-size:13px;margin-top:2px'>{r['dia'].strftime('%d/%m/%Y')}</div>"
        f"<div style='font-size:36px;font-weight:bold;margin:12px 0 4px'>{brl(r['total'])}</div>"
        f"<div style='font-size:14px;color:{cor_var}'>{seta} {texto_variacao(r['variacao'])}</div>"
        "</div>"
        # cartões
        "<table role='presentation' style='width:100%;border-collapse:collapse;margin-top:8px'><tr>"
        + _cartao("🧾", "Vendas", str(r["qtd_vendas"]), "#00897b")
        + _cartao("🎯", "Ticket médio", brl(r["ticket_medio"]), "#6a1b9a")
        + _cartao("💰", "Comissão est.", brl(r["comissao_total"]), "#ef6c00")
        + "</tr></table>"
        # ranking e produtos
        + _secao("🏆 Ranking de vendedores", _linhas_ranking(r["por_vendedor"]))
        + _secao("📦 Vendas por produto", _linhas_produtos(r["por_produto"]))
        # comissão
        + "<div style='background:#fff8e1;border:1px solid #ffe082;border-radius:10px;"
        "padding:16px;margin-top:12px'>"
        "<div style='font-size:16px;font-weight:bold;color:#ef6c00'>💰 Comissão estimada a receber</div>"
        f"<div style='font-size:28px;font-weight:bold;color:#2e7d32;margin:8px 0'>{brl(r['comissao_total'])}</div>"
        "<div style='font-size:13px;color:#546e7a;line-height:1.8'>"
        f"👤 Média por vendedor: <strong>{brl(r['comissao_media_vendedor'])}</strong><br>"
        f"🧾 Média por venda: <strong>{brl(r['comissao_media_venda'])}</strong></div>"
        "<div style='font-size:11px;color:#90a4ae;margin-top:8px'>"
        "Estimativa com base nas taxas configuradas por produto; o valor real pode variar.</div>"
        "</div>"
        "<p style='text-align:center;color:#90a4ae;font-size:12px;margin-top:16px'>"
        "🤖 Relatório gerado automaticamente</p>"
        "</div></div>"
    )


# envio
def enviar_email(assunto: str, texto: str, html: str) -> None:
    usuario = os.environ["SMTP_USER"]
    senha = os.environ["SMTP_PASSWORD"]
    destinos = [e.strip() for e in os.environ["EMAIL_DESTINO"].split(",")]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    porta = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = usuario
    msg["To"] = ", ".join(destinos)
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(host, porta, context=ssl.create_default_context()) as servidor:
        servidor.login(usuario, senha)
        servidor.send_message(msg)
    log.info("E-mail enviado para %s", ", ".join(destinos))


# main
def main() -> int:
    p = argparse.ArgumentParser(description="Relatório diário de vendas por e-mail")
    p.add_argument("--csv", type=Path, default=Path("vendas.csv"))
    p.add_argument("--dia", help="AAAA-MM-DD (padrão: ontem)")
    p.add_argument("--demo", action="store_true", help="gera um CSV de exemplo e sai")
    p.add_argument("--dry-run", action="store_true", help="salva relatorio.html sem enviar")
    args = p.parse_args()

    if args.demo:
        gerar_dados_exemplo(args.csv)
        return 0

    dia = (
        datetime.strptime(args.dia, "%Y-%m-%d").date()
        if args.dia
        else date.today() - timedelta(days=1)
    )

    try:
        df = carregar_vendas(args.csv)
    except (FileNotFoundError, ValueError) as erro:
        log.error(erro)
        return 1

    resumo = resumir(df, dia)
    if resumo is None:
        assunto = f"⚠️ [ATENÇÃO] Sem vendas registradas em {dia.strftime('%d/%m/%Y')}"
        texto = "Nenhuma venda encontrada para o dia. Verifique se a exportação dos dados rodou."
        html = f"<p>{texto}</p>"
        log.warning(texto)
    else:
        assunto = f"📊 Vendas de {dia.strftime('%d/%m/%Y')}: {brl(resumo['total'])}"
        texto, html = montar_texto(resumo), montar_html(resumo)

    if args.dry_run:
        Path("relatorio.html").write_text(html, encoding="utf-8")
        log.info("Prévia salva em relatorio.html")
        return 0

    try:
        enviar_email(assunto, texto, html)
    except KeyError as faltando:
        log.error("Variável de ambiente ausente: %s (veja o .env.example)", faltando)
        return 1
    except smtplib.SMTPException as erro:
        log.error("Falha ao enviar e-mail: %s", erro)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
