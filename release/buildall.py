import os
import subprocess
import shutil
import sys
from datetime import datetime

def run_command(cmd, cwd):
    print(f"\n>> Running: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=cwd, shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed with code {e.returncode}")
        sys.exit(1)

def main():
    print("========================================")
    print("  PristineCam - Unified Build Script")
    print("========================================")
    
    # Calculate paths
    release_script_dir = os.path.abspath(os.path.dirname(__file__))
    root_dir = os.path.abspath(os.path.join(release_script_dir, '..'))
    
    # Generate timestamp folder (Windows compatible format: dd-mm_-_HH-MM)
    timestamp = datetime.now().strftime("%d-%m_-_%H-%M")
    out_dir = os.path.join(release_script_dir, timestamp)
    os.makedirs(out_dir, exist_ok=True)
    
    print(f"Output Directory: {out_dir}\n")
    
    # 1. Build Debug APK
    run_command([sys.executable, 'build_debug.py', '--outdir', out_dir], cwd=root_dir)
    
    # 2. Build Release APK
    run_command([sys.executable, 'build_release.py', '--outdir', out_dir], cwd=root_dir)
    
    # 3. Build PC Executable
    run_command([sys.executable, 'buildexe.py'], cwd=root_dir)
    
    # 4. Move PC Executable to output directory
    exe_source = os.path.join(root_dir, 'dist', 'changed_gui.exe')
    exe_dest = os.path.join(out_dir, 'PristineCam.exe')
    
    if os.path.exists(exe_source):
        shutil.copy2(exe_source, exe_dest)
        print(f"\nSuccess! PC Executable copied to: {exe_dest}")
    else:
        print("\nWarning: Could not find the generated PC executable (dist/changed_gui.exe).")
        
    print("\n========================================")
    print(f"ALL BUILDS COMPLETE! Files are in: {out_dir}")
    print("========================================")

if __name__ == '__main__':
    main()
