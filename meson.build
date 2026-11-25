project(
    'showtime',
    version: '52.alpha',
    meson_version: '>= 1.8.0',
    default_options: [
        'warning_level=2',
        'werror=false',
    ],
)

dependency('gtk4', version: '>= 4.18.0')
dependency('libadwaita-1', version: '>= 1.8.alpha')
dependency('blueprint-compiler', version: '>= 0.17')

i18n = import('i18n')
gnome = import('gnome')
python = import('python')

py_installation = python.find_installation('python3')
mpv_check = run_command(
    py_installation, '-c',
    'from mpv import MPV_VERSION, MpvGlGetProcAddressFn, MpvRenderContext; assert MPV_VERSION >= (2, 3)',
    check: false,
)
if mpv_check.returncode() != 0
    error('Showtime requires python-mpv and libmpv >= 0.38: ' + mpv_check.stderr().strip())
endif

test_env = environment()
test_env.prepend('PYTHONPATH', meson.current_source_dir() / 'showtime')
test('libmpv playback', py_installation, args: ['-B', files('tests/test_playback.py')], env: test_env)

python_dir = join_paths(get_option('prefix'), py_installation.get_install_dir())
pkgdatadir = get_option('prefix') / get_option('datadir') / meson.project_name()

profile = get_option('profile')
if profile == 'development'
    app_id = 'org.gnome.Showtime.Devel'
    prefix = '/org/gnome/Showtime/Devel'
elif profile == 'release'
    app_id = 'org.gnome.Showtime'
    prefix = '/org/gnome/Showtime'
endif

bin_name = 'showtime'

conf = configuration_data()
python_exec = get_option('python_exec')
if python_exec == ''
    conf.set('PYTHON', py_installation.full_path())
else
    conf.set('PYTHON', python_exec)
endif
conf.set('VERSION', meson.project_version())
conf.set('APP_ID', app_id)
conf.set('BIN_NAME', bin_name)
conf.set('PREFIX', prefix)
conf.set('PROFILE', profile)
conf.set('localedir', get_option('prefix') / get_option('localedir'))
conf.set('pkgdatadir', pkgdatadir)
conf.set('bindir', get_option('bindir'))
conf.set('prefix', get_option('prefix'))

subdir('data')
subdir('showtime')
subdir('po')

gnome.post_install(
    glib_compile_schemas: true,
    gtk_update_icon_cache: true,
    update_desktop_database: true,
)
