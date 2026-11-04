#!/bin/sh
# Installs or updates habituality on Debian/Ubuntu. Run as root from the unpacked folder:
#   sh install.sh
# Your data in /var/lib/habituality is kept when you run it again to update.
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (in the container console you already are)." >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/habituality
DATA=/var/lib/habituality

for f in server.py index.html common-passwords.txt.gz habituality.service; do
  [ -f "$SRC/$f" ] || { echo "Missing $f next to install.sh" >&2; exit 1; }
done

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9 or newer is needed." >&2; exit 1; }

if ! id habituality >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin habituality
fi

install -d -m 0755 "$APP"
if [ "$SRC" != "$APP" ]; then
  install -m 0644 "$SRC/server.py" "$SRC/index.html" "$SRC/common-passwords.txt.gz" "$APP/"
fi
install -d -m 0750 -o habituality -g habituality "$DATA"
install -m 0644 "$SRC/habituality.service" /etc/systemd/system/habituality.service

cat > /usr/local/bin/habituality <<'EOF'
#!/bin/sh
# Admin commands: habituality invite | users | passwd NAME | bans | unban IP|all | setup-code | backup [DIR]
cd /var/lib/habituality
exec runuser -u habituality -- env HB_DATA=/var/lib/habituality python3 /opt/habituality/server.py "$@"
EOF
chmod 0755 /usr/local/bin/habituality

systemctl daemon-reload
systemctl enable habituality >/dev/null 2>&1
systemctl restart habituality
sleep 2

if systemctl is-active --quiet habituality; then
  echo
  echo "habituality is running on http://127.0.0.1:8080 inside this container."
  if [ -f "$DATA/setup-code.txt" ]; then
    echo "Setup code for the first (owner) account: $(cat "$DATA/setup-code.txt")"
  fi
  echo "Next: give it a private https address in your tailnet:  tailscale serve --bg 8080"
else
  echo "The service did not start. See:  journalctl -u habituality -n 50" >&2
  exit 1
fi
