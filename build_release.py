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

    print("Building PristineCam Android - Release APK...")
    
    root_dir = os.path.abspath(os.path.dirname(__file__))
    android_dir = os.path.join(root_dir, 'android')
    keystore_path = os.path.join(root_dir, 'dev', 'keys', 'android')
    password_path = os.path.join(root_dir, 'dev', 'keys', 'pass.txt')
    
    gradle_cmd = 'gradlew' if os.name == 'nt' else './gradlew'
    if not os.path.exists(os.path.join(android_dir, 'gradlew.bat' if os.name == 'nt' else 'gradlew')):
        gradle_cmd = 'gradle'
        
    cmd = [gradle_cmd, 'assembleRelease']
    
    # Safely load the password from pass.txt if available
    if os.path.exists(keystore_path) and os.path.exists(password_path):
        with open(password_path, 'r') as f:
            pwd = f.read().strip()
        
        cmd.extend([
            f'-Pandroid.injected.signing.store.file={keystore_path}',
            f'-Pandroid.injected.signing.store.password={pwd}',
            f'-Pandroid.injected.signing.key.alias=pristine',
            f'-Pandroid.injected.signing.key.password={pwd}'
        ])
        print("Loaded signing configuration and password from dev/keys/pass.txt")
    else:
        print(f"Warning: Keystore or pass.txt not found in dev/keys/.")
        print("Building unsigned release APK (or relying on gradle config).")

    try:
        subprocess.run(cmd, cwd=android_dir, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: Build failed with code {e.returncode}")
        sys.exit(1)
    
    if args.outdir:
        out_dir = os.path.abspath(args.outdir)
    else:
        timestamp = datetime.now().strftime("%d-%m_-_%H-%M")
        out_dir = os.path.join(root_dir, 'release', timestamp)
        
    os.makedirs(out_dir, exist_ok=True)
    
    apk_source = os.path.join(android_dir, 'app', 'build', 'outputs', 'apk', 'release', 'app-release.apk')
    apk_dest = os.path.join(out_dir, 'PristineCam-Release.apk')
    
    if os.path.exists(apk_source):
        shutil.copy2(apk_source, apk_dest)
        print(f"\nSuccess! Signed Release APK copied to: {apk_dest}")
    else:
        unsigned_source = os.path.join(android_dir, 'app', 'build', 'outputs', 'apk', 'release', 'app-release-unsigned.apk')
        if os.path.exists(unsigned_source):
            unsigned_dest = os.path.join(out_dir, 'PristineCam-Release-Unsigned.apk')
            shutil.copy2(unsigned_source, unsigned_dest)
            print(f"\nSuccess! Unsigned Release APK copied to: {unsigned_dest}")
        else:
            print("\nError: Could not find the generated APK.")
            sys.exit(1)

if __name__ == '__main__':
    main()
