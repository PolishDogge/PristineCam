import PyInstaller.__main__
import os

PyInstaller.__main__.run([
    'gui_client.py',
    '--onefile',
    '--windowed',
    '--icon=assets/icon.ico',
    f'--add-data=assets/icon.png{os.pathsep}assets'
])