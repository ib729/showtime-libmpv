blueprints = custom_target(
    'blueprints',
    input: files('options.blp', 'shortcuts-dialog.blp', 'sound-options.blp', 'window.blp'),
    output: ['options.ui', 'shortcuts-dialog.ui', 'sound-options.ui', 'window.ui'],
    command: [
        find_program('blueprint-compiler'),
        'batch-compile',
        '@OUTDIR@',
        '@CURRENT_SOURCE_DIR@',
        '@INPUT@',
    ],
)
