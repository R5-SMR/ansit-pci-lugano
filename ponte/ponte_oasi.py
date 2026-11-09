#!/usr/bin/env python3
"""
Ponte OASI – gira su un server in Svizzera (ansit-srv1) e scarica ogni 10 minuti
i file CSV delle stazioni OASI configurate nel repository, pubblicandoli come
file statici in /var/www/html/oasi/. Il collettore su GitHub Actions li legge da lì,
perché OASI non accetta connessioni dai server di GitHub (esteri).

Non richiede credenziali: legge la configurazione pubblica del repository e
scrive solo in locale. Stato dell'ultimo giro in /var/www/html/oasi/stato.json.
"""
import json, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

REPO_RAW = "https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/"
CONFIGS = ("config/config.json", "config/cantone.json")
DEST = Path("/var/www/html/oasi")
UA = {"User-Agent": "ANSIT-ponte-OASI/1.0 (Consorzio PCi Regione Lugano Citta; info@pcilugano.ch)"}
TZ_OASI = timezone(timedelta(hours=1))


def log(m):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {m}", flush=True)


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    stazioni, url_tpl = [], None
    for c in CONFIGS:
        try:
            cfg = requests.get(REPO_RAW + c, headers=UA, timeout=20).json()
        except Exception as e:
            log(f"configurazione {c} non leggibile: {e}")
            continue
        o = cfg.get("oasi", {})
        url_tpl = url_tpl or o.get("url")
        stazioni += o.get("stazioni", [])
    # deduplica per (codice, parametro)
    viste, lista = set(), []
    for s in stazioni:
        k = (s["codice"], s["param"])
        if k not in viste:
            viste.add(k); lista.append(s)
    if not lista or not url_tpl:
        log("nessuna stazione configurata"); sys.exit(1)

    oggi = datetime.now(TZ_OASI).date()
    ok, ko = 0, []
    for s in lista:
        url = url_tpl.format(domain=s["dominio"], param=s["param"], codice=quote(s["codice"]),
                             da=(oggi - timedelta(days=1)).isoformat(), a=oggi.isoformat())
        nome = f"{s['codice'].replace(' ', '_')}_{s['param']}.csv"
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            if "data;" not in r.text[:5000] and "# " not in r.text[:200]:
                raise ValueError("risposta non riconosciuta come CSV OASI")
            tmp = DEST / (nome + ".tmp")
            tmp.write_text(r.text, encoding="utf-8")
            tmp.replace(DEST / nome)
            ok += 1
        except Exception as e:
            ko.append({"codice": s["codice"], "param": s["param"], "errore": str(e)[:160]})
            log(f"{s['codice']} {s['param']}: {str(e)[:160]}")
        time.sleep(0.3)   # non martellare OASI
    stato = {"aggiornato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "aggiornato_locale": datetime.now().strftime("%d.%m.%Y %H:%M"),
             "stazioni_ok": ok, "stazioni_ko": ko, "totale": len(lista)}
    (DEST / "stato.json").write_text(json.dumps(stato, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"concluso: {ok}/{len(lista)} stazioni")


if __name__ == "__main__":
    main()
