#!/bin/sh
# Cria (ou remove) o atalho da Arca, com o logo, na Área de Trabalho.
#   atalho-desktop.sh criar [usuario]
#   atalho-desktop.sh remover
# Sem usuário: usa quem chamou o sudo/pkexec; se não souber, todos os usuários
# comuns que tenham pasta de Área de Trabalho.
ACAO="${1:-criar}"
ORIGEM=/usr/share/applications/io.github.aragonxpd154.Arca.desktop
NOME=io.github.aragonxpd154.Arca.desktop

usuarios() {
  if [ -n "${2:-}" ]; then echo "$2"; return; fi
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then echo "$SUDO_USER"; return; fi
  if [ -n "${PKEXEC_UID:-}" ]; then getent passwd "$PKEXEC_UID" | cut -d: -f1; return; fi
  getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\/home\// {print $1}'
}

pasta_desktop() {
  u="$1"; casa="$(getent passwd "$u" | cut -d: -f6)"
  d="$(runuser -u "$u" -- env HOME="$casa" xdg-user-dir DESKTOP 2>/dev/null)"
  if [ -z "$d" ] || [ "$d" = "$casa" ]; then
    for c in "$casa/Área de Trabalho" "$casa/Desktop"; do [ -d "$c" ] && d="$c" && break; done
  fi
  [ -n "$d" ] && [ "$d" != "$casa" ] && echo "$d"
}

for u in $(usuarios "$@"); do
  d="$(pasta_desktop "$u")" || continue
  [ -n "$d" ] || continue
  alvo="$d/$NOME"
  if [ "$ACAO" = remover ]; then
    grep -q "^Exec=arca" "$alvo" 2>/dev/null && rm -f "$alvo"
    continue
  fi
  [ -d "$d" ] && [ -f "$ORIGEM" ] || continue
  cp "$ORIGEM" "$alvo"
  chown "$u:$(id -gn "$u")" "$alvo"
  chmod 755 "$alvo"
  # marca como confiável para o Zorin/GNOME abrir sem perguntar "Permitir execução"
  uid="$(id -u "$u")"
  if [ -S "/run/user/$uid/bus" ]; then
    runuser -u "$u" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      gio set "$alvo" metadata::trusted true 2>/dev/null || true
  fi
  echo "Atalho criado em: $alvo"
done
exit 0
