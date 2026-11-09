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
Description: Resgate setor a setor e clonagem de cartões CompactFlash
 Copia cartões CF desgastados para uma imagem, lendo primeiro as estruturas
 de boot, isolando setores ruins em várias passadas (estilo ddrescue) e
 mostrando o mapa de setores ao vivo. Depois grava a imagem num cartão novo
 e verifica a cópia por SHA-256.
EOF

cat > "$BUILD/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
gtk-update-icon-cache -q -f /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true
EOF
cp "$BUILD/DEBIAN/postinst" "$BUILD/DEBIAN/postrm"
chmod 755 "$BUILD/DEBIAN/postinst" "$BUILD/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$BUILD" "dist/$PKG.deb"
echo "✔ Pacote gerado: dist/$PKG.deb"
echo "  Instale com:  sudo apt install ./dist/$PKG.deb"
