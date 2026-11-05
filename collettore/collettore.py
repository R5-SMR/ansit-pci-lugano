#!/usr/bin/env python3
"""
Collettore del quadro di situazione – Consorzio PCi Regione Lugano Città.

Per ogni ciclo:
  1. interroga le fonti (MeteoSvizzera, Alertswiss, UFAM, Open-Meteo);
  2. salva le risposte grezze in archivio/AAAA/MM/GG/HHMM.json.gz;
  3. produce sito/dati.json (normalizzato) per il cockpit;
  4. confronta con il ciclo precedente e scrive la lista delle variazioni.

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
    print(f"[{ADESSO.astimezone(TZ):%H:%M:%S}] {msg}", flush=True)


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
    return {
        "disponibile": True, "stazione": st["id"], "nome": st["nome"],
        "rilevamento": ultima.get(col_ts), "temperatura": num(ultima, "temperatura"),
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


# ---------------------------------------------------------------- UFAM: pegel
def pegel(st):
    valore, serie, usato = None, [], None
    for tpl in CFG["ufam_url_candidati"]:
        url = tpl.format(id=st["id"])
        try:
            js = scarica(url, "json")
        except Exception as e:
            log(f"UFAM {st['id']}: {url} non risponde ({e})")
            continue
        GREZZI.setdefault("ufam", {})[st["id"]] = js
        valore, serie = _estrai_ufam(js, st["param"])
        if valore is not None:
            usato = url
            break
    if valore is None:
        return {**non_disp("valore non trovato nelle risposte"), "id": st["id"],
                "nome": st["nome"], "luogo": st["luogo"]}
    livello = 1
    for liv, soglia in sorted(st["soglie"].items(), key=lambda kv: int(kv[0])):
        if valore >= soglia:
            livello = int(liv)
    trend = None
    if len(serie) >= 2:
        d = serie[-1] - serie[0]
        trend = "in aumento" if d > 0 else "in calo" if d < 0 else "stabile"
    return {"disponibile": True, "id": st["id"], "nome": st["nome"], "luogo": st["luogo"],
            "tipo": st["tipo"], "parametro": st["param"], "unita": st["unita"],
            "valore": round(valore, 2), "livello": livello, "soglie": st["soglie"],
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
                    return float(y[-1]), y[-48:]
    except (KeyError, TypeError):
        pass
    # formato geo.admin identify: {"feature": {"attributes": {...}}}
    try:
        attr = js["feature"]["attributes"]
        for k, v in attr.items():
            if isinstance(v, (int, float)) and ("value" in k.lower() or "wert" in k.lower()):
                return float(v), []
    except (KeyError, TypeError):
        pass
    return None, []


# ---------------------------------------------------------------- OASI: rete cantonale
TZ_OASI = timezone(timedelta(hours=1))   # OASI usa GMT+1 fisso, senza ora legale


OASI_BLOCCATO = {"stato": False, "motivo": ""}


def oasi(st):
    cfg = CFG["oasi"]
    if OASI_BLOCCATO["stato"]:
        return {**non_disp("OASI non raggiungibile in questo ciclo: " + OASI_BLOCCATO["motivo"]),
                "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"]}
    oggi = ADESSO.astimezone(TZ_OASI).date()
    url = cfg["url"].format(domain=st["dominio"], param=st["param"], codice=quote(st["codice"]),
                            da=(oggi - timedelta(days=1)).isoformat(), a=oggi.isoformat())
    try:
        testo = scarica(url, timeout=8)
    except requests.exceptions.ConnectionError as e:
        # errore di rete (DNS, rifiuto, timeout di connessione): inutile insistere sulle altre stazioni
        OASI_BLOCCATO.update(stato=True, motivo=str(e)[:120])
        log(f"OASI: connessione fallita, salto le stazioni successive ({str(e)[:120]})")
        return {**non_disp(e), "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"]}
    except Exception as e:
        log(f"OASI {st['codice']}: {str(e)[:160]}")
        return {**non_disp(e), "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"]}
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
        return {**non_disp("nessun valore nel periodo"), "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"]}
    t_ult, v_ult, provv = punti[-1]
    eta = int((ADESSO - t_ult).total_seconds() // 60)
    base = {"disponibile": True, "nome": st["nome"], "luogo": st["luogo"], "codice": st["codice"],
            "bacino": st.get("bacino"), "parametro": st["param"], "unita": st["unita"],
            "rilevamento": t_ult.astimezone(TZ).strftime("%d.%m. %H:%M"), "eta_minuti": eta,
            "provvisorio": provv, "proprietario": st.get("proprietario", cfg["proprietario"])}
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


# ---------------------------------------------------------------- Open-Meteo: previsione
def previsione():
    p = CFG["previsione"]
    url = p["url"].format(lat=p["lat"], lon=p["lon"])
    try:
        js = scarica(url, "json")
    except Exception as e:
        return non_disp(e)
    GREZZI["open_meteo"] = js
    h = js.get("hourly", {})
    ore = h.get("time", [])
    adesso_loc = ADESSO.astimezone(TZ).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")
    i0 = next((i for i, t in enumerate(ore) if t >= adesso_loc), 0)
    orario = []
    for i in range(i0, min(i0 + 12, len(ore))):
        orario.append({
            "ora": ore[i][11:16], "temperatura": h["temperature_2m"][i],
            "umidita": h["relative_humidity_2m"][i], "pioggia_mm": h["precipitation"][i],
            "prob_pioggia": h["precipitation_probability"][i], "vento_kmh": h["wind_speed_10m"][i],
            "raffica_kmh": h["wind_gusts_10m"][i], "codice": h["weather_code"][i],
        })
    d = js.get("daily", {})
    giorni = [{"data": d["time"][i], "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
               "pioggia_mm": d["precipitation_sum"][i], "raffica_max": d["wind_gusts_10m_max"][i],
               "codice": d["weather_code"][i]} for i in range(len(d.get("time", [])))]
    massimi = {
        "raffica_kmh": max((o["raffica_kmh"] for o in orario), default=None),
        "pioggia_oraria_mm": max((o["pioggia_mm"] for o in orario), default=None),
        "pioggia_totale_mm": round(sum(o["pioggia_mm"] for o in orario), 1) if orario else None,
        "temperatura": max((o["temperatura"] for o in orario), default=None),
    }
    return {"disponibile": True, "fonte": "Open-Meteo (non ufficiale)", "orario": orario,
            "giorni": giorni, "massimi_12h": massimi}


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
        if p.get("disponibile") and p["id"] in vp and p["livello"] != vp[p["id"]]["livello"]:
            out.append({"testo": f"{p['nome']} ({p['luogo']}): livello da {vp[p['id']]['livello']} a {p['livello']}",
                        "tipo": "peggiora" if p["livello"] > vp[p["id"]]["livello"] else "migliora"})
    vs, ns = vecchio.get("alertswiss", {}), nuovo.get("alertswiss", {})
    if vs.get("disponibile") and ns.get("disponibile"):
        vt = {v["titolo"] for v in vs["notifiche"]}
        for v in ns["notifiche"]:
            if v["titolo"] not in vt:
                out.append({"testo": f"Nuova notifica Alertswiss: {v['titolo']}", "tipo": "peggiora"})
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
    dati["previsione"] = previsione()
    log("Previsione letta")

    sito = BASE / "sito" / "dati.json"
    vecchio = json.loads(sito.read_text(encoding="utf-8")) if sito.exists() else None
    dati["variazioni"] = variazioni(dati, vecchio)
    dati["precedente"] = vecchio["generato"] if vecchio else None
    sito.write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")

    t = ADESSO.astimezone(TZ)
    arch = BASE / "archivio" / t.strftime("%Y/%m/%d")
    arch.mkdir(parents=True, exist_ok=True)
    with gzip.open(arch / f"{t:%H%M}.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"generato": dati["generato"], "normalizzato": dati, "grezzi": GREZZI}, f, ensure_ascii=False)
    log(f"Ciclo concluso. Fonti: " + ", ".join(
        f"{k}={'ok' if _ok(dati, k) else 'KO'}" for k in ("misure", "allerte", "alertswiss", "pegel", "oasi", "previsione")))


if __name__ == "__main__":
    main()
