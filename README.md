# Quadro della situazione – meteo e pericoli naturali (ANSIT) – PCi Regione Lugano Città

Prototipo interno a scopo di istruzione (corsi di stato maggiore) e, in prospettiva, di impiego.
Non è un canale di allerta: fanno fede MeteoSvizzera, il Portale pericoli naturali della Confederazione, Alertswiss e l'UFAM.

## Come funziona

```
config/config.json        Comuni, stazioni, soglie, endpoint   → l'unico file da adattare
collettore/collettore.py  Interroga le fonti, archivia, normalizza, calcola le variazioni
sito/index.html           Cockpit statico, legge sito/dati.json
sito/dati.json            Prodotto a ogni ciclo dal collettore
archivio/AAAA/MM/GG/      Snapshot compressi di ogni ciclo (dati grezzi + normalizzati), per rigiocare eventi nei corsi
.github/workflows/        Ciclo ogni 10 minuti su GitHub Actions, pubblicazione su GitHub Pages
```

Regola di base: nessuno stato «verde» senza dato fresco. Una fonte che non risponde produce `disponibile: false` e il cockpit mostra «n/d».

## Installazione (circa 20 minuti)

1. Creare un account GitHub per il Consorzio (o un'organizzazione) e un repository **pubblico**, ad esempio `lagebild-pci-lugano`.
2. Caricare tutti i file di questa cartella nel ramo `main` (via web: *Add file → Upload files*, oppure con git).
3. In *Settings → Pages*: alla voce *Source* scegliere **GitHub Actions**.
4. In *Settings → Actions → General*: alla voce *Workflow permissions* scegliere **Read and write permissions** e salvare.
5. In *Actions* aprire il workflow «Quadro di situazione» e premere **Run workflow**. Il primo ciclo dura 1–2 minuti.
6. Il cockpit è raggiungibile su `https://<account>.github.io/lagebild-pci-lugano/`.

Il ciclo parte poi da solo ogni 10 minuti. In *Actions* ogni esecuzione mostra il log del collettore; l'ultima riga riassume quali fonti hanno risposto (`ok`/`KO`).

## Prima verifica: i punti marcati DA_VERIFICARE

Il collettore è scritto senza aver potuto interrogare le fonti reali. Al primo avvio, aprire il log e controllare:

| Fonte | Cosa controllare | Dove intervenire |
|---|---|---|
| Misure MeteoSvizzera (OGD, ufficiale) | che il CSV di `LUG` venga letto e i parametri abbiano valori sensati; unità del vento (km/h o m/s) nei metadati `ogd-smn_meta_parameters.csv` | `config.json → meteosvizzera_smn_url`, `collettore.py → PARAM` |
| Allerte per Comune | risponde il backend dell'app MeteoSvizzera (`plzDetail`)? la struttura `warnings` corrisponde? il numero PLZ di ogni Comune è corretto? | `config.json → allerte_meteosvizzera`, `comuni` |
| Alertswiss | quale URL del feed risponde; se nessuno, cercare il link RSS su alert.swiss e inserirlo | `config.json → alertswiss.url_candidati` |
| UFAM pegel | id delle stazioni (Cassarate 2321 è certo; Vedeggio e Lago da confermare su hydrodaten.admin.ch) e **soglie ufficiali dei livelli di pericolo** per stazione; formato della risposta JSON | `config.json → pegel`, `collettore.py → _estrai_ufam` |
| Open-Meteo | nulla da verificare, API stabile | – |

Dopo il primo ciclo, nel file `archivio/.../HHMM.json.gz` ci sono le risposte grezze di ogni fonte: è il materiale per correggere il parsing.

## Fonti e licenze

- MeteoSvizzera open data: uso libero con citazione della fonte («Fonte: MeteoSvizzera»).
- UFAM Hydrodaten: uso libero con citazione della fonte.
- Alertswiss: informazione pubblica della Confederazione.
- Open-Meteo: licenza CC BY 4.0, servizio non ufficiale usato per la previsione puntuale.
- Backend dell'app MeteoSvizzera: pubblico ma non documentato; da sostituire con l'API OGD delle allerte appena disponibile.

## Manutenzione

- GitHub disattiva le pianificazioni dopo 60 giorni senza attività umana sul repository: un commit ogni due mesi è sufficiente. Mettere in agenda.
- L'archivio cresce di qualche decina di MB al mese. Quando supera alcune centinaia di MB, consolidare i file in uno per giorno (azione da aggiungere in fase 2).
- Responsabile tecnico e sostituto: da nominare prima dell'uso in un impiego reale.

## Migrazione al server del Consorzio

Lo stesso `collettore.py` lanciato da cron ogni 10 minuti e la cartella `sito/` servita da un web server statico. Nessuna modifica al codice.

## Fase 2 (dopo il primo corso)

Soglie locali concordate con l'Ufficio dei pericoli naturali, stazioni cantonali OASI, bollettino mattutino via e-mail ai quadri, modalità «rigioca evento» per le esercitazioni, consolidamento archivio, versione semplificata per la popolazione sul sito del Consorzio.
