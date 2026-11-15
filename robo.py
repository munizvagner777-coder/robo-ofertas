"""
Robô de Ofertas — posta ofertas com link de afiliado num canal do Telegram.

Fontes de ofertas (nesta ordem):
  1. fila.txt  -> links que VOCÊ coloca (ex.: Mercado Livre). Posta primeiro.
  2. Shopee    -> busca automática pela API oficial de afiliados (precisa App ID + Secret).

Roda a cada ~15 min pelo GitHub Actions. Cada rodada posta no máximo 1 oferta,
respeitando o intervalo mínimo, o limite por dia e o horário de silêncio.
"""

import hashlib
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# =====================================================================
# CONFIGURAÇÕES — pode mudar à vontade
# =====================================================================

# Palavras que o robô usa para buscar ofertas na Shopee (vai rodando uma por vez).
PALAVRAS_CHAVE = [
    "air fryer",
    "fone bluetooth",
    "chapinha",
    "organizador de cozinha",
    "smartwatch",
    "secador de cabelo",
    "garrafa térmica",
    "caixa de som bluetooth",
    "kit maquiagem",
    "mop giratório",
    "carregador turbo",
    "tênis feminino",
    "panela antiaderente",
    "luminária led",
    "perfume",
    "ferramentas kit",
    "brinquedo infantil",
    "mochila",
    "relógio masculino",
    "jogo de cama",
]

DESCONTO_MINIMO = 20        # % — só posta se tiver pelo menos esse desconto
NOTA_MINIMA = 4.5           # estrelas
VENDAS_MINIMAS = 50         # quantidade já vendida
POSTS_POR_DIA = 20          # limite de posts por dia
INTERVALO_MINUTOS = 40      # tempo mínimo entre um post e outro
HORA_INICIO = 7             # começa a postar às 7h
HORA_FIM = 23               # para de postar às 23h

# =====================================================================
# Segredos (ficam guardados no GitHub, nunca no código)
# =====================================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CANAL = os.environ.get("TELEGRAM_CANAL", "")   # ex.: @meucanaldeofertas
SHOPEE_APP_ID = os.environ.get("SHOPEE_APP_ID", "")
SHOPEE_SECRET = os.environ.get("SHOPEE_SECRET", "")

SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"
ARQ_ESTADO = "postados.json"
ARQ_FILA = "fila.txt"
FUSO = timezone(timedelta(hours=-3))  # horário de Brasília


# ---------------------------------------------------------------------
# Estado (o que já foi postado, contador do dia, etc.)
# ---------------------------------------------------------------------
def carregar_estado():
    try:
        with open(ARQ_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"postados": [], "dia": "", "posts_hoje": 0,
                "ultimo_post": 0, "indice_palavra": 0}


def salvar_estado(estado):
    estado["postados"] = estado["postados"][-3000:]  # guarda só os mais recentes
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------
def reais(valor):
    s = f"{float(valor):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def vendidos(n):
    n = int(n or 0)
    if n >= 1000:
        return f"{n / 1000:.1f}".replace(".", ",").replace(",0", "") + " mil vendidos"
    return f"{n} vendidos"


def montar_texto(o):
    nome = html.escape(o["nome"][:120])
    linhas = [f"🔥 <b>{nome}</b>", ""]
    if o.get("preco_antigo") and o.get("desconto"):
        linhas.append(f"De <s>{reais(o['preco_antigo'])}</s> por <b>{reais(o['preco'])}</b>  (-{o['desconto']}%)")
    elif o.get("preco"):
        linhas.append(f"💰 Por <b>{reais(o['preco'])}</b>")
    extras = []
    if o.get("nota"):
        extras.append(f"⭐ {float(o['nota']):.1f}".replace(".", ","))
    if o.get("vendas"):
        extras.append(vendidos(o["vendas"]))
    if extras:
        linhas.append(" | ".join(extras))
    linhas += ["", f"🛒 <a href=\"{html.escape(o['link'])}\">COMPRAR AQUI</a>",
               "", "<i>⚠️ Preço pode mudar a qualquer momento.</i>"]
    return "\n".join(linhas)


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
def postar_telegram(o):
    texto = montar_texto(o)
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if o.get("imagem"):
        r = requests.post(f"{base}/sendPhoto", data={
            "chat_id": TELEGRAM_CANAL, "photo": o["imagem"],
            "caption": texto, "parse_mode": "HTML"}, timeout=30)
        if r.ok:
            return True
        print("Falhou com foto, tentando só texto:", r.text)
    r = requests.post(f"{base}/sendMessage", data={
        "chat_id": TELEGRAM_CANAL, "text": texto, "parse_mode": "HTML"}, timeout=30)
    if not r.ok:
        print("Erro no Telegram:", r.text)
    return r.ok


# ---------------------------------------------------------------------
# Fonte 1: fila.txt (links que você coloca — ex.: Mercado Livre)
# Formato de cada linha:  título | preço | link de afiliado | link da imagem (opcional)
# ---------------------------------------------------------------------
def pegar_da_fila():
    try:
        with open(ARQ_FILA, encoding="utf-8") as f:
            linhas = f.read().splitlines()
    except FileNotFoundError:
        return None, None
    for i, linha in enumerate(linhas):
        if not linha.strip() or linha.strip().startswith("#"):
            continue
        partes = [p.strip() for p in linha.split("|")]
        if len(partes) < 3:
            print("Linha da fila com formato errado, pulando:", linha)
            continue
        preco = partes[1].replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            preco = float(preco)
        except ValueError:
            preco = None
        oferta = {"id": "fila:" + partes[2], "nome": partes[0], "preco": preco,
                  "link": partes[2], "imagem": partes[3] if len(partes) > 3 else ""}
        return oferta, i
    return None, None


def remover_da_fila(indice):
    with open(ARQ_FILA, encoding="utf-8") as f:
        linhas = f.read().splitlines()
    del linhas[indice]
    with open(ARQ_FILA, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


# ---------------------------------------------------------------------
# Fonte 2: Shopee (API oficial de afiliados)
# ---------------------------------------------------------------------
CONSULTA = """
query ($keyword: String, $page: Int, $limit: Int) {
  productOfferV2(keyword: $keyword, sortType: 2, page: $page, limit: $limit) {
    nodes {
      itemId productName price priceDiscountRate
      ratingStar sales imageUrl offerLink commissionRate
    }
  }
}
"""


def buscar_shopee(palavra):
    corpo = json.dumps({"query": CONSULTA,
                        "variables": {"keyword": palavra, "page": 1, "limit": 50}},
                       separators=(",", ":"))
    ts = str(int(time.time()))
    assinatura = hashlib.sha256((SHOPEE_APP_ID + ts + corpo + SHOPEE_SECRET).encode()).hexdigest()
    headers = {"Content-Type": "application/json",
               "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={ts}, Signature={assinatura}"}
    r = requests.post(SHOPEE_URL, data=corpo, headers=headers, timeout=30)
    dados = r.json()
    if dados.get("errors"):
        print("Erro da Shopee:", dados["errors"])
        return []
    return (dados.get("data") or {}).get("productOfferV2", {}).get("nodes", []) or []


def pegar_da_shopee(estado):
    ja_postados = set(estado["postados"])
    for _ in range(len(PALAVRAS_CHAVE)):
        palavra = PALAVRAS_CHAVE[estado["indice_palavra"] % len(PALAVRAS_CHAVE)]
        estado["indice_palavra"] += 1
        print(f"Buscando na Shopee: {palavra}")
        for p in buscar_shopee(palavra):
            pid = f"shopee:{p.get('itemId')}"
            desconto = int(p.get("priceDiscountRate") or 0)
            nota = float(p.get("ratingStar") or 0)
            vendas = int(p.get("sales") or 0)
            if pid in ja_postados or not p.get("offerLink"):
                continue
            if desconto < DESCONTO_MINIMO or nota < NOTA_MINIMA or vendas < VENDAS_MINIMAS:
                continue
            preco = float(p.get("price") or 0)
            antigo = preco / (1 - desconto / 100) if 0 < desconto < 100 else None
            return {"id": pid, "nome": p.get("productName", ""), "preco": preco,
                    "preco_antigo": antigo, "desconto": desconto, "nota": nota,
                    "vendas": vendas, "imagem": p.get("imageUrl", ""),
                    "link": p["offerLink"]}
    return None


# ---------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CANAL:
        sys.exit("Falta configurar TELEGRAM_TOKEN e TELEGRAM_CANAL nos Secrets do GitHub.")

    agora = datetime.now(FUSO)
    estado = carregar_estado()

    hoje = agora.strftime("%Y-%m-%d")
    if estado.get("dia") != hoje:
        estado["dia"], estado["posts_hoje"] = hoje, 0

    forcar = os.environ.get("FORCAR") == "1"  # rodada manual de teste ignora as regras
    if not forcar:
        if not (HORA_INICIO <= agora.hour < HORA_FIM):
            print("Fora do horário de postagem."); return
        if estado["posts_hoje"] >= POSTS_POR_DIA:
            print("Limite do dia atingido."); return
        if time.time() - estado.get("ultimo_post", 0) < INTERVALO_MINUTOS * 60:
            print("Ainda não deu o intervalo mínimo."); return

    oferta, indice_fila = pegar_da_fila()
    if oferta is None and SHOPEE_APP_ID and SHOPEE_SECRET:
        oferta = pegar_da_shopee(estado)

    if oferta is None:
        print("Nenhuma oferta nova encontrada nesta rodada.")
        salvar_estado(estado); return

    if postar_telegram(oferta):
        print("Postado:", oferta["nome"])
        estado["postados"].append(oferta["id"])
        estado["posts_hoje"] += 1
        estado["ultimo_post"] = time.time()
        if indice_fila is not None:
            remover_da_fila(indice_fila)
    salvar_estado(estado)


if __name__ == "__main__":
    main()
