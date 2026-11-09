#!/usr/bin/env python3
"""
Messaggio in evidenza dello stato maggiore – piccolo servizio web sul server ansit-srv1.
Mostra un modulo (protetto da nginx con password condivisa) e scrive /var/www/html/messaggio.json,
che le pagine del cockpit leggono e mostrano in evidenza. Nessuna dipendenza oltre Python 3.
"""
import json, html
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

FILE = "/var/www/html/messaggio.json"

PAGINA = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANSIT – Messaggio in evidenza</title>
<style>body{font:16px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;margin:2em auto;padding:0 1em;color:#1b1f1d}
h1{font-size:1.3em}label{display:block;margin:.8em 0 .3em;font-weight:600}textarea,input{width:100%;font:inherit;padding:.5em;border:1px solid #bbb;border-radius:6px}
textarea{height:7em}button{font:inherit;padding:.6em 1.2em;border-radius:6px;border:0;background:#1b1f1d;color:#fff;margin-top:1em;cursor:pointer}
button.sec{background:#eee;color:#1b1f1d;margin-left:.5em}.att{background:#fff3c4;border-left:5px solid #f08a1f;padding:.6em 1em;margin:1em 0}
small{color:#5c645f}</style></head><body>
<h1>Analisi della situazione – messaggio in evidenza</h1>
<p><small>Compare in cima alle pagine «Regione Lugano Città» e «Ticino» entro un minuto. Lasciare vuoto e salvare per rimuoverlo.</small></p>
%STATO%
<form method="post">
<label for="testo">Messaggio</label>
<textarea id="testo" name="testo" maxlength="600" placeholder="Es.: CP 51 in esercizio, posto comando a Cadro, prossimo rapporto 16:00.">%TESTO%</textarea>
<label for="autore">Firma (funzione o nome)</label>
<input id="autore" name="autore" maxlength="60" value="%AUTORE%" placeholder="Es.: C SM">
<label for="livello">Rilevanza</label>
<select id="livello" name="livello" style="font:inherit;padding:.5em;border:1px solid #bbb;border-radius:6px;width:100%%">
<option value="info" %S_INFO%>Informazione</option><option value="attenzione" %S_ATT%>Attenzione</option><option value="urgente" %S_URG%>Urgente</option></select>
<button type="submit">Salva</button><button type="submit" name="cancella" value="1" class="sec">Rimuovi il messaggio</button>
</form></body></html>"""


def leggi():
    try:
        return json.load(open(FILE, encoding="utf-8"))
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    def _rispondi(self, codice, corpo, tipo="text/html; charset=utf-8"):
        b = corpo.encode("utf-8")
        self.send_response(codice); self.send_header("Content-Type", tipo); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        m = leggi()
        stato = (f'<div class="att">Messaggio attuale ({html.escape(m.get("aggiornato_locale",""))}, {html.escape(m.get("autore",""))}): '
                 f'{html.escape(m.get("testo",""))}</div>') if m.get("testo") else '<p><small>Nessun messaggio attivo.</small></p>'
        liv = m.get("livello", "info")
        pag = (PAGINA.replace("%STATO%", stato).replace("%TESTO%", html.escape(m.get("testo", ""))).replace("%AUTORE%", html.escape(m.get("autore", "")))
               .replace("%S_INFO%", "selected" if liv == "info" else "").replace("%S_ATT%", "selected" if liv == "attenzione" else "")
               .replace("%S_URG%", "selected" if liv == "urgente" else ""))
        self._rispondi(200, pag)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        dati = parse_qs(self.rfile.read(n).decode("utf-8"))
        testo = dati.get("testo", [""])[0].strip()[:600]
        if dati.get("cancella"):
            testo = ""
        m = {"testo": testo, "autore": dati.get("autore", [""])[0].strip()[:60],
             "livello": dati.get("livello", ["info"])[0] if dati.get("livello", ["info"])[0] in ("info", "attenzione", "urgente") else "info",
             "aggiornato": datetime.now().astimezone().isoformat(timespec="seconds"),
             "aggiornato_locale": datetime.now().strftime("%d.%m.%Y %H:%M")}
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        import os; os.replace(tmp, FILE)
        self.send_response(303); self.send_header("Location", self.path); self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8081), H).serve_forever()
