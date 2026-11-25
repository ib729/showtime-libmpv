showtime-libmpv is an unofficial fork of [GNOME Showtime](https://apps.gnome.org/Showtime/).
It keeps Showtime's GTK4 and Libadwaita interface and replaces GStreamer with libmpv.
The original authors, credits, history and GPL-3.0-or-later license are retained.

## Install

```sh
flatpak install --user ./com.ivanbelousov.showtime-libmpv.flatpak
flatpak run com.ivanbelousov.showtime-libmpv
```

To build the bundle yourself, install `flatpak-builder` and configure the Flathub remote:

```sh
flatpak-builder --user --install-deps-from=flathub --repo=/tmp/showtime-libmpv-repo \
  /tmp/showtime-libmpv-build build-aux/flatpak/com.ivanbelousov.showtime-libmpv.json
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
  /tmp/showtime-libmpv-repo ./com.ivanbelousov.showtime-libmpv.flatpak \
  com.ivanbelousov.showtime-libmpv
```
