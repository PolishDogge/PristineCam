package com.pristinecam

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.os.Binder
import android.os.IBinder
import android.os.PowerManager
import android.util.Size
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.UseCase
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors

/**
 * Foreground service that owns the camera and the MJPEG HTTP server.
 *
 * Lifecycle:
 *   - The service is bound by MainActivity, which registers/unregisters the
 *     Preview use case by calling [attachPreview].
 *   - ImageAnalysis always runs while the service is alive, so the stream
 *     continues even when the screen is dimmed or the app is backgrounded.
 *   - A PARTIAL_WAKE_LOCK keeps the CPU awake during streaming.
 *
 * Performance:
 *   - Resolution is pinned via [ResolutionSelector] + [ResolutionStrategy]
 *     (720p by default, selectable to 480p/1080p via [setResolution]).
 *   - STRATEGY_KEEP_ONLY_LATEST drops stale frames so the analysis
 *     executor never queues up behind a slow encoder.
 *   - Frame encoding runs on a dedicated single-thread executor; the MJPEG
 *     server simply reads the latest pre-compressed JPEG from an
 *     AtomicReference — zero contention between camera and HTTP threads.
 *
 * Privacy:
 *   - No analytics, no telemetry, no external network calls.
 *   - The only outbound traffic is the MJPEG stream on the local network.
 */
class StreamingService : LifecycleService() {

    // ── Public observable state ───────────────────────────────────────────────

    private val _isStreaming = MutableStateFlow(false)
    val isStreaming: StateFlow<Boolean> = _isStreaming.asStateFlow()

    private val _streamUrl = MutableStateFlow<String?>(null)
    val streamUrl: StateFlow<String?> = _streamUrl.asStateFlow()

    /** Currently active resolution preset, observable by the UI. */
    private val _resolution = MutableStateFlow(StreamResolution.RES_720P)
    val resolution: StateFlow<StreamResolution> = _resolution.asStateFlow()

    // ── Camera ────────────────────────────────────────────────────────────────

    private var cameraProvider: ProcessCameraProvider? = null
    private var imageAnalysis: ImageAnalysis?          = null
    private var previewUseCase: Preview?               = null
    private var cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA

    /** Dedicated executor for the ImageAnalysis.Analyzer — keeps encoding off
     *  the main thread and off the CameraX capture thread. */
    private val analysisExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "pristinecam-encoder").apply { isDaemon = true }
    }

    // ── HTTP server ───────────────────────────────────────────────────────────

    private val mjpegServer = MjpegServer(DEFAULT_PORT)
    var jpegQuality: Int = DEFAULT_QUALITY

    // ── Wake lock ─────────────────────────────────────────────────────────────

    private var wakeLock: PowerManager.WakeLock? = null

    // ── Binder (for Activity ↔ Service communication) ─────────────────────────

    inner class LocalBinder : Binder() {
        fun service() = this@StreamingService
    }
    private val binder = LocalBinder()

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    // ── Service lifecycle ─────────────────────────────────────────────────────

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_START -> startStreaming()
            ACTION_STOP  -> stopStreamingAndSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopStreaming()
        analysisExecutor.shutdown()
        super.onDestroy()
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /** Start the HTTP server, acquire wake lock, and begin capturing frames. */
    fun startStreaming() {
        if (_isStreaming.value) return
        acquireWakeLock()
        postForegroundNotification()

        // Auto-shutdown: when the last PC client disconnects and no new client
        // reconnects within 5 seconds, stop everything automatically.
        // The callback fires on a background timer thread, so we post to the
        // main looper to safely touch service/camera state.
        mjpegServer.onAllClientsDisconnected = {
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                stopStreamingAndSelf()
            }
        }

        mjpegServer.start()
        initCamera()
        _isStreaming.value = true
        refreshStreamUrl()
    }

    /** Stop all streaming activity; the service stays alive if still bound. */
    fun stopStreaming() {
        if (!_isStreaming.value) return
        releaseCamera()
        mjpegServer.onAllClientsDisconnected = null  // prevent stale callbacks
        mjpegServer.stop()
        releaseWakeLock()
        stopForeground(STOP_FOREGROUND_REMOVE)
        _isStreaming.value = false
        _streamUrl.value   = null
    }

    private fun stopStreamingAndSelf() {
        stopStreaming()
        stopSelf()
    }

    /**
     * Switch between front and back camera.
     * Safe to call while streaming; triggers a camera rebind.
     */
    fun setLensFacing(useFront: Boolean) {
        cameraSelector = if (useFront)
            CameraSelector.DEFAULT_FRONT_CAMERA
        else
            CameraSelector.DEFAULT_BACK_CAMERA
        if (_isStreaming.value) rebindCamera()
    }

    /**
     * Change the stream resolution. Rebuilds ImageAnalysis with the new
     * ResolutionStrategy and rebinds the camera if currently streaming.
     */
    fun setResolution(res: StreamResolution) {
        if (res == _resolution.value) return
        _resolution.value = res
        if (_isStreaming.value) {
            imageAnalysis = buildImageAnalysis()
            rebindCamera()
        }
    }

    /**
     * Attach a [PreviewView] to show live camera output in the Activity.
     * Pass null to detach the preview (e.g. when Activity is paused).
     * ImageAnalysis — and therefore streaming — continues either way.
     */
    fun attachPreview(previewView: PreviewView?) {
        previewUseCase = previewView?.let { pv ->
            Preview.Builder().build().also { it.setSurfaceProvider(pv.surfaceProvider) }
        }
        if (_isStreaming.value) rebindCamera()
    }

    // ── Camera internals ──────────────────────────────────────────────────────

    private fun initCamera() {
        ProcessCameraProvider.getInstance(this).also { future ->
            future.addListener({
                cameraProvider = future.get()
                imageAnalysis  = buildImageAnalysis()
                rebindCamera()
            }, ContextCompat.getMainExecutor(this))
        }
    }

    /**
     * Builds an [ImageAnalysis] use case pinned to the currently selected
     * resolution via [ResolutionSelector].
     *
     * Key design decisions:
     *   - [ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER]:
     *     if the device can't do exactly 1280×720 it picks the nearest lower,
     *     then the nearest higher — so we never get an absurd 4K frame.
     *   - [STRATEGY_KEEP_ONLY_LATEST]: the analysis executor always works on
     *     the freshest frame; stale frames are dropped by CameraX.
     *   - Output format is YUV_420_888 (CameraX default); we convert to JPEG
     *     manually via [YuvImage] — this is faster than going through
     *     RGBA_8888 → Bitmap → JPEG because it avoids the Bitmap allocation
     *     and the color-space conversion overhead.
     */
    private fun buildImageAnalysis(): ImageAnalysis {
        val target = _resolution.value.size

        val resolutionSelector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(
                    target,
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER
                )
            )
            .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
            .build()

        return ImageAnalysis.Builder()
            .setResolutionSelector(resolutionSelector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            // YUV_420_888 is the default; explicit for clarity.
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
            .build()
            .also { analysis ->
                analysis.setAnalyzer(analysisExecutor) { proxy -> encodeAndPush(proxy) }
            }
    }

    private fun rebindCamera() {
        val provider = cameraProvider ?: return
        provider.unbindAll()
        val useCases = buildList<UseCase> {
            imageAnalysis?.let { add(it) }
            previewUseCase?.let { add(it) }
        }
        if (useCases.isNotEmpty()) {
            runCatching {
                provider.bindToLifecycle(this, cameraSelector, *useCases.toTypedArray())
            }
        }
    }

    private fun releaseCamera() {
        cameraProvider?.unbindAll()
        cameraProvider = null
        imageAnalysis  = null
        previewUseCase = null
    }

    // ── Frame encoding ────────────────────────────────────────────────────────

    /**
     * Convert YUV_420_888 → NV21 → JPEG on the dedicated analysis executor,
     * then push the compressed bytes into the MjpegServer's AtomicReference.
     *
     * This is the hot path; we avoid:
     *   - Bitmap allocation (no toBitmap())
     *   - RGBA conversion (stay in YUV, compress directly)
     *   - Any synchronization (AtomicReference.set is lock-free)
     */
    private fun encodeAndPush(proxy: ImageProxy) {
        try {
            val width  = proxy.width
            val height = proxy.height
            val nv21   = yuvToNv21(proxy)
            val yuvImg = YuvImage(nv21, ImageFormat.NV21, width, height, null)
            val out    = ByteArrayOutputStream(width * height / 4)
            yuvImg.compressToJpeg(Rect(0, 0, width, height), jpegQuality, out)
            mjpegServer.pushFrame(out.toByteArray())
        } finally {
            proxy.close()  // must always be called to unblock the analysis pipeline
        }
    }

    /**
     * Converts a CameraX YUV_420_888 [ImageProxy] to a packed NV21 byte array.
     *
     * NV21 layout: [Y plane (w*h bytes)] [interleaved VU plane (w*h/2 bytes)]
     *
     * CameraX on most devices already outputs NV21 (the V/U plane *is* interleaved
     * with rowStride == width and pixelStride == 2), so in that fast path we just
     * bulk-copy the planes. On devices where pixelStride == 1 (rare, fully planar
     * YUV) we manually interleave.
     */
    private fun yuvToNv21(image: ImageProxy): ByteArray {
        val w = image.width
        val h = image.height
        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        val ySize  = w * h
        val uvSize = w * h / 2
        val nv21   = ByteArray(ySize + uvSize)

        // ── Y plane ──────────────────────────────────────────────────────
        val yBuf  = yPlane.buffer
        val yRow  = yPlane.rowStride
        if (yRow == w) {
            // Fast path: no padding, bulk copy
            yBuf.get(nv21, 0, ySize)
        } else {
            // Row-by-row copy, skipping row-stride padding
            var pos = 0
            for (row in 0 until h) {
                yBuf.position(row * yRow)
                yBuf.get(nv21, pos, w)
                pos += w
            }
        }

        // ── VU interleave ────────────────────────────────────────────────
        val vBuf     = vPlane.buffer
        val uBuf     = uPlane.buffer
        val vRowStr  = vPlane.rowStride
        val vPixStr  = vPlane.pixelStride

        if (vPixStr == 2 && vRowStr == w) {
            // Fast path: already interleaved NV21 in memory
            vBuf.get(nv21, ySize, uvSize.coerceAtMost(vBuf.remaining()))
        } else {
            // Slow path: pixel-by-pixel interleave
            var offset = ySize
            val halfH  = h / 2
            val halfW  = w / 2
            for (row in 0 until halfH) {
                for (col in 0 until halfW) {
                    val vIdx = row * vRowStr + col * vPixStr
                    val uIdx = row * uPlane.rowStride + col * uPlane.pixelStride
                    nv21[offset++] = vBuf.get(vIdx)
                    nv21[offset++] = uBuf.get(uIdx)
                }
            }
        }
        return nv21
    }

    // ── Wake lock ─────────────────────────────────────────────────────────────

    private fun acquireWakeLock() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        @Suppress("WakelockTimeout")
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKELOCK_TAG)
            .apply { acquire(MAX_STREAM_MS) }
    }

    private fun releaseWakeLock() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
    }

    // ── Foreground notification ───────────────────────────────────────────────

    private fun postForegroundNotification() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(NOTIF_CHANNEL_ID, "Camera Stream", NotificationManager.IMPORTANCE_LOW)
                .apply { description = "PristineCam is streaming" }
        )

        val stopPi = PendingIntent.getService(
            this, 0,
            Intent(this, StreamingService::class.java).apply { action = ACTION_STOP },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val openPi = PendingIntent.getActivity(
            this, 1,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification: Notification = NotificationCompat.Builder(this, NOTIF_CHANNEL_ID)
            .setContentTitle("PristineCam — Streaming")
            .setContentText("Serving camera on port $DEFAULT_PORT")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true)
            .setContentIntent(openPi)
            .addAction(android.R.drawable.ic_delete, "Stop", stopPi)
            .build()

        startForeground(NOTIF_ID, notification)
    }

    private fun refreshStreamUrl() {
        val ip = NetworkUtils.getLocalIpAddress() ?: "?.?.?.?"
        _streamUrl.value = "http://$ip:$DEFAULT_PORT/video_feed"
    }

    // ── Resolution presets ────────────────────────────────────────────────────

    enum class StreamResolution(val label: String, val size: Size) {
        RES_480P("480p",  Size(854,  480)),
        RES_720P("720p",  Size(1280, 720)),
        RES_1080P("1080p", Size(1920, 1080));
    }

    // ── Constants ─────────────────────────────────────────────────────────────

    companion object {
        const val ACTION_START    = "com.pristinecam.ACTION_START"
        const val ACTION_STOP     = "com.pristinecam.ACTION_STOP"
        const val DEFAULT_PORT    = 8080
        const val DEFAULT_QUALITY = 80  // JPEG quality 0–100

        private const val NOTIF_CHANNEL_ID = "pristinecam_stream"
        private const val NOTIF_ID         = 1
        private const val WAKELOCK_TAG     = "PristineCam:StreamWakeLock"
        private const val MAX_STREAM_MS    = 6L * 60 * 60 * 1000 // 6 hours max
    }
}
