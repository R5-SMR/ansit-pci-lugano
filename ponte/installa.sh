#!/usr/bin/env bash
# Installazione del ponte OASI su Ubuntu 24.04 (ansit-srv1). Eseguire come root (sudo).
# Uso:  curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/installa.sh | sudo bash
set -euo pipefail
echo "== ANSIT – installazione del ponte OASI =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx python3 python3-requests curl ufw
# firewall: solo SSH e HTTP
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw --force enable >/dev/null
# script
install -d /opt/ansit /var/www/html/oasi
curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/ponte_oasi.py -o /opt/ansit/ponte_oasi.py
chmod 755 /opt/ansit/ponte_oasi.py
# pagina indice minimale e CORS (per eventuale lettura diretta dal browser)
cat > /etc/nginx/sites-available/default <<'NG'
server {
    listen 80 default_server;
    root /var/www/html;
    location /oasi/ {
        add_header Access-Control-Allow-Origin *;
        add_header Cache-Control "no-cache";
        autoindex on;
    }
    location = / { return 200 "ANSIT ponte OASI - Consorzio PCi Regione Lugano Citta\n"; add_header Content-Type text/plain; }
}
NG
systemctl reload nginx
# timer systemd: ogni 10 minuti
cat > /etc/systemd/system/ansit-ponte.service <<'SV'
[Unit]
Description=ANSIT ponte OASI (scarica i CSV delle stazioni cantonali)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/ansit/ponte_oasi.py
SV
cat > /etc/systemd/system/ansit-ponte.timer <<'TM'
[Unit]
Description=ANSIT ponte OASI ogni 10 minuti
[Timer]
OnBootSec=1min
OnCalendar=*:0/10
Persistent=true
[Install]
WantedBy=timers.target
TM
systemctl daemon-reload
systemctl enable --now ansit-ponte.timer
# aggiornamenti di sicurezza automatici
apt-get install -y -q unattended-upgrades >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades
# primo giro subito
systemctl start ansit-ponte.service || true
echo
echo "== Installazione conclusa =="
echo "Stato ultimo giro:"; cat /var/www/html/oasi/stato.json 2>/dev/null || echo "(non ancora disponibile)"
echo
echo "Verifica dal browser: http://$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/oasi/"
echo "Log:  journalctl -u ansit-ponte.service -n 50"
