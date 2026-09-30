import os
import subprocess
import shutil
import sys
from datetime import datetime

def run_command(cmd, cwd):
    print(f"\n>> Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, shell=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command '{' '.join(cmd)}' failed with exit code {result.returncode}")

def get_python_executable():
    """Finds a Python interpreter with required dependencies (PyQt6, cv2, PyInstaller)."""
    # 1. If current interpreter has PyQt6 and cv2, use it directly
    try:
        import PyQt6  # noqa: F401
        import cv2    # noqa: F401
        return sys.executable
    except ImportError:
        pass

    # 2. Try the Windows Python Launcher for Python 3.13
    try:
        res = subprocess.run(['py', '-3.13', '-c', 'import sys; print(sys.executable)'],
                             capture_output=True, text=True, check=True)
        py_path = res.stdout.strip()
        if py_path and os.path.exists(py_path):
            test = subprocess.run([py_path, '-c', 'import PyQt6, cv2; print("OK")'],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                print(f"[Python Detection] Current interpreter ({sys.executable}) lacks dependencies.")
                print(f"[Python Detection] Auto-switching to Python 3.13: {py_path}\n")
                return py_path
    except Exception:
        pass

    # 3. Try standard installation path for Python 3.13
    fallback = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\python.exe")
    if os.path.exists(fallback):
        return fallback

    return sys.executable

def main():
    print("========================================")
    print("  PristineCam v1.2 - Unified Build Script")
    print("========================================")
    
    python_bin = get_python_executable()

    # Calculate paths
    release_script_dir = os.path.abspath(os.path.dirname(__file__))
    root_dir = os.path.abspath(os.path.join(release_script_dir, '..'))
    latest_txt_path = os.path.join(release_script_dir, 'latest.txt')
    
    # Generate timestamp folder (Windows compatible format: dd-mm_-_HH-MM)
    timestamp = datetime.now().strftime("%d-%m_-_%H-%M")
    out_dir = os.path.join(release_script_dir, timestamp)
    os.makedirs(out_dir, exist_ok=True)
    
    print(f"Output Directory: {out_dir}\n")

    try:
        # 1. Build Debug APK
        run_command([python_bin, 'build_debug.py', '--outdir', out_dir], cwd=root_dir)
        debug_apk = os.path.join(out_dir, 'PristineCam-Debug.apk')
        if not os.path.exists(debug_apk):
            raise FileNotFoundError(f"Debug APK was not found at {debug_apk}")

        # 2. Build Release APK
        run_command([python_bin, 'build_release.py', '--outdir', out_dir], cwd=root_dir)
        release_apk = os.path.join(out_dir, 'PristineCam-Release.apk')
        if not os.path.exists(release_apk):
            raise FileNotFoundError(f"Release APK was not found at {release_apk}")

        # 3. Pre-Build Self-Tests (Guarantees PC client is working before PyInstaller packaging)
        run_command([python_bin, 'pristinecam.py', '--self-test'], cwd=root_dir)

        # 4. Build PC Executable (ensure any running instances are closed so dist file is not locked)
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/f', '/im', 'PristineCam.exe'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['taskkill', '/f', '/im', 'changed_gui.exe'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run_command([python_bin, 'buildexe.py'], cwd=root_dir)
        
        # 5. Move PC Executable to output directory
        exe_source = os.path.join(root_dir, 'dist', 'PristineCam.exe')
        exe_dest = os.path.join(out_dir, 'PristineCam.exe')
        
        if not os.path.exists(exe_source):
            raise FileNotFoundError(f"Could not find the generated PC executable ({exe_source})")
            
        shutil.copy2(exe_source, exe_dest)
        print(f"\nSuccess! PC Executable copied to: {exe_dest}")

        # Write latest.txt pointing to the successful release folder
        with open(latest_txt_path, 'w', encoding='utf-8') as f:
            f.write(f"BUILD SUCCESS\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Folder: {timestamp}\n")
            f.write(f"Artifacts:\n")
            f.write(f"  - PristineCam-Debug.apk\n")
            f.write(f"  - PristineCam-Release.apk\n")
            f.write(f"  - PristineCam.exe\n")

        print("\n========================================")
        print(f"ALL BUILDS COMPLETE! Files are in: {out_dir}")
        print("========================================")

    except Exception as exc:
        print(f"\n[BUILD FAILURE] {exc}")
        print(f"Cleaning up incomplete build folder: {out_dir}")
        shutil.rmtree(out_dir, ignore_errors=True)

        with open(latest_txt_path, 'w', encoding='utf-8') as f:
            f.write(f"BUILD FAILED\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Error: {exc}\n")

        print(f"Recorded failure to {latest_txt_path}")
        sys.exit(1)

if __name__ == '__main__':
    main()
