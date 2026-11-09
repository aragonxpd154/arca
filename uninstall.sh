#!/usr/bin/env bash
# Remove a Arca (as imagens em ~/Arca não são apagadas).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || exec sudo "$0" "$@"
if [ -x /usr/libexec/arca/atalho-desktop.sh ]; then sh /usr/libexec/arca/atalho-desktop.sh remover || true; fi
rm -rf /usr/lib/arca /usr/libexec/arca /usr/share/doc/arca
rm -f /usr/bin/arca /usr/share/applications/io.github.aragonxpd154.Arca.desktop \
      /usr/share/polkit-1/actions/io.github.aragonxpd154.arca.policy \
      /usr/share/icons/hicolor/scalable/apps/io.github.aragonxpd154.Arca.svg \
      /usr/share/icons/hicolor/scalable/apps/arca-*-symbolic.svg \
      /usr/share/icons/hicolor/*/apps/io.github.aragonxpd154.Arca.png
gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
echo "✔ Arca removido."
