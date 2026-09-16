import os
import subprocess
import shutil
import sys
import argparse
from datetime import datetime

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--outdir', type=str, help='Output directory for the APK')
    args = parser.parse_args()

    print("Building PristineCam Android - Debug APK...")
    root_dir = os.path.abspath(os.path.dirname(__file__))
    android_dir = os.path.join(root_dir, 'android')

    gradle_cmd = 'gradlew' if os.name == 'nt' else './gradlew'
    if not os.path.exists(os.path.join(android_dir, 'gradlew.bat' if os.name == 'nt' else 'gradlew')):
        gradle_cmd = 'gradle'
        
    cmd = [gradle_cmd, 'assembleDebug']
    try:
        subprocess.run(cmd, cwd=android_dir, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: Build failed with code {e.returncode}")
        sys.exit(1)
    
    apk_source = os.path.join(android_dir, 'app', 'build', 'outputs', 'apk', 'debug', 'app-debug.apk')
    
    if args.outdir:
        out_dir = os.path.abspath(args.outdir)
    else:
        # Create a timestamped folder in release directory (using _-_ instead of | for Windows compatibility)
        timestamp = datetime.now().strftime("%d-%m_-_%H-%M")
        out_dir = os.path.join(root_dir, 'release', timestamp)
        
    os.makedirs(out_dir, exist_ok=True)
    apk_dest = os.path.join(out_dir, 'PristineCam-Debug.apk')
    
    if os.path.exists(apk_source):
        shutil.copy2(apk_source, apk_dest)
        print(f"\nSuccess! Debug APK copied to: {apk_dest}")
    else:
        print("\nError: Could not find the generated APK.")
        sys.exit(1)

if __name__ == '__main__':
    main()
