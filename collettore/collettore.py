#!/usr/bin/env python3
"""
Collettore del quadro di situazione – Consorzio PCi Regione Lugano Città.

Per ogni ciclo:
  1. interroga le fonti (MeteoSvizzera, Alertswiss, UFAM, Open-Meteo);
  2. salva le risposte grezze in archivio/AAAA/MM/GG/HHMM.json.gz;
  3. produce sito/dati.json (normalizzato) per il cockpit;
  4. confronta con il ciclo precedente e scrive la lista delle variazioni;
  5. riconosce le variazioni rapide (vento, stazioni UFAM) e tiene la cronologia delle ultime ore.

Regola di base: una fonte che non risponde produce "disponibile": false,
mai un valore di default rassicurante.
"""
import csv, gzip, io, json, os, re, sys, xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent.parent
CFG = json.loads((BASE / "config" / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG["fuso_orario"])
ADESSO = datetime.now(timezone.utc)
HEADERS = {"User-Agent": "Lagebild-PCi-LuganoCitta/0.1 (prototipo; contatto: info@pcilugano.ch)"}
GREZZI = {}   # risposte grezze da archiviare


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def scarica(url, tipo="text", accept_lang=None, timeout=25):
    h = dict(HEADERS)
    if accept_lang:
        h["Accept-Language"] = accept_lang
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json() if tipo == "json" else r.text


def non_disp(motivo):
    return {"disponibile": False, "motivo": str(motivo)[:200]}


# ---------------------------------------------------------------- MeteoSvizzera: misure SMN
PARAM = {  # identificatori OGD, granularità 10 minuti
    "temperatura": "tre200s0", "umidita": "ure200s0", "pioggia_10min": "rre150z0",
    "vento_kmh": "fkl010z0", "raffica_kmh": "fkl010z1", "direzione": "dkl010z0",
    "sole_min": "sre000z0", "pressione": "prestas0",
}


def misure_smn(st):
    url = CFG["meteosvizzera_smn_url"].format(id=st["id"].lower())
    try:
        testo = scarica(url)
    except Exception as e:
        return non_disp(e)
    GREZZI[f"smn_{st['id']}"] = testo[-20000:]
    righe = list(csv.DictReader(io.StringIO(testo), delimiter=";"))
    if not righe:
        return non_disp("file vuoto")
    ultima = righe[-1]
    col_ts = next((c for c in ultima if "timestamp" in c.lower()), None)

    def num(r, k):
        v = r.get(PARAM[k], "")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # pioggia ultime 24 h = somma dei valori a 10 min delle ultime 144 righe
    pioggia24 = sum(x for x in (num(r, "pioggia_10min") for r in righe[-144:]) if x is not None)
    pioggia1h = sum(x for x in (num(r, "pioggia_10min") for r in righe[-6:]) if x is not None)
    vento = num(ultima, "vento_kmh")
    raffica = num(ultima, "raffica_kmh")
    ultime = righe[-144:]
    serie_t = [num(r, "temperatura") for r in ultime]
    serie_raf = [num(r, "raffica_kmh") for r in ultime]
    pioggia_oraria = []
    for i in range(0, len(ultime), 6):
        blocco = [num(r, "pioggia_10min") for r in ultime[i:i + 6]]
        pioggia_oraria.append(round(sum(x for x in blocco if x is not None), 1))
    eta = None
    try:   # l'ora del file OGD è in UTC
        t_ril = datetime.strptime(str(ultima.get(col_ts)).strip(), "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        eta = int((ADESSO - t_ril).total_seconds() // 60)
    except (TypeError, ValueError):
        pass
    return {
        "disponibile": True, "stazione": st["id"], "nome": st["nome"],
        "rilevamento": ultima.get(col_ts), "eta_minuti": eta, "temperatura": num(ultima, "temperatura"),
        "umidita": num(ultima, "umidita"), "pioggia_10min": num(ultima, "pioggia_10min"),
        "pioggia_1h": round(pioggia1h, 1), "pioggia_24h": round(pioggia24, 1),
        # OGD fornisce il vento in km/h (da verificare nei metadati: fkl010z0 è in km/h)
        "vento_kmh": vento, "raffica_kmh": raffica, "direzione": num(ultima, "direzione"),
        "pressione": num(ultima, "pressione"),
        "serie_24h": {"temperatura": serie_t, "raffica_kmh": serie_raf, "pioggia_oraria": pioggia_oraria},
    }


# ---------------------------------------------------------------- MeteoSvizzera: allerte per Comune
def allerte_comune(c):
    cfg = CFG["allerte_meteosvizzera"]
    url = cfg["url"].format(plz=c["plz"])
    try:
        js = scarica(url, "json", accept_lang=cfg["lingua"])
    except Exception as e:
        return non_disp(e)
    GREZZI.setdefault("allerte", {})[c["nome"]] = js
    lista = js.get("warnings") or js.get("warningsOverview") or []
    out = []
    for w in lista:
        tipo = str(w.get("warnType", w.get("type", "?")))
        liv = w.get("warnLevel", w.get("level"))
        out.append({
            "tipo": cfg["tipi"].get(tipo, f"tipo {tipo}"), "livello": liv,
            "dal": _epoch(w.get("validFrom")), "al": _epoch(w.get("validTo")),
            "testo": (w.get("text") or w.get("description") or "")[:600],
            "preallerta": bool(w.get("outlook", False)),
        })
    livello_max = max([w["livello"] for w in out if isinstance(w["livello"], (int, float))], default=1)
    return {"disponibile": True, "livello_max": livello_max, "allerte": out}


def _epoch(v):
    if v is None:
        return None
    try:
        v = float(v)
        if v > 1e11:
            v /= 1000
        return datetime.fromtimestamp(v, TZ).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------- Alertswiss
CHIAVI_TITOLO = ("title", "titolo", "headline", "titel")
CHIAVI_TESTO = ("description", "text", "summary", "instruction", "message", "beschreibung", "testo", "content")
CHIAVI_LINK = ("link", "url", "detailsUrl", "detailUrl", "href")
CHIAVI_DATA = ("sent", "date", "published", "publishedAt", "created", "timestamp", "datum", "data")
CHIAVI_AREA = ("area", "areas", "region", "regions", "areaDesc", "location", "gebiet")
CHIAVI_LIVELLO = ("severity", "level", "type", "alertType", "category", "kind")


def _primo(d, chiavi):
    for k in chiavi:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _testo_piatto(v, max_len=800):
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    return re.sub(r"<[^>]+>", " ", str(v)).strip()[:max_len]


def _trova_notifiche(nodo, out):
    """Raccoglie ricorsivamente gli oggetti che hanno un titolo: sono le notifiche."""
    if isinstance(nodo, dict):
        if _primo(nodo, CHIAVI_TITOLO) is not None and any(k in nodo for k in CHIAVI_TESTO + CHIAVI_AREA + CHIAVI_LIVELLO):
            out.append(nodo)
        for v in nodo.values():
            _trova_notifiche(v, out)
    elif isinstance(nodo, list):
        for v in nodo:
            _trova_notifiche(v, out)


def alertswiss():
    cfg = CFG["alertswiss"]
    js, usato = None, None
    for url in cfg["url_candidati"]:
        try:
            js = scarica(url, "json")
            usato = url
            break
        except Exception as e:
            log(f"Alertswiss: {url} non risponde ({e})")
    if js is None:
        return non_disp("nessun URL candidato risponde")
    GREZZI["alertswiss"] = js
    grezze = []
    _trova_notifiche(js, grezze)
    voci = []
    for n in grezze:
        voci.append({
            "titolo": _testo_piatto(_primo(n, CHIAVI_TITOLO), 200),
            "testo": _testo_piatto(_primo(n, CHIAVI_TESTO) or ""),
            "area": _testo_piatto(_primo(n, CHIAVI_AREA) or "", 300),
            "livello": _testo_piatto(_primo(n, CHIAVI_LIVELLO) or "", 60),
            "link": _primo(n, CHIAVI_LINK) if isinstance(_primo(n, CHIAVI_LINK), str) else None,
            "data": _epoch(_primo(n, CHIAVI_DATA)) if isinstance(_primo(n, CHIAVI_DATA), (int, float)) else _testo_piatto(_primo(n, CHIAVI_DATA) or "", 40),
        })
    kw = [k.lower() for k in cfg["parole_chiave"]]
    pertinenti = [v for v in voci if any(k in (v["titolo"] + v["testo"] + v["area"]).lower() for k in kw)]
    return {"disponibile": True, "fonte": usato, "totale_svizzera": len(voci), "notifiche": pertinenti}


# ---------------------------------------------------------------- SLF: bollettino valanghe
GRADI_VALANGHE = {"low": 1, "moderate": 2, "considerable": 3, "high": 4, "very_high": 5}
PROBLEMI_VALANGHE = {"new_snow": "neve fresca", "wind_slab": "neve ventata", "persistent_weak_layers": "strati deboli persistenti",
                     "wet_snow": "neve bagnata", "gliding_snow": "slittamenti", "cornices": "cornici"}


def valanghe():
    """Bollettino valanghe SLF (CAAML v6, JSON): grado massimo per le regioni di allerta indicate in config.json.
    Distingue tre casi: fonte non raggiungibile (non disponibile), nessun bollettino in vigore, bollettino in vigore.
    Un bollettino scaduto non viene mai mostrato come valido."""
    cfg = CFG.get("valanghe") or {}
    if not cfg.get("url_candidati"):
        return None
    js, usato = None, None
    for url in cfg["url_candidati"]:
        try:
            js = scarica(url, "json", timeout=15)
            usato = url
            break
        except Exception as e:
            log(f"Valanghe: {url} non risponde ({str(e)[:100]})")
    if js is None:
        return non_disp("nessun URL del bollettino SLF risponde")
    GREZZI["valanghe"] = js
    if isinstance(js, dict) and isinstance(js.get("bulletins"), list):
        bollettini = js["bulletins"]
    elif isinstance(js, dict) and isinstance(js.get("features"), list):      # variante GeoJSON
        bollettini = [f.get("properties") or {} for f in js["features"]]
    elif isinstance(js, list):
        bollettini = js
    else:
        return non_disp("formato del bollettino non riconosciuto")
    prefissi = tuple(cfg.get("prefissi_regione") or ["CH-6"])
    regioni, tutte, validita = {}, set(), []
    for b in bollettini:
        if not isinstance(b, dict):
            continue
        vt = b.get("validTime") or {}
        inizio, fine = _tempo(vt.get("startTime")), _tempo(vt.get("endTime"))
        reg = [r for r in (b.get("regions") or []) if isinstance(r, dict) and r.get("regionID")]
        if not reg and b.get("regionID"):
            reg = [{"regionID": b["regionID"], "name": b.get("regionName") or b.get("name")}]
        tutte.update(r["regionID"] for r in reg)
        miei = [r for r in reg if str(r["regionID"]).startswith(prefissi)]
        if not miei:
            continue
        if fine is not None and fine < ADESSO:
            continue                                  # scaduto
        if inizio is not None and inizio > ADESSO + timedelta(hours=12):
            continue                                  # non ancora in vigore
        migliore = None
        for d in b.get("dangerRatings") or []:
            g = GRADI_VALANGHE.get(str((d or {}).get("mainValue")))
            if g is None:
                continue
            sudd = (((d.get("customData") or {}).get("CH") or {}).get("subdivision"))
            cand = {"grado": g, "suddivisione": {"minus": "−", "neutral": "=", "plus": "+"}.get(sudd, ""),
                    "quota_da": (d.get("elevation") or {}).get("lowerBound"), "periodo": d.get("validTimePeriod")}
            if migliore is None or g > migliore["grado"]:
                migliore = cand
        if migliore is None:
            continue
        problemi = []
        for pr in b.get("avalancheProblems") or []:
            nome = PROBLEMI_VALANGHE.get(str((pr or {}).get("problemType")))
            if nome and nome not in problemi:
                problemi.append(nome)
        validita.append((inizio, fine))
        for r in miei:
            prec = regioni.get(r["regionID"])
            if prec is None or migliore["grado"] > prec["grado"]:
                regioni[r["regionID"]] = {"id": r["regionID"], "nome": r.get("name") or r["regionID"], **migliore, "problemi": problemi}
    base = {"disponibile": True, "fonte": usato, "regioni_svizzera": len(tutte)}
    if not regioni:
        esempio = ", ".join(sorted(tutte)[:6])
        return {**base, "in_vigore": False,
                "nota": "Nessun bollettino valanghe in vigore per il Ticino" + (f" (in Svizzera: {len(tutte)} regioni)" if tutte else "")
                , "_regioni_viste": esempio}
    elenco = sorted(regioni.values(), key=lambda r: (-r["grado"], r["nome"]))
    fini = [f for _, f in validita if f is not None]
    return {**base, "in_vigore": True, "grado_max": elenco[0]["grado"], "regioni": elenco,
            "valido_fino": min(fini).astimezone(TZ).strftime("%d.%m. %H:%M") if fini else None,
            "valido_fino_iso": min(fini).isoformat(timespec="minutes") if fini else None}


# ---------------------------------------------------------------- UFAM: pegel
def pegel(st):
    valore, serie, punti, usato = None, [], [], None
    for tpl in CFG["ufam_url_candidati"]:
        url = tpl.format(id=st["id"])
        try:
            js = scarica(url, "json")
        except Exception as e:
            log(f"UFAM {st['id']}: {url} non risponde ({e})")
            continue
        GREZZI.setdefault("ufam", {})[st["id"]] = js
        valore, serie, punti = _estrai_ufam(js, st["param"])
        if valore is not None:
            usato = url
            break
    if valore is None:
        return {**non_disp("valore non trovato nelle risposte"), "id": st["id"],
                "nome": st["nome"], "luogo": st["luogo"]}
    livello = None
    if st.get("soglie"):
        livello = 1
        for liv, soglia in sorted(st["soglie"].items(), key=lambda kv: int(kv[0])):
            if valore >= soglia:
                livello = int(liv)
    trend = None
    if len(serie) >= 2:
        d = serie[-1] - serie[0]
        trend = "in aumento" if d > 0 else "in calo" if d < 0 else "stabile"
    prev = previsione_ufam(st)
    return {"disponibile": True, "id": st["id"], "nome": st["nome"], "luogo": st["luogo"],
            "tipo": st["tipo"], "parametro": st["param"], "unita": st["unita"], "previsione": prev,
            "ritmo": _ritmo_ufam(punti, valore, st),
            "valore": round(valore, 2), "livello": livello, "soglie": st.get("soglie"),
            "lat": st.get("lat"), "lon": st.get("lon"),
            "trend_8h": trend, "serie_8h": [round(v, 2) for v in serie], "fonte": usato}


PAROLE_Q = ("PORTATA", "ABFLUSS", "DÉBIT", "DEBIT", "DISCHARGE")
PAROLE_P = ("LIVELLO", "PEGEL", "NIVEAU", "WATER LEVEL", "WASSERSTAND")


def _estrai_ufam(js, param):
    """Cerca la serie giusta nel JSON dei grafici UFAM (nomi in IT/DE/FR/EN),
    oppure un attributo con l'ultimo valore nel formato geo.admin."""
    parole = PAROLE_Q if param == "Q" else PAROLE_P
    # formato "plots": {"plot": {"data": [{"name": ..., "x": [...], "y": [...]}]}}
    try:
        for serie in js["plot"]["data"]:
            nome = str(serie.get("name", "")).upper()
            if any(p in nome for p in parole):
                y = [v for v in serie.get("y", []) if isinstance(v, (int, float))]
                if y:
                    xs, ys = serie.get("x", []), serie.get("y", [])
                    recenti = [(_tempo(x), v) for x, v in list(zip(xs, ys))[-60:] if isinstance(v, (int, float))]
                    return float(y[-1]), y[-48:], [(t, v) for t, v in recenti if t is not None]
    except (KeyError, TypeError):
        pass
    # formato geo.admin identify: {"feature": {"attributes": {...}}}
    try:
        attr = js["feature"]["attributes"]
        for k, v in attr.items():
            if isinstance(v, (int, float)) and ("value" in k.lower() or "wert" in k.lower()):
                return float(v), [], []
    except (KeyError, TypeError):
        pass
    return None, [], []


def _ritmo_ufam(punti, valore, st):
    """Variazione nell'ultima ora e tempo stimato alla soglia del grado successivo, al ritmo attuale.
    Restituisce None se la serie non basta o se l'ultima misura è troppo vecchia: in quel caso non si segnala nulla."""
    if not punti:
        return None
    t0, v0 = punti[-1]
    eta = (ADESSO - t0).total_seconds() / 60
    if eta > 60 or eta < -10:
        return None

    def valore_a(minuti_prima):   # misura più vicina a t0 − n minuti, con tolleranza di 10 minuti
        bersaglio = t0 - timedelta(minutes=minuti_prima)
        t, v = min(punti, key=lambda tv: abs((tv[0] - bersaglio).total_seconds()))
        return v if abs((t - bersaglio).total_seconds()) <= 600 else None

    v60, v30 = valore_a(60), valore_a(30)
    if v60 is None:
        return None
    out = {"eta_minuti": int(eta), "delta_1h": round(v0 - v60, 3),
           "in_salita": bool(v30 is not None and v0 > v30 and v0 > v60)}
    soglie = sorted((int(k), float(v)) for k, v in (st.get("soglie") or {}).items())
    if soglie:
        sopra = [(g, s) for g, s in soglie if s > v0]
        sotto = [s for g, s in soglie if s <= v0]
        if sopra:
            g, s = sopra[0]
            # scala di riferimento per giudicare se la salita è significativa
            if sotto:
                scala = s - sotto[-1]
            elif st["param"] == "P" and len(soglie) >= 2:
                scala = soglie[1][1] - soglie[0][1]      # laghi: distanza tra le prime due soglie
            else:
                scala = s                                 # fiumi: da zero alla prima soglia
            out.update(grado_successivo=g, soglia_successiva=s, scala=round(scala, 3))
            if out["delta_1h"] > 0:
                out["ore_alla_soglia"] = round((s - v0) / out["delta_1h"], 2)
    return out


# ---------------------------------------------------------------- OASI: rete cantonale
TZ_OASI = timezone(timedelta(hours=1))   # OASI usa GMT+1 fisso, senza ora legale


OASI_BLOCCATO = {"stato": False, "motivo": ""}


def oasi(st):
    cfg = CFG["oasi"]
    base_nd = {"nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"],
               "parametro": st["param"], "bacino": st.get("bacino"), "lat": st.get("lat"), "lon": st.get("lon")}
    if OASI_BLOCCATO["stato"]:
        return {**non_disp("OASI non raggiungibile in questo ciclo"), **base_nd}
    oggi = ADESSO.astimezone(TZ_OASI).date()
    url = cfg["url"].format(domain=st["dominio"], param=st["param"], codice=quote(st["codice"]),
                            da=(oggi - timedelta(days=1)).isoformat(), a=oggi.isoformat())
    tentativi = []
    ponte = (CFG["oasi"].get("ponte_url") or "").strip()
    if ponte and "INDIRIZZO-IP" not in ponte:
        tentativi.append(ponte.rstrip("/") + "/" + f"{st['codice'].replace(' ', '_')}_{st['param']}.csv")
    tentativi += [url, url.replace("https://", "http://", 1)]
    testo, errori = None, []
    for tentativo in tentativi:
        try:
            testo = scarica(tentativo, timeout=8)
            break
        except requests.exceptions.ConnectionError as e:
            errori.append(f"{tentativo[:30]}: {type(e).__name__} – {str(e)[:100]}")
        except requests.exceptions.HTTPError as e:
            errori.append(f"{tentativo[:30]}: HTTP {e.response.status_code if e.response is not None else '?'}")
        except Exception as e:
            log(f"OASI {st['codice']}: {str(e)[:160]}")
            return {**non_disp(str(e)[:80]), **base_nd}
    if testo is None:
        # nessuna connessione né in https né in http: inutile insistere sulle altre stazioni
        OASI_BLOCCATO.update(stato=True, motivo=" | ".join(errori)[:200])
        log("OASI: connessione fallita, salto le stazioni successive. " + " | ".join(errori))
        return {**non_disp("OASI non risponde (connessione rifiutata o timeout)"), **base_nd}
    GREZZI.setdefault("oasi", {})[st["codice"]] = testo[-30000:]
    punti = []
    for riga in testo.splitlines():
        if riga.startswith("#") or ";" not in riga:
            continue
        campi = riga.split(";")
        if len(campi) < 2 or not campi[0][:2].isdigit():
            continue
        try:
            t = datetime.strptime(campi[0].strip(), "%d.%m.%Y %H:%M:%S").replace(tzinfo=TZ_OASI)
            v = float(campi[1])
        except ValueError:
            continue
        punti.append((t, v, campi[2].strip() == "*" if len(campi) > 2 else False))
    if not punti:
        return {**non_disp("nessun valore nel periodo"), **base_nd}
    t_ult, v_ult, provv = punti[-1]
    eta = int((ADESSO - t_ult).total_seconds() // 60)
    base = {"disponibile": True, "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"],
            "bacino": st.get("bacino"), "parametro": st["param"], "unita": st["unita"],
            "rilevamento": t_ult.astimezone(TZ).strftime("%d.%m. %H:%M"), "eta_minuti": eta,
            "provvisorio": provv, "proprietario": st.get("proprietario", cfg["proprietario"]),
            "lat": st.get("lat"), "lon": st.get("lon")}
    if st["param"] == "Prec":
        # i valori sono quantità per intervallo (10 min): si sommano su finestre
        def somma(ore):
            return round(sum(v for (t, v, _) in punti if (t_ult - t) < timedelta(hours=ore)), 1)
        orarie = []
        for h in range(23, -1, -1):
            a, b = t_ult - timedelta(hours=h + 1), t_ult - timedelta(hours=h)
            orarie.append(round(sum(v for (t, v, _) in punti if a < t <= b), 1))
        return {**base, "valore": somma(1), "pioggia_1h": somma(1), "pioggia_3h": somma(3),
                "pioggia_6h": somma(6), "pioggia_24h": somma(24), "orarie_24h": orarie}
    finestra = [v for (t, v, _) in punti if (t_ult - t) <= timedelta(hours=8)]
    passo = max(1, len(finestra) // 48)
    serie = finestra[::passo][-48:]
    trend = None
    if len(finestra) >= 2:
        d = finestra[-1] - finestra[0]
        trend = "in aumento" if d > 0 else "in calo" if d < 0 else "stabile"
    return {**base, "valore": round(v_ult, 2), "serie_8h": [round(v, 2) for v in serie], "trend_8h": trend}


# ---------------------------------------------------------------- UFAM: previsione idrologica (mediana, 25–75 %, min/max)
def previsione_ufam(st):
    """Legge il file del grafico delle previsioni (q_forecast per i fiumi, p_forecast per i laghi).
    Restituisce i massimi attesi a 24 e 48 h della mediana e dello scenario peggiore, con il grado di pericolo."""
    tipo = "p" if st["param"] == "P" else "q"
    url = f"https://www.hydrodaten.admin.ch/plots/{tipo}_forecast/{st['id']}_{tipo}_forecast_it.json"
    try:
        js = scarica(url, "json", timeout=15)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None          # nessuna previsione pubblicata per questa stazione
        return {"disponibile": False, "motivo": f"HTTP {e.response.status_code if e.response is not None else '?'}"}
    except Exception as e:
        return {"disponibile": False, "motivo": str(e)[:80]}
    GREZZI.setdefault("ufam_previsioni", {})[st["id"]] = js
    serie = {}
    try:
        for tr in js["plot"]["data"]:
            nome = str(tr.get("name", "")).lower()
            xs, ys = tr.get("x", []), tr.get("y", [])
            punti = [(_tempo(x), y) for x, y in zip(xs, ys) if isinstance(y, (int, float))]
            punti = [(t, y) for t, y in punti if t is not None]
            if "median" in nome or "mediana" in nome:
                serie["mediana"] = punti
            elif "max" in nome and "min" not in nome:
                serie["max"] = punti
            elif "75" in nome:
                serie["p75"] = punti
            elif "min" in nome and "max" in nome:
                # una sola traccia min/max: tengo il massimo per sicurezza
                serie.setdefault("max", punti)
    except (KeyError, TypeError):
        return {"disponibile": False, "motivo": "formato non riconosciuto"}
    if not serie.get("mediana"):
        return {"disponibile": False, "motivo": "mediana non trovata"}

    def massimo(nome, ore):
        fin = ADESSO + timedelta(hours=ore)
        vals = [y for t, y in serie.get(nome, []) if ADESSO <= t <= fin]
        return round(max(vals), 2) if vals else None

    def grado(v):
        if v is None or not st.get("soglie"):
            return None
        g = 1
        for liv, soglia in sorted(st["soglie"].items(), key=lambda kv: int(kv[0])):
            if v >= soglia:
                g = int(liv)
        return g

    med24, med48 = massimo("mediana", 24), massimo("mediana", 48)
    peggio48 = massimo("max", 48) or massimo("p75", 48)
    futuro = [(t, y) for t, y in serie["mediana"] if t >= ADESSO]
    passo = max(1, len(futuro) // 48)
    emessa = re.search(r"\d{2}\.\d{2}\.\d{2,4} \d{2}:\d{2}", json.dumps(js.get("plot", {}).get("layout", {}), ensure_ascii=False))
    return {"disponibile": True, "mediana_24h": med24, "mediana_48h": med48, "peggiore_48h": peggio48,
            "grado_mediana_48h": grado(med48), "grado_peggiore_48h": grado(peggio48),
            "serie_mediana_48h": [round(y, 2) for _, y in futuro[:96][::passo]],
            "emessa": emessa.group(0) if emessa else None, "fonte": "UFAM, previsione d'insieme (21 corse)"}


def _tempo(x):
    """Converte l'asse temporale del grafico (ISO o epoch) in datetime UTC."""
    try:
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, timezone.utc)
        t = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=TZ).astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------- MeteoSvizzera: previsione locale ufficiale (dati aperti)
MS_CHIAVI = ("temperatura", "pioggia", "pioggia_alta", "raffica", "raffica_alta", "vento", "zero_termico", "prob_pioggia_3h")
MS_OBBLIGATORI = ("temperatura", "pioggia", "raffica")


def _ms_ultima_corsa(cfg):
    """Cerca la corsa più recente che contiene tutti i parametri richiesti. I file stanno in un «item» per giorno
    (AAAAMMGG-ch) e si chiamano …<corsa AAAAMMGGHHMM>.<parametro>.csv. Restituisce (corsa, {parametro: url})."""
    richiesti = {v for k, v in cfg["parametri"].items() if k in MS_OBBLIGATORI}
    for giorni_fa in (0, 1, 2):
        giorno = (ADESSO - timedelta(days=giorni_fa)).strftime("%Y%m%d")
        try:
            js = scarica(cfg["url_item"].format(giorno=giorno), "json", timeout=30)
        except Exception as e:
            log(f"MeteoSvizzera: elenco del {giorno} non letto ({str(e)[:80]})")
            continue
        corse = {}
        for nome, a in (js.get("assets") or {}).items():
            m = re.search(r"\.(\d{12})\.([a-z0-9]+)\.csv$", nome)
            if m and isinstance(a, dict) and a.get("href"):
                corse.setdefault(m.group(1), {})[m.group(2)] = a["href"]
        complete = [c for c, f in corse.items() if richiesti <= set(f)]
        if complete:
            c = max(complete)
            return c, corse[c]
    return None, {}


def _ms_leggi_file(url, prefissi):
    """Scorre un file CSV (fino a 33 MB, tutti i punti della Svizzera) tenendo solo le righe dei punti richiesti.
    prefissi: {b"690000;2;": chiave}. Restituisce {chiave: {AAAAMMGGHHMM: valore}}."""
    out = {k: {} for k in prefissi.values()}
    inizi = tuple(prefissi)
    with requests.get(url, headers=HEADERS, timeout=(15, 90), stream=True) as r:
        r.raise_for_status()
        for riga in r.iter_lines():
            if not riga or not riga.startswith(inizi):
                continue
            campi = riga.decode("latin-1").split(";")
            if len(campi) < 4:
                continue
            try:
                out[prefissi[(campi[0] + ";" + campi[1] + ";").encode()]][campi[2].strip()] = float(campi[3])
            except (KeyError, ValueError):
                continue
    return out


def meteosvizzera_locale(prec):
    """Previsione locale ufficiale di MeteoSvizzera (la stessa dell'app), per i punti di config.json.
    Si scaricano i file solo quando esce una corsa nuova; fra una corsa e l'altra vale l'ultima lettura.
    Una corsa più vecchia di max_eta_ore non viene usata: la pagina ripiega su Open-Meteo e lo dichiara."""
    cfg = (CFG["previsione"].get("meteosvizzera") or {})
    if not cfg.get("url_item"):
        return None
    prec = prec if isinstance(prec, dict) else {}

    def valida(d):
        t = _ms_tempo(d.get("corsa"))
        return bool(d.get("disponibile") and t and (ADESSO - t).total_seconds() <= cfg.get("max_eta_ore", 6) * 3600)

    # se in config.json sono stati aggiunti punti, la lettura salvata non basta: si riscarica
    completo = {str(k) for k in (cfg.get("punti") or {})} <= set(prec.get("punti") or {})

    try:   # controllo dell'elenco al massimo ogni intervallo_minuti
        if prec.get("controllato") and (ADESSO - _tempo(prec["controllato"])).total_seconds() < cfg.get("intervallo_minuti", 20) * 60 and valida(prec) and completo:
            return prec
    except Exception:
        pass
    corsa, file = _ms_ultima_corsa(cfg)
    if corsa is None:
        if valida(prec):
            return {**prec, "nota": "elenco delle corse non raggiungibile, vale l'ultima lettura"}
        return non_disp("nessuna corsa di previsione trovata")
    if prec.get("corsa") == corsa and prec.get("disponibile") and completo:
        return {**prec, "controllato": ADESSO.isoformat(timespec="seconds")} if valida(prec) else non_disp("ultima corsa pubblicata troppo vecchia")
    punti = {str(k): v for k, v in (cfg.get("punti") or {}).items()}
    prefissi = {f"{pid};{v.get('tipo', 2)};".encode(): pid for pid, v in punti.items()}
    serie = {pid: {} for pid in punti}
    errori = []
    for chiave in MS_CHIAVI:
        par = cfg["parametri"].get(chiave)
        if not par or par not in file:
            continue
        try:
            letto = _ms_leggi_file(file[par], prefissi)
        except Exception as e:
            errori.append(f"{chiave}: {str(e)[:60]}")
            continue
        for pid, valori in letto.items():
            serie[pid][chiave] = valori
    principale = str(cfg.get("punto_principale"))
    if errori:
        log("MeteoSvizzera: " + " | ".join(errori))
    if any(not serie.get(principale, {}).get(k) for k in MS_OBBLIGATORI):
        if valida(prec):
            return {**prec, "nota": "nuova corsa non leggibile, vale la precedente"}
        return non_disp("file della previsione non leggibili" + (": " + errori[0] if errori else ""))
    out = {"disponibile": True, "corsa": corsa, "corsa_locale": _ms_tempo(corsa).astimezone(TZ).strftime("%d.%m. %H:%M"),
           "letto": ADESSO.isoformat(timespec="seconds"), "controllato": ADESSO.isoformat(timespec="seconds"),
           "fonte": "MeteoSvizzera, previsione locale", "punti": {}}
    for pid, v in punti.items():
        s = serie.get(pid) or {}
        if not s.get("temperatura"):
            continue
        da = (ADESSO - timedelta(hours=1)).strftime("%Y%m%d%H%M")      # le ore già trascorse non servono
        tempi = [t for t in sorted(set().union(*[set(x) for x in s.values()])) if t >= da]
        chiavi = MS_CHIAVI
        if pid != principale:
            # per gli altri punti bastano poche ore e le grandezze dei riepiloghi
            cantonale = v.get("gruppo") in ("ticino", "passi")
            limite = (ADESSO + timedelta(hours=56 if cantonale else 30)).strftime("%Y%m%d%H%M")
            tempi = [t for t in tempi if t <= limite]
            chiavi = ("temperatura", "pioggia", "pioggia_alta", "raffica", "raffica_alta") + (("zero_termico",) if cantonale else ())
        out["punti"][pid] = {"nome": v.get("nome"), "gruppo": v.get("gruppo", "comuni"), "quota": v.get("quota"), "t": tempi,
                             **{k: [s.get(k, {}).get(t) for t in tempi] for k in chiavi if s.get(k)}}
    GREZZI["meteosvizzera_locale"] = {"corsa": corsa, "punti": list(out["punti"]), "errori": errori}
    log(f"MeteoSvizzera: corsa {corsa} letta, {len(out['punti'])} punti")
    return out


MS_STATO = BASE / "archivio" / "stato" / "meteosvizzera.json"


def _ms_stato_leggi():
    """Le serie complete stanno in archivio/stato (salvato dal flusso insieme all'archivio), non nei file letti dalle pagine."""
    return _leggi_json(MS_STATO) if MS_STATO.exists() else None


def _ms_stato_scrivi(ms):
    try:
        MS_STATO.parent.mkdir(parents=True, exist_ok=True)
        MS_STATO.write_text(json.dumps(ms, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as e:
        log(f"MeteoSvizzera: stato non salvato ({str(e)[:100]})")


def _ms_sintesi(ms):
    """Ciò che del risultato va nei file delle pagine: tutto tranne le serie."""
    if not isinstance(ms, dict):
        return ms
    return {**{k: v for k, v in ms.items() if k != "punti"}, "numero_punti": len(ms.get("punti") or {})}


def _ms_come_generico(punto):
    """Serie MeteoSvizzera nella forma attesa da _neve_48h (ore locali, precipitazione, zero termico):
    serve per stimare il limite delle nevicate con dati ufficiali quando il modello della quota neve non risponde."""
    ore = [_ms_tempo(t).astimezone(TZ).strftime("%Y-%m-%dT%H:%M") for t in punto["t"]]
    return {"hourly": {"time": ore, "precipitation": punto.get("pioggia") or [None] * len(ore),
                       "freezing_level_height": punto.get("zero_termico") or [None] * len(ore)},
            "elevation": punto.get("quota")}


def previsione_ticino(luoghi, ms, prec, luce=None):
    """Previsioni della pagina Ticino dai punti ufficiali MeteoSvizzera. Per il limite delle nevicate si chiede a Open-Meteo
    solo il modello della quota neve (una richiesta per località con coordinate); se manca, stima dai dati ufficiali."""
    out = {}
    for pid, pt in (ms.get("punti") or {}).items():
        if pt.get("gruppo") != "ticino" and pid != str(CFG["previsione"]["meteosvizzera"].get("punto_principale")):
            continue
        nome = pt.get("nome")
        r = _ms_riepilogo(pt, ore=(12,))["12h"]
        if r["raffica_max"] is None:
            continue
        coord = luoghi.get(nome)
        modello = _neve_modello(coord["lat"], coord["lon"]) if coord else None
        try:
            neve = _neve_48h(_ms_come_generico(pt), modello)
            if neve.get("modello", "").startswith("Open-Meteo, modello generico"):
                neve["modello"] = "MeteoSvizzera, previsione locale"
        except Exception as e:
            log(f"Neve {nome}: {str(e)[:100]}")
            neve = {"zero_termico_min": None, "zero_termico_max": None, "precipitazione": None}
        out[nome] = {"disponibile": True, "ufficiale": True, "fonte": "MeteoSvizzera, previsione locale ufficiale",
                     "corsa": ms.get("corsa_locale"), "quota": pt.get("quota"),
                     "massimi_12h": {"raffica_kmh": r["raffica_max"], "raffica_alta_kmh": r["raffica_alta_max"],
                                     "pioggia_totale_mm": r["pioggia"], "pioggia_totale_alta_mm": r["pioggia_alta"],
                                     "temperatura": r["tmax"]},
                     "neve_48h": neve, "luce": luce if nome == "Lugano" and luce else [], "riutilizzata": False,
                     "letta": ms.get("letto")}
    ordine = {n: i for i, n in enumerate(CFG["previsione"]["meteosvizzera"].get("ordine_ticino") or [])}
    return dict(sorted(out.items(), key=lambda kv: ordine.get(kv[0], 99)))


def previsione_passi(ms):
    """Passi alpini: temperatura, raffiche e precipitazione delle prossime 24 ore dai punti di montagna MeteoSvizzera.
    «Neve» = precipitazione nelle ore con temperatura prevista fino a 1 °C (indicazione, non una previsione di neve fresca)."""
    out = []
    for pid, pt in (ms.get("punti") or {}).items():
        if pt.get("gruppo") != "passi":
            continue
        futuri = [i for i, t in enumerate(pt["t"]) if (_ms_tempo(t) or ADESSO) > ADESSO][:24]
        g = lambda k: [pt[k][i] for i in futuri if k in pt and pt[k][i] is not None]
        tem, raf, raf_a, pio = g("temperatura"), g("raffica"), g("raffica_alta"), g("pioggia")
        if not tem:
            continue
        neve = sum(pt["pioggia"][i] for i in futuri if "pioggia" in pt and pt["pioggia"][i] is not None
                   and pt["temperatura"][i] is not None and pt["temperatura"][i] <= 1.0)
        out.append({"nome": pt.get("nome"), "quota": pt.get("quota"), "temperatura_ora": tem[0], "tmin": min(tem), "tmax": max(tem),
                    "raffica_max": max(raf) if raf else None, "raffica_alta_max": max(raf_a) if raf_a else None,
                    "precipitazione_mm": round(sum(pio), 1) if pio else None, "di_cui_con_gelo_mm": round(neve, 1), "ore": len(futuri)})
    return out


def _ms_tempo(v):
    try:
        return datetime.strptime(str(v), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ms_riepilogo(punto, ore=(12, 24)):
    """Massimi e somme delle prossime ore per un punto (usato per l'elenco dei Comuni)."""
    righe = [(t, i) for i, t in enumerate(punto["t"]) if (_ms_tempo(t) or ADESSO) > ADESSO]
    def col(k, n):
        return [punto[k][i] for _, i in righe[:n] if k in punto and punto[k][i] is not None]
    r = {}
    for n in ore:
        raf, raf_a, pio, pio_a, tem = col("raffica", n), col("raffica_alta", n), col("pioggia", n), col("pioggia_alta", n), col("temperatura", n)
        r[f"{n}h"] = {"raffica_max": max(raf) if raf else None, "raffica_alta_max": max(raf_a) if raf_a else None,
                      "pioggia": round(sum(pio), 1) if pio else None, "pioggia_alta": round(sum(pio_a), 1) if pio_a else None,
                      "tmin": min(tem) if tem else None, "tmax": max(tem) if tem else None}
    return r


def _applica_ms(out, punto, ms):
    """Sostituisce nei riquadri «Prossime 12 ore» e «Prossimi giorni» i valori di Open-Meteo con la previsione ufficiale.
    Da Open-Meteo restano solo il tipo di tempo, l'umidità e gli orari di luce, che MeteoSvizzera non fornisce in questa forma."""
    g = lambda k, i: punto[k][i] if k in punto and i < len(punto[k]) else None
    futuri = [(t, i) for i, t in enumerate(punto["t"]) if (_ms_tempo(t) or ADESSO) > ADESSO - timedelta(minutes=1) and g("temperatura", i) is not None]
    if len(futuri) < 12:
        return False
    vecchio_orario = {o["ora"]: o for o in (out.get("orario") or [])}
    orario = []
    for t, i in futuri[:12]:
        loc = _ms_tempo(t).astimezone(TZ)
        ora = loc.strftime("%H:%M")
        # probabilità su 3 ore: vale il primo intervallo che termina a quest'ora o dopo
        prob = next((punto["prob_pioggia_3h"][j] for j in range(i, min(i + 3, len(punto["t"])))
                     if punto["prob_pioggia_3h"][j] is not None), None) if "prob_pioggia_3h" in punto else None
        om = vecchio_orario.get(ora, {})
        orario.append({"ora": ora, "temperatura": g("temperatura", i), "umidita": om.get("umidita"), "pioggia_mm": g("pioggia", i),
                       "pioggia_alta_mm": g("pioggia_alta", i), "prob_pioggia": round(prob) if prob is not None else None,
                       "vento_kmh": g("vento", i), "raffica_kmh": g("raffica", i), "raffica_alta_kmh": g("raffica_alta", i),
                       "codice": om.get("codice")})
    def mx(k):
        v = [o[k] for o in orario if o.get(k) is not None]
        return max(v) if v else None
    def somma(k):
        v = [o[k] for o in orario if o.get(k) is not None]
        return round(sum(v), 1) if v else None
    massimi = {"raffica_kmh": mx("raffica_kmh"), "raffica_alta_kmh": mx("raffica_alta_kmh"), "pioggia_oraria_mm": mx("pioggia_mm"),
               "pioggia_totale_mm": somma("pioggia_mm"), "pioggia_totale_alta_mm": somma("pioggia_alta_mm"), "temperatura": mx("temperatura")}
    # giorni: minime, massime e somme calcolate sulle ore di ciascun giorno locale (il primo e l'ultimo possono essere parziali)
    per_giorno = {}
    for t, i in futuri:
        d = (_ms_tempo(t) - timedelta(minutes=1)).astimezone(TZ).strftime("%Y-%m-%d")
        per_giorno.setdefault(d, []).append(i)
    codici = {x["data"]: x.get("codice") for x in (out.get("giorni") or [])}
    giorni = []
    for d in sorted(per_giorno)[:6]:
        idx = per_giorno[d]
        tem = [g("temperatura", i) for i in idx if g("temperatura", i) is not None]
        pio = [g("pioggia", i) for i in idx if g("pioggia", i) is not None]
        raf = [g("raffica", i) for i in idx if g("raffica", i) is not None]
        if not tem:
            continue
        giorni.append({"data": d, "tmax": max(tem), "tmin": min(tem), "pioggia_mm": round(sum(pio), 1) if pio else None,
                       "raffica_max": max(raf) if raf else None, "codice": codici.get(d), "ore": len(idx)})
    zero = [g("zero_termico", i) for _, i in futuri[:48] if g("zero_termico", i) is not None]
    out.update(orario=orario, giorni=giorni, massimi_12h=massimi, ufficiale=True, corsa=ms.get("corsa_locale"),
               fonte=f"MeteoSvizzera, previsione locale ufficiale (corsa del {ms.get('corsa_locale')})",
               fonte_secondaria="tipo di tempo, umidità, luce e limite neve: Open-Meteo")
    if zero and isinstance(out.get("neve_48h"), dict):
        out["neve_48h"].update(zero_termico_min=round(min(zero)), zero_termico_max=round(max(zero)), zero_termico_ora=round(zero[0]),
                               zero_termico_fonte="MeteoSvizzera")
    return True


# ---------------------------------------------------------------- Open-Meteo: previsione
ORE_SERIE = 60   # ore di previsione conservate per il riuso (48 h di finestra neve + margine)


def previsione(lat=None, lon=None, precedente=None, ms=None):
    out = _previsione_open_meteo(lat, lon, precedente)
    punto = None
    try:
        if ms and ms.get("disponibile"):
            punto = (ms.get("punti") or {}).get(str(CFG["previsione"]["meteosvizzera"].get("punto_principale")))
    except Exception:
        punto = None
    if punto is None:
        return out
    if not out.get("disponibile"):      # Open-Meteo non risponde: la previsione ufficiale basta da sola
        out = {"disponibile": True, "orario": [], "giorni": [], "massimi_12h": {}, "luce": [], "riutilizzata": False,
               "neve_48h": {"zero_termico_min": None, "zero_termico_max": None, "zero_termico_ora": None, "limite_neve_min": None,
                            "neve_48h_cm": None, "precipitazione": None, "nota": "Open-Meteo non disponibile"},
               "letta": ADESSO.isoformat(timespec="seconds")}
        senza_om = True
    else:
        senza_om = False
    try:
        if _applica_ms(out, punto, ms):
            if senza_om:
                out["fonte_secondaria"] = "tipo di tempo, umidità, luce e limite neve non disponibili (Open-Meteo non risponde)"
            return out
    except Exception as e:
        log(f"MeteoSvizzera: applicazione non riuscita ({str(e)[:120]})")
    return out if not senza_om else non_disp("previsione non disponibile")


def _previsione_open_meteo(lat=None, lon=None, precedente=None):
    """Legge la previsione Open-Meteo. Se il servizio non risponde (anche dopo un secondo tentativo),
    riusa l'ultima lettura riuscita, ricalcolata sull'ora attuale e marcata con l'ora di lettura,
    purché non sia più vecchia di previsione.riuso_max_ore (config.json, predefinito 6 h)."""
    p = CFG["previsione"]
    url = p["url"].format(lat=lat if lat is not None else p["lat"], lon=lon if lon is not None else p["lon"])
    errore = None
    modello = _neve_modello(lat if lat is not None else p["lat"], lon if lon is not None else p["lon"])
    for tentativo in (1, 2):
        try:
            js = scarica(url, "json", timeout=15)
            GREZZI["open_meteo"] = js
            out = _elabora_previsione(js, modello)
            out["letta"] = ADESSO.isoformat(timespec="seconds")
            out["riutilizzata"] = False
            out["_serie"] = _serie_previsione(js)
            return out
        except Exception as e:
            errore = e
    # servizio non raggiungibile: ultima lettura riuscita, se abbastanza recente
    try:
        serie, letta = precedente["_serie"], _tempo(precedente["letta"])
        eta_ore = (ADESSO - letta).total_seconds() / 3600
        if 0 <= eta_ore <= p.get("riuso_max_ore", 6):
            out = _elabora_previsione(serie, modello)
            if out["orario"]:
                ora_lettura = letta.astimezone(TZ).strftime("%H:%M")
                out.update(fonte=f"Open-Meteo (non ufficiale), ultima lettura {ora_lettura}",
                           letta=precedente["letta"], riutilizzata=True, _serie=serie,
                           motivo_riuso=str(errore)[:200])
                log(f"Previsione: Open-Meteo non risponde, riuso la lettura delle {ora_lettura}")
                return out
    except Exception:
        pass
    return non_disp(errore)


def _serie_previsione(js):
    """Estratto compatto della risposta Open-Meteo, salvato nel file dati per poterlo riusare al ciclo successivo."""
    h = js.get("hourly", {})
    ore = h.get("time", [])
    adesso_loc = ADESSO.astimezone(TZ).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    i0 = next((i for i, t in enumerate(ore) if t >= adesso_loc), 0)
    return {"hourly": {k: v[i0:i0 + ORE_SERIE] for k, v in h.items() if isinstance(v, list)},
            "daily": js.get("daily", {})}


def _neve_modello(lat, lon):
    """Seconda lettura, solo per la neve: modello MeteoSvizzera ICON-CH (tramite Open-Meteo), che fornisce
    la quota delle nevicate calcolata dal modello. Se non risponde restituisce None e si ripiega sulla stima."""
    url = ((CFG["previsione"].get("neve") or {}).get("url_modello") or "").strip()
    if not url:
        return None
    try:
        js = scarica(url.format(lat=lat, lon=lon), "json", timeout=15)
    except Exception as e:
        log(f"Neve: modello MeteoSvizzera non letto ({str(e)[:120]}), uso la stima dallo zero termico")
        return None
    GREZZI.setdefault("open_meteo_neve", {})[f"{lat},{lon}"] = js
    return js


def _neve_48h(generico, modello):
    """Limite delle nevicate nelle prossime 48 ore, considerando solo le ore con precipitazione prevista.
    Con il modello MeteoSvizzera si usa la quota delle nevicate del modello; altrimenti zero termico meno
    uno scarto che cresce con l'intensità della precipitazione (la fusione raffredda l'aria)."""
    cfg = CFG["previsione"].get("neve") or {}
    prec_min = cfg.get("prec_minima_mm", 0.1)
    scarti = sorted(cfg.get("scarti_m") or [[0, 300], [1, 450], [4, 600]])
    adesso_loc = ADESSO.astimezone(TZ).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    def finestra(js):
        h = (js or {}).get("hourly") or {}
        ore = h.get("time") or []
        i0 = next((i for i, t in enumerate(ore) if t >= adesso_loc), len(ore))
        col = lambda k: [v if isinstance(v, (int, float)) else None for v in (h.get(k) or [])[i0:i0 + 48]]
        return {"ore": ore[i0:i0 + 48], "prec": col("precipitation"), "zt": col("freezing_level_height"),
                "neve": col("snowfall"), "quota": col("snowfall_height")}

    g, m = finestra(generico), finestra(modello)
    # il modello vale se copre almeno 24 delle 48 ore con la precipitazione
    da_modello = sum(v is not None for v in m["prec"]) >= 24
    f = m if da_modello else g
    n = len(f["ore"])
    zt = [v for v in f["zt"] if v is not None] or [v for v in g["zt"] if v is not None]
    nv = [v for v in f["neve"] if v is not None]
    out = {"zero_termico_min": round(min(zt)) if zt else None, "zero_termico_max": round(max(zt)) if zt else None,
           "zero_termico_ora": round(zt[0]) if zt else None,
           # campo storico (zero termico minimo − 300 m, anche senza precipitazioni): resta per le pagine non ancora aggiornate
           "limite_neve_min": max(0, round(min(zt) - 300, -1)) if zt else None,
           "neve_48h_cm": round(sum(nv), 1) if nv else None}
    if n < 12 or not any(v is not None for v in f["prec"]):
        out.update(precipitazione=None, nota="precipitazione prevista non disponibile")
        return out
    quote, stimate = [], 0
    for i in range(n):
        p = f["prec"][i]
        if p is None or p < prec_min:
            continue
        q = f["quota"][i] if da_modello and i < len(f["quota"]) else None
        if q is None:
            z = f["zt"][i] if i < len(f["zt"]) else None
            if z is None:
                continue
            scarto = [s for soglia, s in scarti if p >= soglia][-1]
            q, stimata = z - scarto, True
            stimate += 1
        quote.append((max(0.0, q), f["ore"][i]))
    quota_luogo = (modello if da_modello else generico or {}).get("elevation")
    out.update(precipitazione=bool(quote), ore_con_precipitazione=len(quote),
               modello="MeteoSvizzera ICON-CH (via Open-Meteo)" if da_modello else "Open-Meteo, modello generico")
    if not quote:
        out["nota"] = "nessuna precipitazione prevista nelle prossime 48 ore"
        return out
    q_min, quando = min(quote)
    q_max = max(quote)[0]
    arr = lambda v: int(round(v / 50.0) * 50)
    try:
        t = datetime.fromisoformat(quando)
        giorni = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
        quando_txt = f"{giorni[t.weekday()]} {t:%H:%M}"
    except ValueError:
        quando_txt = quando
    out.update(limite_min=arr(q_min), limite_max=arr(q_max), limite_quando=quando_txt,
               metodo="modello" if stimate == 0 else "stima",
               fino_al_piano=bool(isinstance(quota_luogo, (int, float)) and q_min <= quota_luogo + 50),
               quota_luogo=round(quota_luogo) if isinstance(quota_luogo, (int, float)) else None,
               nota="quota delle nevicate del modello, solo ore con precipitazione" if stimate == 0
               else "stima: zero termico meno 300–600 m secondo l'intensità, solo ore con precipitazione")
    return out


def _elabora_previsione(js, modello=None):
    h = js.get("hourly", {})
    ore = h.get("time", [])
    adesso_loc = ADESSO.astimezone(TZ).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    i0 = next((i for i, t in enumerate(ore) if t >= adesso_loc), len(ore))
    orario = []
    for i in range(i0, min(i0 + 12, len(ore))):
        orario.append({
            "ora": ore[i][11:16], "temperatura": h["temperature_2m"][i],
            "umidita": h["relative_humidity_2m"][i], "pioggia_mm": h["precipitation"][i],
            "prob_pioggia": h["precipitation_probability"][i], "vento_kmh": h["wind_speed_10m"][i],
            "raffica_kmh": h["wind_gusts_10m"][i], "codice": h["weather_code"][i],
        })
    d = js.get("daily", {})
    oggi = ADESSO.astimezone(TZ).strftime("%Y-%m-%d")
    gi = [i for i, g in enumerate(d.get("time", [])) if g >= oggi]     # solo da oggi in avanti
    giorni = [{"data": d["time"][i], "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
               "pioggia_mm": d["precipitation_sum"][i], "raffica_max": d["wind_gusts_10m_max"][i],
               "codice": d["weather_code"][i]} for i in gi]
    # zero termico e limite delle nevicate nelle prossime 48 ore
    try:
        neve = _neve_48h(js, modello)
    except Exception as e:      # la neve non deve mai far cadere il resto della previsione
        log(f"Neve: elaborazione non riuscita ({str(e)[:120]})")
        neve = {"zero_termico_min": None, "zero_termico_max": None, "zero_termico_ora": None,
                "limite_neve_min": None, "neve_48h_cm": None, "precipitazione": None, "nota": "non disponibile"}
    # ore di luce di oggi e domani
    luce = []
    for i in gi[:2]:
        alba, tram = d["sunrise"][i][11:16], d["sunset"][i][11:16]
        luce.append({"data": d["time"][i], "alba": alba, "tramonto": tram,
                     "crepuscolo_mattino": _sposta(alba, -32), "crepuscolo_sera": _sposta(tram, 32)})
    massimi = {
        "raffica_kmh": max((o["raffica_kmh"] for o in orario), default=None),
        "pioggia_oraria_mm": max((o["pioggia_mm"] for o in orario), default=None),
        "pioggia_totale_mm": round(sum(o["pioggia_mm"] for o in orario), 1) if orario else None,
        "temperatura": max((o["temperatura"] for o in orario), default=None),
    }
    return {"disponibile": True, "fonte": "Open-Meteo (non ufficiale)", "orario": orario,
            "giorni": giorni, "massimi_12h": massimi, "neve_48h": neve, "luce": luce}


def _leggi_json(percorso):
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sposta(hhmm, minuti):
    """Sposta un orario HH:MM di n minuti (crepuscolo civile ≈ 32 min prima dell'alba / dopo il tramonto alle nostre latitudini in inverno)."""
    try:
        hh, mm = map(int, hhmm.split(":"))
        t = (hh * 60 + mm + minuti) % 1440
        return f"{t // 60:02d}:{t % 60:02d}"
    except Exception:
        return None


# ---------------------------------------------------------------- variazioni rispetto al ciclo precedente
def variazioni(nuovo, vecchio):
    if not vecchio:
        return [{"testo": "Prima situazione registrata.", "tipo": "info"}]
    out = []
    va, na = vecchio.get("allerte", {}), nuovo.get("allerte", {})
    for comune, nd in na.items():
        vd = va.get(comune, {})
        if nd.get("disponibile") and vd.get("disponibile"):
            if nd["livello_max"] != vd["livello_max"]:
                out.append({"testo": f"{comune}: livello di allerta da {vd['livello_max']} a {nd['livello_max']}",
                            "tipo": "peggiora" if nd["livello_max"] > vd["livello_max"] else "migliora"})
    vp = {p["id"]: p for p in vecchio.get("pegel", []) if p.get("disponibile")}
    for p in nuovo.get("pegel", []):
        if (p.get("disponibile") and p["id"] in vp and p["livello"] is not None
                and vp[p["id"]].get("livello") is not None and p["livello"] != vp[p["id"]]["livello"]):
            out.append({"testo": f"{p['nome']} ({p['luogo']}): livello da {vp[p['id']]['livello']} a {p['livello']}",
                        "tipo": "peggiora" if p["livello"] > vp[p["id"]]["livello"] else "migliora",
                        "chiave": f"grado:{p['id']}", "da": vp[p["id"]]["livello"], "a": p["livello"]})
    vs, ns = vecchio.get("alertswiss", {}), nuovo.get("alertswiss", {})
    if vs.get("disponibile") and ns.get("disponibile"):
        vt = {v["titolo"] for v in vs["notifiche"]}
        for v in ns["notifiche"]:
            if v["titolo"] not in vt:
                out.append({"testo": f"Nuova notifica Alertswiss: {v['titolo']}", "tipo": "peggiora"})
    vv, nvv = vecchio.get("valanghe") or {}, nuovo.get("valanghe") or {}
    if vv.get("disponibile") and nvv.get("disponibile"):
        a, b = vv.get("grado_max") or 0, nvv.get("grado_max") or 0
        if a != b:
            out.append({"testo": f"Valanghe (SLF): grado massimo in Ticino da {a or 'nessun bollettino'} a {b or 'nessun bollettino'}",
                        "tipo": "peggiora" if b > a else "migliora"})
    vpk, npk = vecchio.get("picchetto") or {}, nuovo.get("picchetto") or {}
    vi, ni = vpk.get("incendio") or {}, npk.get("incendio") or {}
    if vi.get("disponibile") and ni.get("disponibile"):
        prima = {r["id"]: r for r in vi.get("regioni") or []}
        for r in ni.get("regioni") or []:
            p = prima.get(r["id"])
            if p and p.get("livello") is not None and r.get("livello") is not None and p["livello"] != r["livello"]:
                out.append({"testo": f"Incendio di bosco, {r['nome']}: {r.get('misura') or 'misura ' + str(r['livello'])}",
                            "tipo": "peggiora" if r["livello"] > p["livello"] else "migliora"})
    vt, nt = vpk.get("terremoti") or {}, npk.get("terremoti") or {}
    if vt.get("disponibile") and nt.get("disponibile"):
        noti = {e["ora"] for e in vt.get("eventi") or []}
        for e in nt.get("eventi") or []:
            if e["ora"] not in noti and e.get("naturale") and e["magnitudo"] >= nt.get("soglia_avvertibile", 2.5):
                out.append({"testo": f"Terremoto di magnitudo {e['magnitudo']} – {e['luogo']} ({e['ora_locale']})", "tipo": "peggiora"})
    for fonte in ("misure", "allerte", "alertswiss", "pegel", "oasi", "previsione"):
        if _ok(nuovo, fonte) != _ok(vecchio, fonte):
            out.append({"testo": f"Fonte «{fonte}»: {'di nuovo disponibile' if _ok(nuovo, fonte) else 'NON disponibile'}",
                        "tipo": "info"})
    return out or [{"testo": "Nessuna variazione rispetto al ciclo precedente.", "tipo": "info"}]


def _ok(d, fonte):
    v = d.get(fonte)
    if isinstance(v, dict) and "disponibile" in v:
        return bool(v["disponibile"])
    if isinstance(v, dict):
        return any(x.get("disponibile") for x in v.values() if isinstance(x, dict))
    if isinstance(v, list):
        return any(x.get("disponibile") for x in v)
    return False


# ---------------------------------------------------------------- variazioni rapide (evidenze) e cronologia
EV_PREDEFINITO = {
    "durata_minuti": 60, "cronologia_ore": 6,
    "vento": {"dato_max_minuti": 40, "salto_kmh": 30, "stazioni_quota_alta": [],
              "pianura": {"rapida_kmh": 70, "critica_kmh": 90, "salto_minimo_kmh": 50},
              "quota_alta": {"rapida_kmh": 100, "critica_kmh": 130, "salto_minimo_kmh": 70}},
    "ufam": {"rapida_ore": 3, "critica_ore": 1, "salita_minima_frazione": 0.1},
}


def _cfg_ev():
    """Parametri delle evidenze: valori di config.json sopra i predefiniti."""
    utente = CFG.get("evidenza") or {}
    out = json.loads(json.dumps(EV_PREDEFINITO))
    for k, v in utente.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and isinstance(out[k].get(k2), dict):
                    out[k][k2].update(v2)
                else:
                    out[k][k2] = v2
        else:
            out[k] = v
    return out


def _n(v):
    """Numero in forma leggibile: senza decimali inutili."""
    return f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"


def _durata(ore):
    minuti = max(5, int(round(ore * 60 / 5.0)) * 5)
    return f"{minuti} min" if minuti < 60 else f"{minuti // 60} h" + (f" {minuti % 60:02d} min" if minuti % 60 else "")


def _ev_vento(m, E):
    """Raffiche: superamento delle soglie (allineate alle allerte MeteoSvizzera) oppure salto brusco."""
    v = E["vento"]
    if not m.get("disponibile") or m.get("eta_minuti") is None or m["eta_minuti"] > v["dato_max_minuti"]:
        return None
    serie = (m.get("serie_24h") or {}).get("raffica_kmh") or []
    recenti = [x for x in serie[-3:] if isinstance(x, (int, float))]          # ultimi 30 minuti
    prima = [x for x in serie[-9:-3] if isinstance(x, (int, float))]           # i 60 minuti precedenti
    if not recenti:
        return None
    alta = m.get("stazione") in v["stazioni_quota_alta"]
    s = v["quota_alta" if alta else "pianura"]
    ora, salto = max(recenti), None
    if len(prima) >= 3:
        salto = ora - max(prima)
    brusco = salto is not None and salto >= v["salto_kmh"]
    if ora >= s["critica_kmh"] or (brusco and ora >= s["rapida_kmh"]):
        livello = "critica"
    elif ora >= s["rapida_kmh"] or (brusco and ora >= s["salto_minimo_kmh"]):
        livello = "rapida"
    else:
        return None
    if brusco:
        testo = f"{m['nome']}: raffiche da {_n(max(prima))} a {_n(ora)} km/h in un'ora"
        breve = f"▲ +{_n(salto)} km/h in 1 h"
    else:
        soglia = s["critica_kmh"] if ora >= s["critica_kmh"] else s["rapida_kmh"]
        testo = f"{m['nome']}: raffica {_n(ora)} km/h (soglia {soglia} km/h)"
        breve = f"▲ oltre {soglia} km/h"
    return {"chiave": f"vento:{m.get('stazione')}", "ambito": "vento", "livello": livello, "testo": testo, "breve": breve}


def _ev_ufam(p, E):
    """Stazioni UFAM: tempo stimato alla soglia del grado successivo, al ritmo dell'ultima ora."""
    u, r = E["ufam"], p.get("ritmo")
    if not p.get("disponibile") or not r or not r.get("in_salita") or r.get("ore_alla_soglia") is None:
        return None
    if r["delta_1h"] < u["salita_minima_frazione"] * r["scala"]:
        return None
    ore = r["ore_alla_soglia"]
    if ore < u["critica_ore"]:
        livello = "critica"
    elif ore < u["rapida_ore"]:
        livello = "rapida"
    else:
        return None
    return {"chiave": f"ufam:{p['id']}", "ambito": "ufam", "livello": livello, "verso_grado": r["grado_successivo"],
            "testo": f"{p['nome']} ({p['luogo']}): +{_n(r['delta_1h'])} {p['unita']} in 1 h, al ritmo attuale "
                     f"soglia del grado {r['grado_successivo']} ({_n(r['soglia_successiva'])}) in circa {_durata(ore)}",
            "breve": f"▲ +{_n(r['delta_1h'])} in 1 h · grado {r['grado_successivo']} in ~{_durata(ore)}"}


def evidenze(nuovo, vecchio):
    """Aggiunge a `nuovo` le chiavi "evidenze" (variazioni rapide in corso o concluse da meno di durata_minuti)
    e "cronologia" (inizio delle evidenze e cambi di livello delle ultime ore).
    Una misura mancante o vecchia non genera mai un'evidenza; il ritorno a n/d non conta come variazione."""
    E = _cfg_ev()
    adesso_iso = nuovo["generato"]
    ora_loc = ADESSO.astimezone(TZ).strftime("%H:%M")
    attuali = []
    for m in (nuovo.get("misure") or {}).values():
        attuali.append(_ev_vento(m, E))
    for p in nuovo.get("pegel") or []:
        attuali.append(_ev_ufam(p, E))
    # passaggio a un grado superiore di una stazione UFAM (dalla lista delle variazioni di questo ciclo)
    for v in nuovo.get("variazioni") or []:
        if str(v.get("chiave", "")).startswith("grado:") and v.get("tipo") == "peggiora":
            critica = v["a"] >= 3 or v["a"] - v["da"] >= 2
            attuali.append({"chiave": v["chiave"], "ambito": "ufam", "livello": "critica" if critica else "rapida",
                            "testo": v["testo"].replace("livello da", "grado da"), "breve": f"▲ grado {v['da']} → {v['a']}",
                            "una_tantum": True})
    attuali = {e["chiave"]: e for e in attuali if e}

    precedenti = {e["chiave"]: e for e in ((vecchio or {}).get("evidenze") or []) if isinstance(e, dict) and e.get("chiave")}
    cronologia = [c for c in ((vecchio or {}).get("cronologia") or []) if isinstance(c, dict)]
    out = []
    for chiave, e in attuali.items():
        prec = precedenti.get(chiave)
        una_tantum = e.pop("una_tantum", False)
        nuova = prec is None
        sale = prec is not None and prec.get("livello") == "rapida" and e["livello"] == "critica"
        e.update(dal=adesso_iso if (nuova or una_tantum) else prec.get("dal", adesso_iso), ultimo=adesso_iso,
                 attiva=not una_tantum, dal_locale=ora_loc if (nuova or una_tantum) else prec.get("dal_locale", ora_loc))
        if sale:
            e.update(critica_dal=adesso_iso)
        elif prec and prec.get("critica_dal") and e["livello"] == "critica":
            e["critica_dal"] = prec["critica_dal"]
        out.append(e)
        if (nuova or sale) and not una_tantum:      # i cambi di grado entrano in cronologia come variazioni
            cronologia.append({"ora": adesso_iso, "ora_locale": ora_loc, "testo": e["testo"], "tipo": "peggiora",
                               "livello": e["livello"], "chiave": chiave})
    limite = ADESSO - timedelta(minutes=E["durata_minuti"])
    gradi = {f"ufam:{p['id']}": p.get("livello") for p in nuovo.get("pegel") or [] if p.get("disponibile")}
    for chiave, e in precedenti.items():
        if chiave in attuali:
            continue
        # «verso il grado N» è superata se la stazione ha nel frattempo raggiunto quel grado: resta il cambio di grado
        if e.get("verso_grado") and (gradi.get(chiave) or 0) >= e["verso_grado"]:
            continue
        t = _tempo(e.get("ultimo"))
        if t is not None and t >= limite:
            out.append({**e, "attiva": False})
    for v in nuovo.get("variazioni") or []:
        if v.get("tipo") in ("peggiora", "migliora"):
            ev = attuali.get(v.get("chiave"))
            cronologia.append({"ora": adesso_iso, "ora_locale": ora_loc, "testo": v["testo"], "tipo": v["tipo"],
                               "livello": ev["livello"] if ev else None, "chiave": v.get("chiave")})
    limite_c = ADESSO - timedelta(hours=E["cronologia_ore"])
    cronologia = [c for c in cronologia if (_tempo(c.get("ora")) or limite_c) > limite_c][-40:]
    ordine = {"critica": 0, "rapida": 1}
    out.sort(key=lambda e: (not e.get("attiva"), ordine.get(e.get("livello"), 2), e.get("testo", "")))
    nuovo["evidenze"] = out
    nuovo["cronologia"] = cronologia
    nuovo["evidenza_durata_minuti"] = E["durata_minuti"]


# ---------------------------------------------------------------- pagina «Picchetto»: altri fattori di rischio
def _riusa(prec, minuti):
    """Le fonti del picchetto cambiano lentamente: si rileggono solo ogni `minuti`, altrimenti vale l'ultima lettura riuscita."""
    try:
        if prec and prec.get("disponibile") and (ADESSO - _tempo(prec["letto"])).total_seconds() < minuti * 60:
            return True
    except Exception:
        pass
    return False


def incendio_bosco(cfg, prec):
    """Misure di prevenzione degli incendi di bosco in vigore (UFAM, waldbrandgefahr.ch): è la scala delle MISURE
    (1 = nessuna … 5 = divieto assoluto di accendere fuochi all'aperto), non il grado di pericolo."""
    if _riusa(prec, cfg.get("intervallo_minuti", 60)):
        return prec
    try:
        js = scarica(cfg["url"], "json", timeout=40)
    except Exception as e:
        return non_disp(e)
    ids = set(cfg.get("regioni") or [])
    regioni = []
    for f in js.get("features") or []:
        pr = f.get("properties") or {}
        if pr.get("region_id") not in ids:
            continue
        regioni.append({"id": pr.get("region_id"), "nome": re.sub(r"\s*\((TI|GR)\)$", "", str(pr.get("name_it") or pr.get("name_de") or pr.get("region_id"))),
                        "cantone": pr.get("canton"), "livello": pr.get("level") if isinstance(pr.get("level"), int) else None,
                        "misura": pr.get("title_it") or pr.get("title_de"), "breve": pr.get("short_title_it"),
                        "descrizione": pr.get("description_it"), "dal": (_tempo(pr.get("valid_from")) or ADESSO).astimezone(TZ).strftime("%d.%m.%Y")
                        if pr.get("valid_from") else None})
    if not regioni:
        return non_disp("nessuna delle regioni configurate trovata nel file")
    ordine = {i: n for n, i in enumerate(cfg.get("regioni") or [])}
    regioni.sort(key=lambda r: ordine.get(r["id"], 99))
    GREZZI.setdefault("picchetto", {})["incendio"] = regioni
    livelli = [r["livello"] for r in regioni if r["livello"] is not None]
    return {"disponibile": True, "letto": ADESSO.isoformat(timespec="seconds"), "regioni": regioni,
            "livello_max": max(livelli) if livelli else None, "fonte": "UFAM / Cantoni, waldbrandgefahr.ch"}


def siccita(cfg, prec):
    """Indice combinato di siccità per regione (UFAM, trockenheit.admin.ch): settimana in corso e successiva,
    più il livello di allerta. Un file con validità scaduta da oltre `datato_giorni` viene marcato come datato."""
    if _riusa(prec, cfg.get("intervallo_minuti", 360)):
        return prec
    ids = set(cfg.get("regioni") or [])

    def leggi(url):
        js = scarica(url, "json", timeout=40)
        out = {}
        for f in js.get("features") or []:
            pr = f.get("properties") or {}
            if pr.get("ID") in ids:
                out[pr["ID"]] = pr
        return out

    try:
        ora = leggi(cfg["url_settimana"])
    except Exception as e:
        return non_disp(e)
    if not ora:
        return non_disp("nessuna delle regioni configurate trovata nel file")
    try:
        poi = leggi(cfg["url_prossima"])
    except Exception:
        poi = {}
    try:
        allerta = leggi(cfg["url_allerta"])
    except Exception:
        allerta = {}
    regioni, scadenze = [], []
    for i in cfg.get("regioni") or []:
        pr = ora.get(i)
        if not pr:
            continue
        fine = _tempo(_iso_fuso(pr.get("valid_to")))
        if fine:
            scadenze.append(fine)
        a, n = allerta.get(i) or {}, poi.get(i) or {}
        regioni.append({"id": i, "nome": pr.get("name_it") or pr.get("name_de"), "indice": pr.get("cdi-class"),
                        "testo": pr.get("info_it"), "indice_prossima": n.get("cdi-class"), "testo_prossima": n.get("info_it"),
                        "allerta": a.get("warnlevel"), "allerta_testo": a.get("info_it")})
    GREZZI.setdefault("picchetto", {})["siccita"] = {"settimana": list(ora.values()), "allerta": list(allerta.values())}
    fine = max(scadenze) if scadenze else None
    datato = bool(fine and (ADESSO - fine).total_seconds() > cfg.get("datato_giorni", 10) * 86400)
    indici = [r["indice"] for r in regioni if isinstance(r["indice"], int)]
    return {"disponibile": True, "letto": ADESSO.isoformat(timespec="seconds"), "regioni": regioni,
            "indice_max": max(indici) if indici else None, "datato": datato,
            "valido_fino": fine.astimezone(TZ).strftime("%d.%m.%Y") if fine else None,
            "fonte": "UFAM, piattaforma nazionale siccità"}


def _iso_fuso(v):
    """«2026-08-23T23:59:59+0200» → «…+02:00» (fromisoformat di Python < 3.11 non accetta il fuso senza due punti)."""
    return re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v) if isinstance(v, str) else v


def terremoti(cfg, prec):
    """Eventi sismici degli ultimi giorni nella zona indicata, dal servizio FDSN (formato testo, colonne separate da |).
    Nessun evento è un risultato valido, diverso da «fonte non disponibile»."""
    if _riusa(prec, cfg.get("intervallo_minuti", 10)):
        return prec
    z = cfg["zona"]
    inizio = (ADESSO - timedelta(days=cfg.get("giorni", 7))).strftime("%Y-%m-%dT%H:%M:%S")
    query = (f"?format=text&orderby=time&starttime={inizio}&minlatitude={z['lat_min']}&maxlatitude={z['lat_max']}"
             f"&minlongitude={z['lon_min']}&maxlongitude={z['lon_max']}&minmagnitude={cfg.get('magnitudo_minima', 1.0)}")
    testo, fonte, errori = None, None, []
    for c in cfg["url_candidati"]:
        try:
            r = requests.get(c["url"] + query, headers=HEADERS, timeout=15)
            if r.status_code in (204, 404):       # FDSN: nessun evento nel periodo
                testo, fonte = "", c["nome"]
                break
            r.raise_for_status()
            if "|" not in r.text:
                raise ValueError("risposta non in formato testo FDSN")
            testo, fonte = r.text, c["nome"]
            break
        except Exception as e:
            errori.append(f"{c['nome']}: {str(e)[:80]}")
    if testo is None:
        log("Terremoti: " + " | ".join(errori))
        return non_disp("nessun servizio sismico risponde")
    GREZZI.setdefault("picchetto", {})["terremoti"] = testo[:20000]
    righe = [l for l in testo.splitlines() if l.strip()]
    colonne = [c.strip().lower() for c in righe[0].lstrip("#").split("|")] if righe and righe[0].startswith("#") else \
        ["eventid", "time", "latitude", "longitude", "depth/km", "author", "catalog", "contributor", "contributorid",
         "magtype", "magnitude", "magauthor", "eventlocationname", "eventtype"]
    eventi = []
    for l in righe:
        if l.startswith("#"):
            continue
        v = dict(zip(colonne, [x.strip() for x in l.split("|")]))
        try:
            t = _tempo(v["time"] if v["time"].endswith("Z") or "+" in v["time"][10:] else v["time"] + "+00:00")
            mag = float(v["magnitude"])
        except (KeyError, ValueError, TypeError):
            continue
        if t is None:
            continue
        tipo = (v.get("eventtype") or "").lower()
        eventi.append({"ora": t.isoformat(timespec="minutes"), "ora_locale": t.astimezone(TZ).strftime("%d.%m. %H:%M"),
                       "magnitudo": round(mag, 1), "tipo_magnitudo": v.get("magtype") or "",
                       "luogo": v.get("eventlocationname") or "", "profondita_km": _num(v.get("depth/km")),
                       "lat": _num(v.get("latitude")), "lon": _num(v.get("longitude")),
                       "naturale": tipo in ("", "earthquake"), "tipo": tipo})
    eventi.sort(key=lambda e: e["ora"], reverse=True)
    naturali = [e for e in eventi if e["naturale"]]
    return {"disponibile": True, "letto": ADESSO.isoformat(timespec="seconds"), "fonte": fonte, "giorni": cfg.get("giorni", 7),
            "eventi": eventi[:25], "numero": len(eventi), "magnitudo_max": max((e["magnitudo"] for e in naturali), default=None),
            "soglia_avvertibile": cfg.get("magnitudo_avvertibile", 2.5)}


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def picchetto(prec):
    """Dati della pagina «Picchetto». Ogni fonte è indipendente: l'errore di una non tocca le altre."""
    cfg = CFG.get("picchetto")
    if not cfg:
        return None
    prec = prec or {}
    out = {"collegamenti": cfg.get("collegamenti") or []}
    for chiave, funzione in (("incendio", incendio_bosco), ("siccita", siccita), ("terremoti", terremoti)):
        if chiave not in cfg:
            continue
        try:
            out[chiave] = funzione(cfg[chiave], prec.get(chiave))
        except Exception as e:
            log(f"Picchetto, {chiave}: errore {str(e)[:120]}")
            out[chiave] = non_disp("errore nella lettura")
    return out


# ---------------------------------------------------------------- punti di riferimento: PRU e impianti
PUNTI_CFG = BASE / "config" / "punti.json"
PRU_STATO = BASE / "archivio" / "stato" / "pru.json"


def _lv95_wgs84(e, n):
    """Coordinate nazionali svizzere LV95 → latitudine e longitudine (formule approssimate di swisstopo, precisione circa 1 m)."""
    y, x = (e - 2600000) / 1e6, (n - 1200000) / 1e6
    lon = 2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x * x - 0.0436 * y ** 3
    lat = 16.9023892 + 3.238272 * x - 0.270978 * y * y - 0.002528 * x * x - 0.0447 * y * y * x - 0.0140 * x ** 3
    return round(lat * 100 / 36, 6), round(lon * 100 / 36, 6)


def punti_riferimento():
    """Punti di riferimento della pagina regionale:
    - PRU (punti di raccolta d'urgenza) dai geodati federali dell'UFPP, riletti una volta al giorno; se la fonte non risponde
      vale l'ultima lettura salvata, e in mancanza l'elenco di riserva di config/punti.json;
    - impianti e infrastruttura dall'elenco di config/punti.json, tenuto a mano.
    I cantieri non passano di qui: le pagine li leggono direttamente dal modulo sul server."""
    if not PUNTI_CFG.exists():
        return None
    cfg = json.loads(PUNTI_CFG.read_text(encoding="utf-8"))
    comuni = {c["nome"].lower(): c["nome"] for c in CFG["comuni"]}
    out = {"impianti": [], "pru": None}
    for i in cfg.get("impianti") or []:
        try:
            out["impianti"].append({"nome": str(i["nome"])[:80], "tipo": str(i.get("tipo") or "impianto")[:30], "comune": i.get("comune"),
                                    "lat": float(i["lat"]), "lon": float(i["lon"]), "nota": str(i.get("nota") or "")[:200]})
        except (KeyError, TypeError, ValueError):
            log(f"Punti: impianto ignorato perché incompleto ({str(i)[:60]})")
    c = cfg.get("pru") or {}
    stato = _leggi_json(PRU_STATO) if PRU_STATO.exists() else None
    if stato and _riusa(stato, c.get("intervallo_minuti", 1440)):
        out["pru"] = stato
        return out
    try:
        js = scarica(c["url"], "json", timeout=60)
        punti = []
        for f in js.get("features") or []:
            pr = f.get("properties") or {}
            comune = comuni.get(str(pr.get("gemeinde") or "").strip().lower())
            if pr.get("kanton") != "TI" or not comune:
                continue
            lat, lon = _lv95_wgs84(float(pr["xkoord"]), float(pr["ykoord"]))
            via = " ".join(str(x) for x in (pr.get("strasse"), pr.get("nummer")) if x not in (None, "", "null"))
            punti.append({"id": pr.get("ntp-id"), "nome": pr.get("gebbezeichnung"), "indirizzo": via, "localita": pr.get("ort"),
                          "comune": comune, "lat": lat, "lon": lon, "nota": pr.get("bemerkungen") or ""})
        if not punti:
            raise ValueError("nessun punto del comprensorio nel file")
        punti.sort(key=lambda x: (x["comune"], str(x["localita"])))
        out["pru"] = {"disponibile": True, "letto": ADESSO.isoformat(timespec="seconds"), "fonte": "UFPP, geodati federali", "punti": punti}
        PRU_STATO.parent.mkdir(parents=True, exist_ok=True)
        PRU_STATO.write_text(json.dumps(out["pru"], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"Punti: {len(punti)} PRU letti dai geodati federali")
    except Exception as e:
        log(f"Punti: PRU non letti ({str(e)[:100]})")
        if stato and stato.get("punti"):
            out["pru"] = {**stato, "nota": "fonte non raggiungibile, vale l'ultima lettura"}
        elif c.get("riserva"):
            out["pru"] = {"disponibile": True, "letto": None, "fonte": "UFPP, elenco di riserva del " + str(c.get("riserva_del", "")),
                          "punti": c["riserva"], "nota": "fonte non raggiungibile, elenco di riserva"}
        else:
            out["pru"] = non_disp(e)
    return out


# ---------------------------------------------------------------- sonda provvisoria (da togliere a indagine conclusa)
SONDA_NAZ = BASE / "archivio" / "stato" / "sonda_naz.json"


def sonda_naz():
    """Indagine una tantum: da quali indirizzi la pagina della CENAL prende i valori della rete NADAM.
    Scrive archivio/stato/sonda_naz.json e poi non viene più eseguita. Non tocca i dati pubblicati."""
    if SONDA_NAZ.exists():
        return
    from urllib.parse import urljoin
    ua = {"User-Agent": "Mozilla/5.0 (ANSIT PCi Lugano; indagine fonte dati)", "Accept-Language": "it"}
    esito = {"eseguita": ADESSO.isoformat(timespec="seconds"), "pagine": [], "script": [], "indirizzi": [], "prove": []}
    chiave = re.compile(r"api|json|csv|nadam|mess|zeit|tages|station|sonde|dose|odl|data|graphql|wp-json|ajax", re.I)
    trovati, da_leggere = set(), []
    import time
    scadenza = time.monotonic() + 90      # la sonda non deve mai rallentare il ciclo oltre un minuto e mezzo

    def raccogli(testo, base):
        for m in re.findall(r"""["'`]((?:https?:)?//[^"'`\s<>]{6,200}|/[A-Za-z0-9_\-./]{3,160}(?:\?[^"'`\s<>]{0,80})?)["'`]""", testo):
            if chiave.search(m) and not re.search(r"\.(png|jpg|jpeg|svg|gif|woff2?|ttf|css|ico)(\?|$)", m, re.I):
                trovati.add(urljoin(base, m))

    for u in ("https://www.naz.ch/it/aktuell/zeitverlauf", "https://www.naz.ch/it/aktuell/messwerte"):
        try:
            r = requests.get(u, headers=ua, timeout=10)
            t = r.text
            esito["pagine"].append({"url": u, "finale": r.url, "stato": r.status_code, "byte": len(r.content), "inizio": t[:1500]})
            for src in re.findall(r"""<script[^>]+src=["']([^"']+)["']""", t) + re.findall(r"""<link[^>]+href=["']([^"']+\.js[^"']*)["']""", t):
                da_leggere.append(urljoin(r.url, src))
            raccogli(t, r.url)
        except Exception as e:
            esito["pagine"].append({"url": u, "errore": str(e)[:150]})
    visti = set()
    while da_leggere and len(visti) < 25 and time.monotonic() < scadenza - 30:
        u = da_leggere.pop(0)
        if u in visti:
            continue
        visti.add(u)
        try:
            r = requests.get(u, headers=ua, timeout=10)
            t = r.text if len(r.content) < 6_000_000 else ""
            esito["script"].append({"url": u, "stato": r.status_code, "byte": len(r.content)})
            raccogli(t, u)
            # moduli caricati a richiesta dallo script principale
            for m in re.findall(r"""["'`]((?:\./|/)?[A-Za-z0-9_\-./]{2,120}\.js)["'`]""", t)[:40]:
                da_leggere.append(urljoin(u, m))
            # contesto attorno alle parole che contano, per capire come si compone la richiesta
            for m in list(re.finditer(r"fetch\(|axios|XMLHttpRequest|zeitverlauf|tagesmittel|nSv", t))[:25]:
                esito.setdefault("contesto", []).append({"script": u.rsplit("/", 1)[-1][:60], "testo": t[max(0, m.start() - 160):m.start() + 260]})
        except Exception as e:
            esito["script"].append({"url": u, "errore": str(e)[:150]})
    esito["indirizzi"] = sorted(trovati)[:400]
    # prova diretta degli indirizzi più promettenti e di alcune ipotesi
    ipotesi = ["https://www.naz.ch/api/", "https://www.naz.ch/api/nadam", "https://www.naz.ch/api/stations", "https://www.naz.ch/data/nadam.json",
               "https://www.naz.ch/it/aktuell/zeitverlauf_LUG.shtml", "https://www.naz.ch/it/aktuell/tagesmittelwerte"]
    candidati = [x for x in esito["indirizzi"] if "naz.ch" in x or "admin.ch" in x][:40] + ipotesi
    for u in candidati:
        if time.monotonic() > scadenza:
            esito["prove"].append({"nota": "tempo esaurito, prove interrotte"})
            break
        try:
            r = requests.get(u, headers=ua, timeout=8)
            esito["prove"].append({"url": u, "stato": r.status_code, "tipo": r.headers.get("Content-Type", "")[:60], "byte": len(r.content), "inizio": r.text[:400]})
        except Exception as e:
            esito["prove"].append({"url": u, "errore": str(e)[:120]})
    esito["contesto"] = (esito.get("contesto") or [])[:60]
    SONDA_NAZ.parent.mkdir(parents=True, exist_ok=True)
    SONDA_NAZ.write_text(json.dumps(esito, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Sonda CENAL: {len(esito['script'])} script letti, {len(esito['indirizzi'])} indirizzi annotati")


# ---------------------------------------------------------------- quadro cantonale
def cantone(dati_regione, ms=None):
    """Produce sito/cantone.json riusando le stesse funzioni, con la configurazione cantonale."""
    cfg_path = BASE / "config" / "cantone.json"
    if not cfg_path.exists():
        return
    C = json.loads(cfg_path.read_text(encoding="utf-8"))
    log("Quadro cantonale: avvio")
    d = {"generato": dati_regione["generato"], "generato_locale": dati_regione["generato_locale"],
         "titolo": C["titolo"], "dati_stantii_minuti": CFG["dati_stantii_minuti"],
         "comprensorio": C["comprensorio"], "regioni": [r["nome"] for r in C["regioni"]]}
    d["allerte"] = {}
    for r in C["regioni"]:
        a = allerte_comune({"nome": r["rappresentante"], "plz": r["plz"]})
        a.update(regione=r["nome"], rappresentante=r["rappresentante"], lat=r["lat"], lon=r["lon"])
        d["allerte"][r["nome"]] = a
    log("Quadro cantonale: allerte per regione lette")
    d["pegel"] = [pegel(st) for st in C["pegel"]]
    log("Quadro cantonale: pegel letti")
    d["misure"] = {st["id"]: misure_smn(st) for st in C["stazioni_meteo"]}
    log("Quadro cantonale: misure lette")
    d["alertswiss"] = dati_regione["alertswiss"]
    try:
        d["valanghe"] = valanghe()
    except Exception as e:
        log(f"Valanghe: errore {str(e)[:120]}")
        d["valanghe"] = non_disp("errore nella lettura del bollettino")
    log("Quadro cantonale: bollettino valanghe letto")
    try:
        d["picchetto"] = picchetto((_leggi_json(BASE / "sito" / "cantone.json") or {}).get("picchetto"))
    except Exception as e:
        log(f"Picchetto: errore {str(e)[:120]}")
        d["picchetto"] = None
    log("Quadro cantonale: fonti del picchetto lette")
    if "oasi" in C and not OASI_BLOCCATO["stato"]:
        d["oasi"] = [oasi(st) for st in C["oasi"]["stazioni"]]
        log("Quadro cantonale: rete OASI letta")
    else:
        d["oasi"] = [{**non_disp("OASI non raggiungibile in questo ciclo"), "nome": st["nome"], "luogo": st["luogo"],
                      "codice": st["codice"], "parametro": st["param"], "bacino": st.get("bacino"), "lat": st.get("lat"), "lon": st.get("lon")}
                     for st in C.get("oasi", {}).get("stazioni", [])]
    out = BASE / "sito" / "cantone.json"
    vecchio = _leggi_json(out)
    prev_vecchie = (vecchio or {}).get("previsioni") or {}
    d["previsioni"], d["passi"] = {}, None
    if ms and ms.get("disponibile"):
        try:
            luce = (dati_regione.get("previsione") or {}).get("luce")
            d["previsioni"] = previsione_ticino({p["nome"]: p for p in C["previsioni"]}, ms, prev_vecchie, luce)
            d["passi"] = {"disponibile": True, "corsa": ms.get("corsa_locale"), "fonte": "MeteoSvizzera, previsione locale", "punti": previsione_passi(ms)}
        except Exception as e:
            log(f"Quadro cantonale: previsione ufficiale non applicata ({str(e)[:120]})")
            d["previsioni"] = {}
    if not d["previsioni"]:      # riserva: Open-Meteo, come prima
        d["previsioni"] = {p["nome"]: previsione(p["lat"], p["lon"], prev_vecchie.get(p["nome"])) for p in C["previsioni"]}
        d["passi"] = non_disp("previsione MeteoSvizzera non disponibile")
    log("Quadro cantonale: previsioni lette")
    d["variazioni"] = variazioni(d, vecchio)
    evidenze(d, vecchio)
    d["precedente"] = vecchio["generato"] if vecchio else None
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    GREZZI["cantone_allerte"] = {k: v for k, v in GREZZI.get("allerte", {}).items()}
    log("Quadro cantonale: scritto")


# ---------------------------------------------------------------- main
def main():
    log("Avvio ciclo")
    dati = {
        "generato": ADESSO.isoformat(timespec="seconds"),
        "generato_locale": ADESSO.astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
        "regione": CFG["regione"], "dati_stantii_minuti": CFG["dati_stantii_minuti"],
        "comuni": [c["nome"] for c in CFG["comuni"]],
    }
    dati["misure"] = {st["id"]: misure_smn(st) for st in CFG["stazioni_meteo"]}
    log("Misure SMN lette")
    dati["allerte"] = {c["nome"]: allerte_comune(c) for c in CFG["comuni"]}
    log("Allerte lette")
    dati["alertswiss"] = alertswiss()
    dati["pegel"] = [pegel(st) for st in CFG["pegel"]]
    log("Pegel letti")
    dati["oasi"] = [oasi(st) for st in CFG["oasi"]["stazioni"]]
    log("Rete cantonale OASI letta")
    sito = BASE / "sito" / "dati.json"
    vecchio = _leggi_json(sito)
    try:
        ms = meteosvizzera_locale(_ms_stato_leggi() or (vecchio or {}).get("meteosvizzera"))
    except Exception as e:
        log(f"MeteoSvizzera: errore {str(e)[:120]}")
        ms = non_disp("errore nella lettura")
    if ms and ms.get("disponibile"):
        _ms_stato_scrivi(ms)
    dati["meteosvizzera"] = _ms_sintesi(ms)
    log("Previsione MeteoSvizzera letta")
    dati["previsione"] = previsione(precedente=(vecchio or {}).get("previsione"), ms=ms)
    if ms and ms.get("disponibile"):
        # riepilogo per Comune (raffiche e pioggia delle prossime 12 e 24 ore)
        nomi = {str(k): v.get("nome") for k, v in (CFG["previsione"]["meteosvizzera"].get("punti") or {}).items()}
        try:
            dati["previsione_comuni"] = {"corsa": ms.get("corsa_locale"),
                                         "punti": {nomi.get(pid, pid): _ms_riepilogo(pt) for pid, pt in ms["punti"].items()
                                                   if pt.get("gruppo", "comuni") == "comuni" or pid == str(CFG["previsione"]["meteosvizzera"].get("punto_principale"))}}
        except Exception as e:
            log(f"MeteoSvizzera: riepilogo per Comune non riuscito ({str(e)[:100]})")
    log("Previsione letta")
    dati["messaggio"] = messaggio()
    try:
        dati["punti"] = punti_riferimento()
    except Exception as e:
        log(f"Punti di riferimento: errore {str(e)[:120]}")
        dati["punti"] = None
    try:
        sonda_naz()
    except Exception as e:
        log(f"Sonda CENAL: errore {str(e)[:120]}")

    dati["variazioni"] = variazioni(dati, vecchio)
    evidenze(dati, vecchio)
    dati["precedente"] = vecchio["generato"] if vecchio else None
    sito.write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")

    try:
        cantone(dati, ms)
    except Exception as e:
        log(f"Quadro cantonale: errore {e}")

    t = ADESSO.astimezone(TZ)
    arch = BASE / "archivio" / t.strftime("%Y/%m/%d")
    arch.mkdir(parents=True, exist_ok=True)
    with gzip.open(arch / f"{t:%H%M}.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"generato": dati["generato"], "normalizzato": dati, "grezzi": GREZZI}, f, ensure_ascii=False)
    log(f"Ciclo concluso. Fonti: " + ", ".join(
        f"{k}={'ok' if _ok(dati, k) else 'KO'}" for k in ("misure", "allerte", "alertswiss", "pegel", "oasi", "previsione")))


def messaggio():
    url = CFG.get("messaggio_url")
    if not url:
        return None
    try:
        return scarica(url, "json", timeout=8)
    except Exception as e:
        log(f"Messaggio in evidenza non letto: {str(e)[:100]}")
        return None


if __name__ == "__main__":
    main()
