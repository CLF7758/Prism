from PyInstaller.utils.hooks import collect_all
av_datas, av_binaries, av_hidden = collect_all('av')
a = Analysis(['prism/__main__.py'], pathex=[], binaries=av_binaries,
 datas=[('prism/assets','prism/assets')]+av_datas,
 hiddenimports=['exif','lxml.etree','plum','docx','numpy','PyQt6.QtMultimedia','PyQt6.QtSvg','PyQt6.QtSvgWidgets','PyQt6.QtWebEngineWidgets','PyQt6.QtWebEngineCore']+av_hidden,
 hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest'], noarchive=False, optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Prism',debug=False,strip=False,upx=False,console=False,icon='prism/assets/logo.ico')
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='Prism')
