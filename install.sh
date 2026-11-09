#!/usr/bin/env bash
# Instala o Arca no Zorin OS / Ubuntu / Debian.   Uso:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

echo "→ Instalando dependências…"
apt-get update -qq
PK=pkexec
apt-cache show pkexec >/dev/null 2>&1 || PK=policykit-1
apt-get install -y python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    librsvg2-common util-linux parted "$PK"

echo "→ Copiando arquivos…"
bash packaging/stage.sh /

gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

echo
echo "✔ Arca instalado. Procure por \"Arca\" no menu do Zorin ou rode: arca"
