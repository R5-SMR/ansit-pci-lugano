#!/usr/bin/env python3
"""
Cantieri di lavoro – piccolo servizio web sul server ansit-srv1.
I militi (utenza condivisa, protetta da nginx) inseriscono i cantieri toccando la carta e indicano fino a quando valgono.
Scrive /var/www/html/cantieri.json, che la pagina «Regione Lugano Città» del cockpit legge e mostra sulla carta.
Un cantiere scaduto non viene più pubblicato. Nessuna dipendenza oltre Python 3.
"""
import json, html, os, secrets
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

FILE = os.environ.get("ANSIT_CANTIERI_FILE", "/var/www/html/cantieri.json")
PORTA = int(os.environ.get("ANSIT_CANTIERI_PORTA", "8082"))
TZ = ZoneInfo("Europe/Zurich")
MASSIMO = 80                      # cantieri attivi al massimo
DURATA_MASSIMA = 60               # giorni
LIMITI = (45.7, 46.7, 8.3, 9.4)   # lat min, lat max, lon min, lon max: Ticino e dintorni

PAGINA = """<!DOCTYPE html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANSIT – Cantieri</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css" crossorigin="">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js" crossorigin=""></script>
<style>
body{font:16px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:0 auto;padding:12px;color:#1b1f1d;background:#f3f4f2}
h1{font-size:1.25em;margin:.2em 0}h2{font-size:1.05em;margin:1.2em 0 .4em}
label{display:block;margin:.8em 0 .25em;font-weight:600}
input,textarea,button,select{font:inherit}
input[type=text],input[type=date],textarea{width:100%;padding:.55em;border:1px solid #bbb;border-radius:6px;box-sizing:border-box;background:#fff}
button{padding:.6em 1em;border-radius:6px;border:0;background:#1b1f1d;color:#fff;cursor:pointer;min-height:44px}
button.sec{background:#e6e8e5;color:#1b1f1d}button.rosso{background:#b3261e}
#mappa{height:46vh;min-height:280px;border:1px solid #bbb;border-radius:6px;background:#fff}
.rapidi{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.rapidi button{flex:1;min-width:70px;padding:.4em .3em}
.avviso{background:#fff3c4;border-left:5px solid #f08a1f;padding:.6em 1em;margin:.8em 0;border-radius:4px}
.ok{background:#e3f1e7;border-left:5px solid #2e7d4f;padding:.6em 1em;margin:.8em 0;border-radius:4px}
.cant{background:#fff;border:1px solid #d6d9d4;border-left:6px solid #1565c0;border-radius:6px;padding:.6em .8em;margin:.5em 0}
.cant b{font-size:1.05em}.cant small{color:#5c645f;display:block}
.cant form{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:.5em}.cant input[type=date]{width:auto;flex:1;min-width:140px}
small{color:#5c645f}.pos{font-variant-numeric:tabular-nums}
.pin{background:#1565c0;color:#fff;border:2px solid #fff;border-radius:50%;width:26px;height:26px;line-height:22px;text-align:center;font-weight:700;font-size:13px;box-shadow:0 1px 4px rgba(0,0,0,.5)}
.pin.nuovo{background:#b3261e}
</style></head><body>
<h1>Quadro della situazione – cantieri</h1>
<p><small>I cantieri compaiono sulla carta della pagina «Regione Lugano Città» entro un minuto e spariscono da soli il giorno dopo la data di fine.</small></p>
%MESSAGGIO%
<h2>Nuovo cantiere</h2>
<form method="post" id="nuovo">
<input type="hidden" name="azione" value="aggiungi"><input type="hidden" name="gettone" value="%GETTONE%">
<input type="hidden" name="lat" id="lat"><input type="hidden" name="lon" id="lon">
<label>1. Posizione: toccare la carta nel punto del cantiere</label>
<div id="mappa"></div>
<div class="rapidi"><button type="button" class="sec" id="qui">Usa la mia posizione</button></div>
<small class="pos" id="pos">Nessuna posizione scelta.</small>
<label for="nome">2. Nome del cantiere</label>
<input type="text" id="nome" name="nome" maxlength="60" required placeholder="Es.: Sentiero Brè – Monte Boglia">
<label for="nota">3. Nota (compagnia, lavoro, effettivo, responsabile)</label>
<textarea id="nota" name="nota" maxlength="200" rows="2" placeholder="Es.: cp 51, sez pionieri, 12 militi, capo cantiere sgt Rossi"></textarea>
<label for="fino">4. Valido fino al (compreso)</label>
<input type="date" id="fino" name="fino" required min="%OGGI%" max="%MAX%" value="%OGGI%">
<div class="rapidi"><button type="button" class="sec" data-g="0">Solo oggi</button><button type="button" class="sec" data-g="1">Domani</button><button type="button" class="sec" data-g="4">5 giorni</button><button type="button" class="sec" data-g="9">10 giorni</button></div>
<p><button type="submit">Salva il cantiere</button></p>
</form>
<h2>Cantieri attivi (%NUMERO%)</h2>
%ELENCO%
<p><small>Aggiornato %ORA%. Utenza condivisa: ogni modifica vale per tutti.</small></p>
<script>
(function(){
  var cantieri = %DATI%;
  var m = L.map("mappa", {zoomControl: true, scrollWheelZoom: false}).setView([46.005, 8.95], 12);
  L.tileLayer("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-farbe/default/current/3857/{z}/{x}/{y}.jpeg", {attribution: "© swisstopo", maxZoom: 19, minZoom: 9}).addTo(m);
  var icona = function(c, t){ return L.divIcon({className: "", html: '<div class="pin ' + c + '">' + t + '</div>', iconSize: [26, 26], iconAnchor: [13, 13]}); };
  cantieri.forEach(function(c){ L.marker([c.lat, c.lon], {icon: icona("", "C")}).bindTooltip(String(c.nome).replace(/&/g, "&amp;").replace(/</g, "&lt;")).addTo(m); });
  if (cantieri.length) m.fitBounds(L.latLngBounds(cantieri.map(function(c){ return [c.lat, c.lon]; })).pad(.4), {maxZoom: 14});
  var nuovo = null;
  function scegli(lat, lon, centra){
    document.getElementById("lat").value = lat.toFixed(6); document.getElementById("lon").value = lon.toFixed(6);
    document.getElementById("pos").textContent = "Posizione scelta: " + lat.toFixed(5) + ", " + lon.toFixed(5) + " (toccare di nuovo la carta per correggere)";
    if (nuovo) nuovo.setLatLng([lat, lon]); else nuovo = L.marker([lat, lon], {icon: icona("nuovo", "+")}).addTo(m);
    if (centra) m.setView([lat, lon], 16);
  }
  m.on("click", function(e){ scegli(e.latlng.lat, e.latlng.lng, false); });
  document.getElementById("qui").addEventListener("click", function(){
    var p = document.getElementById("pos");
    if (!navigator.geolocation) { p.textContent = "Questo dispositivo non fornisce la posizione: toccare la carta."; return; }
    p.textContent = "Ricerca della posizione…";
    navigator.geolocation.getCurrentPosition(function(g){ scegli(g.coords.latitude, g.coords.longitude, true); },
      function(){ p.textContent = "Posizione non disponibile (permesso negato o segnale assente): toccare la carta."; }, {enableHighAccuracy: true, timeout: 15000});
  });
  document.querySelectorAll("button[data-g]").forEach(function(b){ b.addEventListener("click", function(){
    var d = new Date("%OGGI%T12:00:00"); d.setDate(d.getDate() + parseInt(b.dataset.g, 10));
    document.getElementById("fino").value = d.toISOString().slice(0, 10);
  }); });
  document.getElementById("nuovo").addEventListener("submit", function(e){
    if (!document.getElementById("lat").value) { e.preventDefault(); document.getElementById("pos").textContent = "Manca la posizione: toccare la carta nel punto del cantiere."; document.getElementById("mappa").scrollIntoView({behavior: "smooth"}); }
  });
  document.querySelectorAll("form[data-conferma]").forEach(function(f){ f.addEventListener("submit", function(e){ if (!window.confirm(f.dataset.conferma)) e.preventDefault(); }); });
})();
</script>
</body></html>"""

GETTONE = secrets.token_hex(16)   # impedisce che un'altra pagina invii il modulo all'insaputa dell'utente


def oggi():
    return datetime.now(TZ).date()


def leggi():
    """Cantieri non scaduti, dal file."""
    try:
        d = json.load(open(FILE, encoding="utf-8"))
        tutti = d.get("cantieri") or []
    except Exception:
        tutti = []
    return [c for c in tutti if isinstance(c, dict) and str(c.get("fino", "")) >= oggi().isoformat()]


def scrivi(cantieri):
    adesso = datetime.now(TZ)
    d = {"aggiornato": adesso.isoformat(timespec="seconds"), "aggiornato_locale": adesso.strftime("%d.%m.%Y %H:%M"), "cantieri": cantieri}
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, FILE)


def data_valida(testo):
    """Data di fine tra oggi e DURATA_MASSIMA giorni; None se non valida."""
    try:
        d = date.fromisoformat(testo)
    except (TypeError, ValueError):
        return None
    return d if oggi() <= d <= oggi() + timedelta(days=DURATA_MASSIMA) else None


def giorno(testo):
    try:
        d = date.fromisoformat(testo)
        return ["lun", "mar", "mer", "gio", "ven", "sab", "dom"][d.weekday()] + d.strftime(" %d.%m.")
    except (TypeError, ValueError):
        return str(testo)


def applica(dati):
    """Esegue l'azione richiesta dal modulo; restituisce (classe, messaggio)."""
    azione = dati.get("azione", [""])[0]
    cantieri = leggi()
    if azione == "aggiungi":
        nome = " ".join(dati.get("nome", [""])[0].split())[:60]
        nota = " ".join(dati.get("nota", [""])[0].split())[:200]
        fino = data_valida(dati.get("fino", [""])[0])
        try:
            lat, lon = float(dati.get("lat", [""])[0]), float(dati.get("lon", [""])[0])
        except ValueError:
            return "avviso", "Cantiere non salvato: manca la posizione sulla carta."
        if not nome:
            return "avviso", "Cantiere non salvato: manca il nome."
        if fino is None:
            return "avviso", f"Cantiere non salvato: la data di fine deve stare tra oggi e {DURATA_MASSIMA} giorni."
        if not (LIMITI[0] <= lat <= LIMITI[1] and LIMITI[2] <= lon <= LIMITI[3]):
            return "avviso", "Cantiere non salvato: la posizione è fuori dal Ticino."
        if len(cantieri) >= MASSIMO:
            return "avviso", f"Cantiere non salvato: ci sono già {MASSIMO} cantieri attivi, eliminarne qualcuno."
        cantieri.append({"id": secrets.token_hex(4), "nome": nome, "nota": nota, "lat": round(lat, 6), "lon": round(lon, 6),
                         "dal": oggi().isoformat(), "fino": fino.isoformat(), "inserito": datetime.now(TZ).isoformat(timespec="seconds")})
        scrivi(cantieri)
        return "ok", f"Cantiere «{nome}» salvato, valido fino a {giorno(fino.isoformat())}"
    ident = dati.get("id", [""])[0]
    trovato = next((c for c in cantieri if c.get("id") == ident), None)
    if trovato is None:
        return "avviso", "Cantiere non trovato: forse è già stato eliminato da un altro utente."
    if azione == "elimina":
        scrivi([c for c in cantieri if c.get("id") != ident])
        return "ok", f"Cantiere «{trovato['nome']}» eliminato."
    if azione == "proroga":
        fino = data_valida(dati.get("fino", [""])[0])
        if fino is None:
            return "avviso", f"Data non valida: deve stare tra oggi e {DURATA_MASSIMA} giorni."
        trovato["fino"] = fino.isoformat()
        scrivi(cantieri)
        return "ok", f"Cantiere «{trovato['nome']}» ora valido fino a {giorno(fino.isoformat())}"
    return "avviso", "Richiesta non riconosciuta."


def pagina(messaggio=""):
    cantieri = sorted(leggi(), key=lambda c: (c.get("fino", ""), c.get("nome", "")))
    e = html.escape
    massimo = (oggi() + timedelta(days=DURATA_MASSIMA)).isoformat()
    righe = []
    for c in cantieri:
        righe.append(
            f'<div class="cant"><b>{e(c["nome"])}</b><small>{e(c.get("nota", ""))}</small>'
            f'<small>dal {e(giorno(c.get("dal", "")))} · valido fino a <b>{e(giorno(c.get("fino", "")))}</b></small>'
            f'<form method="post"><input type="hidden" name="gettone" value="{GETTONE}"><input type="hidden" name="azione" value="proroga">'
            f'<input type="hidden" name="id" value="{e(c["id"])}"><input type="date" name="fino" value="{e(c.get("fino", ""))}" min="{oggi().isoformat()}" max="{massimo}" aria-label="Nuova data di fine">'
            f'<button type="submit" class="sec">Cambia data</button></form>'
            f'<form method="post" data-conferma="Eliminare il cantiere «{e(c["nome"])}»?"><input type="hidden" name="gettone" value="{GETTONE}"><input type="hidden" name="azione" value="elimina">'
            f'<input type="hidden" name="id" value="{e(c["id"])}"><button type="submit" class="rosso">Elimina</button></form></div>')
    pubblici = [{"nome": c["nome"], "lat": c["lat"], "lon": c["lon"]} for c in cantieri]
    return (PAGINA.replace("%MESSAGGIO%", messaggio).replace("%GETTONE%", GETTONE).replace("%OGGI%", oggi().isoformat()).replace("%MAX%", massimo)
            .replace("%NUMERO%", str(len(cantieri))).replace("%ELENCO%", "".join(righe) or "<p><small>Nessun cantiere attivo.</small></p>")
            .replace("%ORA%", datetime.now(TZ).strftime("%d.%m.%Y %H:%M"))
            .replace("%DATI%", json.dumps(pubblici, ensure_ascii=False).replace("</", "<\\/")))


class H(BaseHTTPRequestHandler):
    def _rispondi(self, codice, corpo):
        b = corpo.encode("utf-8")
        self.send_response(codice); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._rispondi(200, pagina())

    def do_POST(self):
        n = min(int(self.headers.get("Content-Length", 0) or 0), 20000)
        dati = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if dati.get("gettone", [""])[0] != GETTONE:
            # pagina rimasta aperta da prima di un riavvio del servizio, oppure invio da un'altra origine
            self._rispondi(200, pagina('<div class="avviso">La pagina era scaduta: è stata ricaricata, ripetere l\'operazione.</div>'))
            return
        try:
            classe, testo = applica(dati)
        except Exception as ex:      # mai una pagina d'errore vuota a chi sta sul cantiere
            classe, testo = "avviso", "Operazione non riuscita: " + str(ex)[:120]
        self._rispondi(200, pagina(f'<div class="{classe}">{html.escape(testo)}</div>'))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not os.path.exists(FILE):
        try:
            scrivi([])
        except OSError:
            pass
    HTTPServer(("127.0.0.1", PORTA), H).serve_forever()
