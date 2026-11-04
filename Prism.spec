from PyInstaller.utils.hooks import collect_all
av_datas, av_binaries, av_hidden = collect_all('av')
oiio_datas, oiio_binaries, oiio_hidden = collect_all('OpenImageIO')
a = Analysis(['prism/__main__.py'], pathex=[], binaries=av_binaries+oiio_binaries,
 datas=[('prism/assets','prism/assets'), ('prism/vendor/vcolorpicker/LICENSE','prism/vendor/vcolorpicker'), ('prism/vendor/vcolorpicker/ORIGIN.json','prism/vendor/vcolorpicker'), ('prism/components','prism/components'), ('prism/source_store.py','prism'), ('prism/storage','prism/storage')]+av_datas+oiio_datas,
 hiddenimports=['exif','lxml.etree','plum','docx','numpy','PyQt6.QtMultimedia','PyQt6.QtSvg','PyQt6.QtSvgWidgets','PyQt6.QtWebEngineWidgets','PyQt6.QtWebEngineCore','prism.components.material_preview']+av_hidden+oiio_hidden,
 hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest','f3d','vtk','vtkmodules'], noarchive=False, optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Prism',debug=False,strip=False,upx=False,console=False,icon='prism/assets/logo.ico')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='Prism')
