#!/usr/bin/env bash
# Copia os arquivos do Arca para DESTDIR (/ para instalar, ou uma pasta para montar o .deb).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?uso: stage.sh DESTDIR}"

install -d "$DEST/usr/lib/arca/arca" "$DEST/usr/bin" "$DEST/usr/libexec/arca" \
           "$DEST/usr/share/applications" "$DEST/usr/share/polkit-1/actions" \
           "$DEST/usr/share/icons/hicolor/scalable/apps" "$DEST/usr/share/doc/arca"

install -m 644 "$ROOT"/arca/*.py "$DEST/usr/lib/arca/arca/"
install -m 755 "$ROOT/packaging/arca" "$DEST/usr/bin/arca"
install -m 755 "$ROOT/packaging/arca-worker" "$DEST/usr/libexec/arca/arca-worker"
install -m 644 "$ROOT/packaging/io.github.aragonxpd154.Arca.desktop" "$DEST/usr/share/applications/"
install -m 644 "$ROOT/packaging/io.github.aragonxpd154.arca.policy" "$DEST/usr/share/polkit-1/actions/"
install -m 644 "$ROOT"/data/icons/hicolor/scalable/apps/*.svg "$DEST/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/README.md" "$ROOT/LICENSE" "$DEST/usr/share/doc/arca/"
