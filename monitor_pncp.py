#!/usr/bin/env python3
"""
Monitor PNCP — avisa sobre publicações novas contendo os termos configurados
(padrão: leilão / leiloeiro / leiloeira).

Funcionamento: consulta a busca pública do PNCP, compara com as publicações já
vistas (arquivo vistos.json) e envia alerta das novas por Telegram e/ou e-mail.
Sem dependências externas (apenas biblioteca padrão do Python 3.9+).
"""
import json
import os
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

# ---------------- Configuração (via variáveis de ambiente) ----------------
TERMOS = [t.strip() for t in os.getenv("TERMOS", "leilão,leiloeiro,leiloeira,leilao").split(",") if t.strip()]
TIPOS = [t.strip() for t in os.getenv("TIPOS_DOCUMENTO", "edital,ata,contrato").split(",") if t.strip()]
UFS = {u.strip().upper() for u in os.getenv("UFS", "").split(",") if u.strip()}  # vazio = Brasil todo
STATUS = os.getenv("STATUS", "").strip()          # vazio = sem filtro de situação
PAGINAS = int(os.getenv("PAGINAS", "2"))          # páginas por termo/tipo a cada execução
TAM_PAGINA = 50
STATE_FILE = Path(os.getenv("STATE_FILE", "vistos.json"))
MAX_ESTADO = 20000

BUSCA_URL = "https://pncp.gov.br/api/search/"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_DE = os.getenv("EMAIL_DE", SMTP_USER)
EMAIL_PARA = os.getenv("EMAIL_PARA", "")


# ---------------- Utilidades ----------------
def normalizar(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


TERMOS_NORM = {normalizar(t) for t in TERMOS}


def http_get_json(url, tentativas=3):
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (monitor-pncp)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            if i == tentativas - 1:
                raise
            time.sleep(5 * (i + 1))


def buscar(termo, tipo, pagina):
    params = {
        "q": termo,
        "tipos_documento": tipo,
        "ordenacao": "-data",
        "pagina": pagina,
        "tam_pagina": TAM_PAGINA,
    }
    if STATUS:
        params["status"] = STATUS
    data = http_get_json(BUSCA_URL + "?" + urllib.parse.urlencode(params))
    return data.get("items") or []


def chave(it):
    return str(it.get("numero_controle_pncp") or it.get("id") or it.get("item_url") or "").strip()


def relevante(it):
    if UFS and str(it.get("uf", "")).upper() not in UFS:
        return False
    texto = normalizar(" ".join(str(it.get(k) or "") for k in ("title", "description")))
    if not texto.strip():
        return True  # formato inesperado: melhor avisar do que perder
    return any(t in texto for t in TERMOS_NORM)


def link(it):
    u = str(it.get("item_url") or "")
    if u.startswith("/compras/"):
        return "https://pncp.gov.br/app/editais/" + u[len("/compras/"):]
    if u.startswith("/"):
        return "https://pncp.gov.br/app" + u
    return u or "https://pncp.gov.br/app/editais"


def eh_credenciamento(it):
    return "credenciamento" in normalizar(f"{it.get('modalidade_licitacao_nome','')} {it.get('title','')} {it.get('description','')}")


def formatar(it):
    linhas = []
    if eh_credenciamento(it):
        linhas.append("⚠️ CREDENCIAMENTO")
    linhas.append(str(it.get("title") or it.get("numero_controle_pncp") or "Publicação PNCP"))
    orgao = it.get("orgao_nome") or ""
    local = " / ".join(x for x in (it.get("municipio_nome"), it.get("uf")) if x)
    if orgao or local:
        linhas.append(f"Órgão: {orgao}" + (f" — {local}" if local else ""))
    if it.get("modalidade_licitacao_nome"):
        linhas.append(f"Modalidade: {it['modalidade_licitacao_nome']}")
    if it.get("situacao_nome"):
        linhas.append(f"Situação: {it['situacao_nome']}")
    if it.get("data_publicacao_pncp"):
        linhas.append(f"Publicado em: {str(it['data_publicacao_pncp'])[:16].replace('T', ' ')}")
    desc = str(it.get("description") or "").strip()
    if desc:
        linhas.append("Objeto: " + (desc[:600] + "…" if len(desc) > 600 else desc))
    linhas.append(link(it))
    return "\n".join(linhas)


# ---------------- Notificações ----------------
def telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30).read()


def email(assunto, corpo):
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = EMAIL_DE
    msg["To"] = EMAIL_PARA
    msg.set_content(corpo)
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=40) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=40) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


def notificar(itens=None, aviso=None):
    """Lança exceção se nenhum canal configurado conseguir enviar (estado não é salvo e tenta de novo)."""
    canais, falhas = 0, []
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        canais += 1
        try:
            if aviso:
                telegram(aviso)
            else:
                for it in itens[:30]:
                    telegram("🔔 PNCP — nova publicação\n\n" + formatar(it))
                    time.sleep(1)
                if len(itens) > 30:
                    telegram(f"…e mais {len(itens) - 30} publicações. Veja o e-mail ou o PNCP.")
        except Exception as e:
            falhas.append(f"Telegram: {e}")
    if SMTP_HOST and EMAIL_PARA:
        canais += 1
        try:
            if aviso:
                email("Monitor PNCP", aviso)
            else:
                assunto = f"PNCP: {len(itens)} nova(s) publicação(ões) — leilão/leiloeiro"
                corpo = "\n\n" + ("\n\n" + "-" * 60 + "\n\n").join(formatar(it) for it in itens)
                email(assunto, corpo)
        except Exception as e:
            falhas.append(f"E-mail: {e}")
    if canais == 0:
        print(aviso or "\n\n".join(formatar(it) for it in itens))
        return
    for f in falhas:
        print("Falha ao notificar —", f, file=sys.stderr)
    if len(falhas) == canais:
        raise RuntimeError("Nenhum canal de notificação funcionou.")


# ---------------- Estado ----------------
def carregar_estado():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def salvar_estado(lista):
    STATE_FILE.write_text(json.dumps(lista[-MAX_ESTADO:], ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------- Execução ----------------
def coletar():
    encontrados, erros = {}, []
    for termo in TERMOS:
        for tipo in TIPOS:
            for pagina in range(1, PAGINAS + 1):
                try:
                    itens = buscar(termo, tipo, pagina)
                except Exception as e:
                    erros.append(f"{termo}/{tipo}/p{pagina}: {e}")
                    break
                for it in itens:
                    k = chave(it)
                    if k and k not in encontrados and relevante(it):
                        encontrados[k] = it
                if len(itens) < TAM_PAGINA:
                    break
                time.sleep(0.5)
    return encontrados, erros


def main():
    estado = carregar_estado()
    primeira = estado is None
    vistos_lista = list(estado or [])
    vistos = set(vistos_lista)

    encontrados, erros = coletar()
    for e in erros:
        print("Erro na consulta —", e, file=sys.stderr)
    if not encontrados and erros:
        sys.exit(1)  # PNCP fora do ar: falha visível no GitHub, estado intacto

    novos = [it for k, it in encontrados.items() if k not in vistos]
    novos.sort(key=lambda it: str(it.get("data_publicacao_pncp") or ""))

    if primeira:
        notificar(aviso=(f"✅ Monitor PNCP ativado (termos: {', '.join(TERMOS)}). "
                         f"{len(encontrados)} publicações já existentes foram registradas; "
                         "a partir de agora você receberá apenas as novas."))
    elif novos:
        notificar(itens=novos)

    for it in novos if not primeira else encontrados.values():
        vistos_lista.append(chave(it))
    salvar_estado(vistos_lista)
    print(f"OK — {len(encontrados)} encontradas, {0 if primeira else len(novos)} novas notificadas.")


if __name__ == "__main__":
    main()
