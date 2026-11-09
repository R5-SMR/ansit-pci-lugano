#!/usr/bin/env bash
# Aggiunge al server ansit-srv1: HTTPS (Let's Encrypt, nome 179-237-106-60.sslip.io) e il modulo
# «messaggio in evidenza» protetto da password condivisa. Eseguire come root.
# Uso:  curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/installa_messaggio.sh -o im.sh && sudo bash im.sh
set -euo pipefail
IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"
echo "== ANSIT – HTTPS e messaggio in evidenza su $HOST =="
read -rp "Nome utente per il modulo (es. quadri): " UTENTE
read -rsp "Password condivisa: " PW; echo
apt-get install -y -q certbot python3-certbot-nginx apache2-utils >/dev/null
ufw allow 443/tcp >/dev/null
htpasswd -bc /etc/nginx/.htpasswd "$UTENTE" "$PW"
curl -fsSL https://raw.githubusercontent.com/R5-SMR/ansit-pci-lugano/main/ponte/messaggio_app.py -o /opt/ansit/messaggio_app.py
chmod 755 /opt/ansit/messaggio_app.py
cat > /etc/systemd/system/ansit-messaggio.service <<'SV'
[Unit]
Description=ANSIT modulo messaggio in evidenza
After=network.target
[Service]
ExecStart=/usr/bin/python3 /opt/ansit/messaggio_app.py
Restart=always
User=www-data
[Install]
WantedBy=multi-user.target
SV
touch /var/www/html/messaggio.json; chown www-data:www-data /var/www/html/messaggio.json /var/www/html
systemctl daemon-reload; systemctl enable --now ansit-messaggio.service
cat > /etc/nginx/sites-available/default <<NG
server {
    listen 80 default_server;
    server_name $HOST _;
    root /var/www/html;
    location /oasi/ { add_header Access-Control-Allow-Origin *; add_header Cache-Control "no-cache"; autoindex on; }
    location = /messaggio.json { add_header Access-Control-Allow-Origin *; add_header Cache-Control "no-cache"; }
    location /messaggio/ { auth_basic "ANSIT - stato maggiore"; auth_basic_user_file /etc/nginx/.htpasswd; proxy_pass http://127.0.0.1:8081/; }
    location = / { return 200 "ANSIT ponte - Consorzio PCi Regione Lugano Citta\n"; add_header Content-Type text/plain; }
}
NG
nginx -t && systemctl reload nginx
certbot --nginx -d "$HOST" --non-interactive --agree-tos --register-unsafely-without-email --redirect
systemctl reload nginx
echo
echo "== Concluso =="
echo "Modulo messaggio:  https://$HOST/messaggio/   (utente: $UTENTE)"
echo "File letto dal cockpit:  https://$HOST/messaggio.json"
echo "Ponte OASI anche in https:  https://$HOST/oasi/"
