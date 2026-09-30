import os
import PyInstaller.__main__

PyInstaller.__main__.run([
    'pristinecam.py',
    '--name=PristineCam',
    '--onefile',
    '--windowed',
    '--icon=assets/icon.ico',
    f'--add-data=assets/icon.png{os.pathsep}assets',
])