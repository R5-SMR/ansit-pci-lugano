#!/usr/bin/env bash
# Aggiunge al server ansit-srv1 il modulo «cantieri», protetto dalla stessa utenza del messaggio in evidenza.
# Richiede che installa_messaggio.sh sia già stato eseguito (HTTPS e password). Eseguire come root.
# Uso:  curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/installa_cantieri.sh -o ic.sh && sudo bash ic.sh
set -euo pipefail
NG=/etc/nginx/sites-available/default
echo "== ANSIT – modulo cantieri =="
[ -f /etc/nginx/.htpasswd ] || { echo "ERRORE: manca /etc/nginx/.htpasswd. Eseguire prima installa_messaggio.sh."; exit 1; }
grep -q "location /messaggio/" "$NG" || { echo "ERRORE: nella configurazione di nginx non trovo il modulo del messaggio. Nulla è stato modificato."; exit 1; }

mkdir -p /opt/ansit
curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/cantieri_app.py -o /opt/ansit/cantieri_app.py
chmod 755 /opt/ansit/cantieri_app.py
python3 -m py_compile /opt/ansit/cantieri_app.py
cat > /etc/systemd/system/ansit-cantieri.service <<'SV'
[Unit]
Description=ANSIT modulo cantieri
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/ansit/cantieri_app.py
Restart=always
User=www-data
[Install]
WantedBy=multi-user.target
SV
[ -f /var/www/html/cantieri.json ] || echo '{"cantieri": []}' > /var/www/html/cantieri.json
chown www-data:www-data /var/www/html/cantieri.json /var/www/html
systemctl daemon-reload
systemctl enable ansit-cantieri.service >/dev/null 2>&1
systemctl restart ansit-cantieri.service

if grep -q "location /cantieri/" "$NG"; then
  echo "nginx: il modulo cantieri è già configurato, lascio la configurazione com'è."
else
  COPIA="$NG.prima-dei-cantieri.$(date +%Y%m%d%H%M%S)"
  cp "$NG" "$COPIA"
  # stesse regole del messaggio: il file dei dati è pubblico in sola lettura, il modulo è protetto da password
  sed -i '/location = \/messaggio.json/a\    location = /cantieri.json { add_header Access-Control-Allow-Origin *; add_header Cache-Control "no-cache"; }' "$NG"
  sed -i '/location \/messaggio\/ /a\    location /cantieri/ { auth_basic "ANSIT - cantieri"; auth_basic_user_file /etc/nginx/.htpasswd; proxy_pass http://127.0.0.1:8082/; }' "$NG"
  if nginx -t 2>/dev/null; then
    systemctl reload nginx
    echo "nginx: configurazione aggiornata (copia della precedente in $COPIA)."
  else
    cp "$COPIA" "$NG"; nginx -t && systemctl reload nginx
    echo "ERRORE: la nuova configurazione di nginx non era valida ed è stata ripristinata quella precedente. Il modulo non è raggiungibile."
    exit 1
  fi
fi
sleep 1
HOST=$(grep -m1 -o '[0-9-]*\.sslip\.io' "$NG" || true)
STATO=$(systemctl is-active ansit-cantieri.service || true)
CODICE=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/cantieri/" || true)
DATI=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/cantieri.json" || true)
echo
echo "== Concluso =="
echo "Servizio: $STATO (atteso: active)"
echo "Modulo:   https://$HOST/cantieri/      risposta $CODICE (atteso: 401, cioè chiede la password)"
echo "Dati:     https://$HOST/cantieri.json  risposta $DATI (atteso: 200)"
echo "Accesso con la stessa utenza e password del messaggio in evidenza."
