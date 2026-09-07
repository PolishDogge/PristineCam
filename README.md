# PristineCam

[![GitHub Release](https://img.shields.io/github/v/release/PolishDogge/PristineCam?color=blue)](https://github.com/PolishDogge/PristineCam/releases)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://github.com/PolishDogge/PristineCam/blob/main/LICENSE.txt)

PristineCam is a private, open-source, ad-free desktop virtual camera powered by your Android device. It uses your Android phone's camera to stream video over your local network (or via a USB cable using ADB) and registers it as a system virtual webcam on your PC for use in Zoom, Discord, OBS, and more.

## Features

- **Strictly Local & Private:** Zero analytics, zero tracking, no cloud telemetry, no internet required. All traffic stays on your local network or USB cable.
- **Auto-Discovery (mDNS):** The PC client automatically finds your phone on the Wi-Fi network—no manual IP typing required!
- **OLED Battery Saver:** The Android app automatically darkens the screen after 1 minute of streaming to save battery and prevent screen burn-in.
- **Background Streaming:** The Android app runs as a foreground service, so the camera stream continues even if you switch apps.
- **Low Latency USB Mode:** Connect via USB and ADB port-forwarding for a direct, low-latency stream without relying on Wi-Fi stability.
- **Desktop GUI:** A clean, dark-mode desktop client with real-time controls for mirroring, rotation, FPS, stream resolution, and advanced camera controls.
- **Standalone Windows Executable:** No need to install Python. Just download the `.exe` and run it.

---

## Download & Installation (Recommended)

Pre-built releases for Android and Windows are available on the [GitHub Releases](https://github.com/PolishDogge/PristineCam/releases) page.

### Android App (`PristineCam.apk`)
1. Download `PristineCam.apk` from the latest release to your phone.
2. Open it to install (allow "Install unknown apps" if prompted).
3. **Note on False Positives:** As an indie open-source app, Google Play Protect or your antivirus may flag it as an "Unknown developer" or give a generic malware warning. This is a false positive. Tap **More details** -> **Install anyway**.

*Alternative (ADB):* `adb install PristineCam.apk`

### PC Client (`PristineCam.exe` - Windows)
1. Ensure you have a virtual camera driver installed on your PC. The easiest way is to install [OBS Studio](https://obsproject.com/), which provides the necessary driver automatically.
2. Download `PristineCam.exe` from the latest release.
3. Double-click to run! (No installation wizard required).
4. **Note on Defender:** Windows Defender may block it initially as an unrecognized app. Click **More info** -> **Run anyway**.

---

## How to Use (Wi-Fi vs USB)

### Via Wi-Fi (Easy)
1. Open PristineCam on your phone.
2. Open `PristineCam.exe` on your PC.
3. Click **Scan for devices** in the PC client. Your phone will appear in the dropdown.
4. Select your phone and it will automatically connect!

### Via USB (Low Latency / Recommended)
1. Ensure your phone is connected via USB and **USB Debugging** is enabled in Developer Options.
2. Ensure you have the [Android Platform Tools (ADB)](https://developer.android.com/tools/releases/platform-tools) installed on your PC.
3. Open the PristineCam PC client and check the **"USB Mode (ADB port-forward)"** box.
4. Click **Connect**. The GUI automatically handles the ADB port-forwarding and connects instantly.

---

## Building from Source

If you prefer to compile PristineCam yourself:

### PC Client
1. Install [Python 3.8+](https://www.python.org/downloads/).
2. Clone the repo and install dependencies: `pip install -r requirements.txt`
3. Run from source: `python gui_client.py` (or headless: `python pc_client.py --ip YOUR_PHONE_IP --port 8080`)
4. **Build the `.exe`:** Run `.\buildexe.py` (requires Pyinstaller).

### Android App
1. Open the `android/` folder in **Android Studio**.
2. Wait for Gradle Sync to complete.
3. Build the APK using `gradle assembleRelease` (or run it directly to your connected device).

---

## License

This project is entirely open-source and free, distributed under the [GPLv3 License](https://github.com/PolishDogge/PristineCam/blob/main/LICENSE.txt). See the [`LICENSE.txt`](LICENSE.txt) file in the repository for full details.
