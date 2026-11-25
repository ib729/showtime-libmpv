subdir('gtk')

gnome.compile_resources(
    'showtime',
    configure_file(
        input: 'showtime.gresource.xml.in',
        output: 'showtime.gresource.xml',
        configuration: conf,
    ),
    gresource_bundle: true,
    install: true,
    install_dir: pkgdatadir,
    dependencies: [blueprints, appstream_file],
)

configure_file(
    input: 'showtime.in',
    output: bin_name,
    configuration: conf,
    install: true,
    install_dir: get_option('bindir'),
    install_mode: 'rwxr-xr-x',
)

showtime_widgets_sources = [
    'widgets/__init__.py',
    'widgets/drag_overlay.py',
    'widgets/options.py',
    'widgets/sound_options.py',
    'widgets/window.py',
]

showtime_util_sources = [
    'util/__init__.py',
    'util/connection.py',
    'util/source.py',
]

showtime_sources = [
    configure_file(
        input: '__init__.py.in',
        output: '__init__.py',
        configuration: conf,
    ),
    'main.py',
    'mpris.py',
    'play.py',
    'utils.py',
]

install_data(showtime_widgets_sources, install_dir: pkgdatadir / 'showtime/widgets')
install_data(showtime_util_sources, install_dir: pkgdatadir / 'showtime/util')
install_data(showtime_sources, install_dir: pkgdatadir / 'showtime')
