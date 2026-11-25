showtime-libmpv is an unofficial fork of GNOME Showtime.
It keeps Showtime's GTK4 and Libadwaita interface and replaces GStreamer with libmpv.
The original authors, credits, history and GPL-3.0-or-later license are retained.

Architecture

Use x86_64 on Intel or AMD 64-bit machines. On ARM64, Flatpak calls the
architecture aarch64. Run this on the machine where you will build or install:

    arch=$(flatpak --default-arch)
    app=com.ivanbelousov.showtime-libmpv

Build

Build natively on an x86_64 or ARM64 Linux machine. Install flatpak-builder,
configure the Flathub remote for your user, and run from the repository root:

    flatpak-builder --user --arch="$arch" --install-deps-from=flathub \
      --repo="/tmp/showtime-libmpv-repo-$arch" "/tmp/showtime-libmpv-build-$arch" \
      "build-aux/flatpak/$app.json"
    flatpak build-bundle --arch="$arch" "/tmp/showtime-libmpv-repo-$arch" \
      "./$app-$arch.flatpak" "$app"

Install

With the Flathub remote configured for the GNOME 51 runtime, install the bundle
that matches your machine:

    flatpak install --user "./$app-$arch.flatpak"
    flatpak run "$app"
