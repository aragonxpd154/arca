#!/usr/bin/env bash
# Gera o pacote .deb em dist/.   Uso:  ./build-deb.sh
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(python3 -c 'import arca; print(arca.VERSION)')"
PKG="arca_${VERSION}_all"
BUILD="build/$PKG"
rm -rf "$BUILD" && mkdir -p "$BUILD/DEBIAN" dist

bash packaging/stage.sh "$BUILD"
find "$BUILD" -name "__pycache__" -prune -exec rm -rf {} +

SIZE=$(du -sk "$BUILD" | cut -f1)
cat > "$BUILD/DEBIAN/control" <<EOF
Package: arca
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1, librsvg2-common, util-linux, parted, pkexec | policykit-1
Maintainer: Marcos Santos <marcos.silvadeveloper@gmail.com>
Homepage: https://github.com/aragonxpd154/arca
Description: Resgate, clonagem e diagnóstico de cartões CompactFlash
 Copia cartões CF desgastados para uma imagem, lendo primeiro as estruturas
 de boot, isolando setores ruins em várias passadas (estilo ddrescue) e
 mostrando o mapa de setores ao vivo. Grava a imagem num cartão novo e
 verifica a cópia por SHA-256. O diagnóstico testa leitura e escrita do
 cartão inteiro, mede a latência de cada bloco, detecta corrupção, leitura
 instável e capacidade falsa, e acompanha a degradação entre testes.
EOF

cat > "$BUILD/DEBIAN/postinst" <<'POS'
#!/bin/sh
set -e
gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
if [ "$1" = configure ]; then
    sh /usr/libexec/arca/atalho-desktop.sh criar || true
fi
POS
cat > "$BUILD/DEBIAN/prerm" <<'POS'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = purge ]; then
    sh /usr/libexec/arca/atalho-desktop.sh remover || true
fi
POS
cat > "$BUILD/DEBIAN/postrm" <<'POS'
#!/bin/sh
set -e
gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
POS
chmod 755 "$BUILD/DEBIAN/postinst" "$BUILD/DEBIAN/prerm" "$BUILD/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$BUILD" "dist/$PKG.deb"
echo "✔ Pacote gerado: dist/$PKG.deb"
echo "  Instale com:  sudo apt install ./dist/$PKG.deb"
