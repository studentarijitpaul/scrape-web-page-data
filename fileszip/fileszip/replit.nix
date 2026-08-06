{pkgs}: {
  deps = [
    pkgs.chromium
    pkgs.alsa-lib
    pkgs.cairo
    pkgs.pango
    pkgs.libxkbcommon
    pkgs.xorg.libxcb
    pkgs.mesa
    pkgs.xorg.libXfixes
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.expat
    pkgs.libdrm
    pkgs.cups
    pkgs.at-spi2-atk
    pkgs.atk
    pkgs.dbus
    pkgs.nspr
    pkgs.nss
    pkgs.unzip
  ];
}
