import PyInstaller.__main__
import os

PyInstaller.__main__.run([
    'changed_gui.py',
    '--onefile',
    '--windowed',
    '--icon=assets/icon.ico',
    f'--add-data=assets/icon.png{os.pathsep}assets'
])