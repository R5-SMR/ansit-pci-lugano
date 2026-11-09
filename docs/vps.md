# Server ansit-srv1 (Infomaniak VPS Lite) – ponte OASI

**Cosa fa**: ogni 10 minuti scarica da OASI i file CSV delle stazioni cantonali configurate in `config/config.json` e `config/cantone.json` (sezione `oasi`) e li pubblica su `http://<IP>/oasi/`. Il collettore su GitHub Actions li legge da lì, perché OASI non accetta connessioni dai server di GitHub (esteri).

**Dove**: Infomaniak, VPS Lite «ansit-srv1», Ubuntu 24.04 LTS, 1 CPU / 2 GB / 20 GB, CHF 3.–/mese. Account Infomaniak personale di C. Albeverio (fase di test); da trasferire al Consorzio quando il progetto si consolida (Infomaniak: «Trasferimento di prodotti»).

**Accesso**: SSH con chiave `ansit-srv1` (file privato sul PC di lavoro di C. Albeverio e nel gestore password del Consorzio). Nessun accesso con password.

**Componenti sul server**
- `/opt/ansit/ponte_oasi.py` – lo script (copia di `ponte/ponte_oasi.py` nel repository)
- `ansit-ponte.timer` / `.service` – esecuzione ogni 10 minuti (systemd)
- nginx – serve `/var/www/html/oasi/` in sola lettura (porta 80)
- ufw – firewall: aperte solo SSH (22) e HTTP (80)
- unattended-upgrades – aggiornamenti di sicurezza automatici

**Comandi utili** (dopo `ssh -i <chiave> ubuntu@<IP>`)
- stato ultimo giro: `cat /var/www/html/oasi/stato.json`
- log: `journalctl -u ansit-ponte.service -n 50`
- esecuzione manuale: `sudo systemctl start ansit-ponte.service`
- aggiornare lo script dopo una modifica nel repository: `sudo curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/ponte_oasi.py -o /opt/ansit/ponte_oasi.py`
- reinstallazione completa: `curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/installa.sh | sudo bash`

**Collegamento con il collettore**: `config/config.json → oasi.ponte_url` contiene `http://<IP>/oasi/`. Il collettore prova prima il ponte; se non risponde, prova OASI direttamente.

**Se OASI un giorno autorizza l'accesso diretto**: svuotare `ponte_url` nel config; il server può essere spento.

**Prospettiva**: lo stesso server può ospitare l'intero collettore (cron ogni 10 min + push su GitHub), eliminando la dipendenza da GitHub Actions e cron-job.org.
