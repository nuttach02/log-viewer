# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

uvicorn_datas, uvicorn_bins, uvicorn_hidden = collect_all('uvicorn')
anyio_datas,   anyio_bins,   anyio_hidden   = collect_all('anyio')

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=uvicorn_bins + anyio_bins,
    datas=[
        ('templates', 'templates'),
        ('static',    'static'),
    ] + uvicorn_datas + anyio_datas,
    hiddenimports=uvicorn_hidden + anyio_hidden + [
        'fastapi',
        'starlette',
        'starlette.routing',
        'starlette.staticfiles',
        'starlette.templating',
        'jinja2',
        'jinja2.ext',
        'python_multipart',
        'multipart',
        'h11',
        'httptools',
        'click',
        'colorama',
        'email.mime',
        'email.mime.text',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'streamlit', 'pandas', 'numpy', 'altair', 'pyarrow',
        'PIL', 'pydeck', 'streamlit', 'tornado', 'bokeh',
        'matplotlib', 'scipy', 'sklearn',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='log_viewer',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='log_viewer',
)
