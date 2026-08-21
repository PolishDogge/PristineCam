# PristineCam

PristineCam is a private, open-source, ad-free desktop virtual camera powered by your Android device. It uses your Android phone's camera to stream video over your local network (or via a USB cable using ADB) and registers it as a system virtual webcam on your PC.

**Features:**
- **Strictly Local & Private:** Zero analytics, zero tracking, no cloud telemetry, no internet required. All traffic stays on your local network or USB cable.
- **Background Streaming:** The Android app runs as a foreground service, so the camera stream continues even if you dim your screen or switch apps.
- **Low Latency USB Mode:** Connect via USB and ADB port-forwarding for a direct, low-latency stream without relying on Wi-Fi stability.
- **Desktop GUI:** A clean, dark-mode desktop client with real-time controls for mirroring, rotation, FPS, and virtual camera backend selection.
- **Cross-Platform PC Client:** The client works on Windows, macOS, and Linux (via OBS Virtualcam or v4l2loopback).

---

## Architecture

1. **Android App:** Built with Kotlin, CameraX, and NanoHTTPD. It captures frames, compresses them to JPEG, and serves them as an MJPEG HTTP stream on port `8080` (e.g., `http://192.168.1.100:8080/video_feed`).
2. **PC Client:** A Python/PyQt6 application that connects to the MJPEG stream, allows for real-time manipulation (rotation/flipping), and pipes the frames into `pyvirtualcam` to emulate a physical webcam.

---

## 1. Setting up the PC Client

### Prerequisites
1. Install [Python 3.8+](https://www.python.org/downloads/).
2. Install a Virtual Camera backend driver:
   - **Windows / macOS:** Install [OBS Studio](https://obsproject.com/). Its virtual camera driver is bundled and `pyvirtualcam` will detect it automatically.
   - **Linux:** Install `v4l2loopback` (e.g., `sudo apt install v4l2loopback-dkms` and `sudo modprobe v4l2loopback`).

### Installation
Clone this repository and install the Python dependencies:

```bash
pip install -r requirements.txt
```

### Running the Client
Start the GUI client:
```bash
python gui_client.py
```
*(Alternatively, you can run the headless command-line client: `python pc_client.py --url http://YOUR_PHONE_IP:8080/video_feed`)*

---

## 2. Building and Installing the Android App

> **Note:** A pre-built APK is not currently included in the repository. You must build it using Android Studio.

### Prerequisites
- [Android Studio](https://developer.android.com/studio) (Hedgehog 2023.1 or newer).
- Android device running Android 7.0 (API 24) or higher.

### Build Instructions
1. Open **Android Studio** and select **Open** (or File → Open).
2. Navigate to and select the `android/` subfolder in this repository.
3. Wait for the initial **Gradle Sync** to finish downloading dependencies.
4. Enable **Developer Options** and **USB Debugging** on your Android phone, then connect it to your PC.
5. Select your device in the Android Studio toolbar and click the green **Run** button to build and install the debug app on your phone.

For a standalone APK, you can run:
- Windows: `.\gradlew assembleDebug` (inside the `android/` directory)
- macOS/Linux: `./gradlew assembleDebug` (inside the `android/` directory)
The APK will be generated at `android/app/build/outputs/apk/debug/app-debug.apk`.

---

## 3. Streaming (Wi-Fi vs USB)

### Via Wi-Fi (Easy)
1. Open the PristineCam app on your phone. It will display a "Connect PC to: http://[IP]:8080" message.
2. In the PC GUI, enter that IP address and click **Connect**. Ensure both devices are on the same Wi-Fi network.

### Via USB (Low Latency / Recommended)
To stream without relying on Wi-Fi, you can use Android Debug Bridge (ADB) to forward the local port over the USB cable.

1. Ensure your phone is connected via USB and USB Debugging is enabled.
2. Ensure you have the [Android Platform Tools (ADB)](https://developer.android.com/tools/releases/platform-tools) installed on your PC.
3. In the PristineCam PC GUI, check the **"USB Mode (ADB port-forward)"** box.
4. Click **Connect**. The GUI will automatically run the command `adb forward tcp:8080 tcp:8080` in the background and connect to `127.0.0.1`.

---

## License

This project is open-source and free. Under GPLv3 License.
