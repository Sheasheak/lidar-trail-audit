# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Collect rasterio and pyproj data/binaries
rasterio_datas   = collect_data_files('rasterio')
pyproj_datas     = collect_data_files('pyproj')
rasterio_bins    = collect_dynamic_libs('rasterio')

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=rasterio_bins,
    datas=[
        ('templates', 'templates'),
        ('static',    'static'),
    ] + rasterio_datas + pyproj_datas,
    hiddenimports=[
        'rasterio._shim',
        'rasterio.control',
        'rasterio.crs',
        'rasterio.merge',
        'rasterio.transform',
        'pyproj',
        'pyproj.datadir',
        'flask',
        'werkzeug',
        'jinja2',
        'click',
        'grader',
        'config',
    ],
    hookspath=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NZ_MTB_Trail_Grader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,       # No terminal window
    icon=None,
)
