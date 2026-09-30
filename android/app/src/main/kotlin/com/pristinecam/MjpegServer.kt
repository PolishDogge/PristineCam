package com.pristinecam

import fi.iki.elonen.NanoHTTPD
import java.io.IOException
import java.io.PipedInputStream
import java.io.PipedOutputStream
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * Lightweight MJPEG HTTP server built on NanoHTTPD.
 *
 * Endpoints:
 *   GET /           → simple HTML page that embeds the stream in an <img> tag
 *   GET /video_feed → multipart/x-mixed-replace MJPEG stream
 *   GET /status     → JSON: {"battery":<0-100>,"charging":<bool>}
 *
 * Thread safety:
 *   [pushFrame] may be called from any thread (e.g. CameraX analysis executor).
 *   Each connected client runs in its own daemon thread and polls [latestJpeg]
 *   via a lock-free version counter — no blocking between producer and consumers.
 *
 * Auto-shutdown:
 *   Tracks the number of active streaming clients via [activeClients].
 *   When a client disconnects (IOException on the pipe), the count decrements.
 *   If the count reaches zero and at least one client had connected, a grace
 *   period ([DISCONNECT_GRACE_MS]) starts. If no new client connects within
 *   that window, [onAllClientsDisconnected] fires — the service uses this
 *   callback to stop the camera, HTTP server, and foreground notification.
 */
class MjpegServer(port: Int) : NanoHTTPD(port) {

    private val latestJpeg   = AtomicReference<ByteArray?>(null)
    private val frameVersion = AtomicLong(0L)

    private val frameLock = Object()

    private class H264Client(
        val pipedOut: PipedOutputStream
    ) {
        val queue = LinkedBlockingQueue<ByteArray>(60)
        @Volatile var isClosed = false
    }

    private val h264Clients = CopyOnWriteArrayList<H264Client>()

    /** Number of clients currently receiving the stream. */
    val activeClients = AtomicInteger(0)

    /**
     * Called (on a background thread) when the last streaming client
     * disconnects and no new client reconnects within the grace period.
     * Set this before calling [start].
     */
    var onAllClientsDisconnected: (() -> Unit)? = null

    /**
     * Called when a client connects to the /live.ts H.264 stream.
     */
    var onH264ClientConnected: (() -> Unit)? = null

    data class StreamStatus(
        val battery: Int,
        val charging: Boolean,
        val fps: Int,
        val width: Int,
        val height: Int,
        val codec: String
    )

    /**
     * Called on each GET /status request to read the current device/stream status.
     * Set by StreamingService after construction.
     */
    var statusProvider: (() -> StreamStatus)? = null

    /**
     * Legacy callback for battery status.
     */
    var batteryProvider: (() -> Pair<Int, Boolean>)? = null

    /** Grace-period timer thread — cancelled if a new client connects. */
    @Volatile
    private var graceTimer: Thread? = null

    /** Replace the current frame; all connected clients will send it immediately. */
    fun pushFrame(jpeg: ByteArray) {
        latestJpeg.set(jpeg)
        frameVersion.incrementAndGet()
        synchronized(frameLock) {
            frameLock.notifyAll()
        }
    }

    /** Push encoded MPEG-TS chunk to connected /live.ts clients immediately. */
    fun pushH264Ts(tsChunk: ByteArray) {
        for (client in h264Clients) {
            if (!client.queue.offer(tsChunk)) {
                client.queue.poll()
                client.queue.offer(tsChunk)
            }
        }
    }

    // ── Request routing ───────────────────────────────────────────────────────

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/live.ts"    -> serveH264Stream()
            "/video_feed" -> serveMjpegStream()
            "/status"     -> serveStatus()
            "/"           -> serveIndexPage()
            else          -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, MIME_PLAINTEXT, "Not found"
            )
        }
    }

    private fun serveStatus(): Response {
        val json = if (statusProvider != null) {
            val s = statusProvider!!.invoke()
            """{"battery":${s.battery},"charging":${s.charging},"fps":${s.fps},"width":${s.width},"height":${s.height},"codec":"${s.codec}"}"""
        } else {
            val (level, charging) = batteryProvider?.invoke() ?: Pair(-1, false)
            """{"battery":$level,"charging":$charging}"""
        }
        return newFixedLengthResponse(Response.Status.OK, "application/json", json)
            .also { it.addHeader("Cache-Control", "no-cache") }
    }

    private fun serveIndexPage(): Response = newFixedLengthResponse(
        Response.Status.OK, "text/html; charset=utf-8",
        """<!DOCTYPE html>
           |<html lang="en">
           |<head><meta charset="UTF-8"><title>PristineCam</title>
           |<style>*{margin:0;padding:0;box-sizing:border-box}
           |body{background:#000;display:flex;align-items:center;justify-content:center;height:100vh}
           |img{max-width:100%;max-height:100vh;object-fit:contain}</style></head>
           |<body><img src="/video_feed" alt="PristineCam stream"/></body>
           |</html>""".trimMargin()
    )

    // ── H.264 MPEG-TS streaming ────────────────────────────────────────────────

    private fun serveH264Stream(): Response {
        val pipedOut = PipedOutputStream()
        val pipedIn  = PipedInputStream(pipedOut, PIPE_BUFFER_BYTES)

        cancelGraceTimer()
        activeClients.incrementAndGet()

        val client = H264Client(pipedOut)
        h264Clients.add(client)

        // Request an immediate keyframe so this new client connects instantly
        onH264ClientConnected?.invoke()

        Thread({
            try {
                while (!client.isClosed) {
                    val chunk = client.queue.poll(500, TimeUnit.MILLISECONDS)
                    if (chunk != null) {
                        pipedOut.write(chunk)
                        pipedOut.flush()
                    }
                }
            } catch (_: IOException) {
                // Client disconnected — socket closed or pipe broken.
            } catch (_: InterruptedException) {
                // Service shutdown.
            } finally {
                client.isClosed = true
                h264Clients.remove(client)
                runCatching { pipedOut.close() }
                onClientDisconnected()
            }
        }, "pristinecam-h264-${activeClients.get()}").also { it.isDaemon = true }.start()

        return newChunkedResponse(
            Response.Status.OK,
            "video/mp2t",
            pipedIn
        ).also {
            it.addHeader("Cache-Control", "no-cache")
        }
    }

    // ── MJPEG streaming ───────────────────────────────────────────────────────

    private fun serveMjpegStream(): Response {
        val pipedOut = PipedOutputStream()
        val pipedIn  = PipedInputStream(pipedOut, PIPE_BUFFER_BYTES)

        // A new client just connected — cancel any pending auto-shutdown grace timer.
        cancelGraceTimer()
        activeClients.incrementAndGet()

        // One daemon thread per connected client: writes MJPEG parts to the pipe.
        // NanoHTTPD reads from pipedIn and sends the bytes to the TCP socket.
        Thread({
            try {
                var lastVersion = -1L
                while (true) {
                    val current = frameVersion.get()
                    if (current != lastVersion) {
                        lastVersion = current
                        val jpeg = latestJpeg.get() ?: continue

                        pipedOut.write(
                            "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\n\r\n"
                                .toByteArray(Charsets.US_ASCII)
                        )
                        pipedOut.write(jpeg)
                        pipedOut.write("\r\n".toByteArray(Charsets.US_ASCII))
                        pipedOut.flush()
                    } else {
                        synchronized(frameLock) {
                            if (frameVersion.get() == lastVersion) {
                                frameLock.wait(WAIT_TIMEOUT_MS)
                            }
                        }
                    }
                }
            } catch (_: IOException) {
                // Client disconnected — socket closed or pipe broken.
            } catch (_: InterruptedException) {
                // Service shutdown.
            } finally {
                runCatching { pipedOut.close() }
                onClientDisconnected()
            }
        }, "pristinecam-stream-${activeClients.get()}").also { it.isDaemon = true }.start()

        return newChunkedResponse(
            Response.Status.OK,
            "multipart/x-mixed-replace; boundary=frame",
            pipedIn
        )
    }

    // ── Client tracking & auto-shutdown ───────────────────────────────────────

    private fun onClientDisconnected() {
        val remaining = activeClients.decrementAndGet()
        if (remaining <= 0) {
            // Last client gone — start the grace period before auto-shutdown.
            startGraceTimer()
        }
    }

    /**
     * Starts a grace-period timer. If no new client connects within
     * [DISCONNECT_GRACE_MS], fires [onAllClientsDisconnected].
     */
    private fun startGraceTimer() {
        cancelGraceTimer()
        val timer = Thread({
            try {
                Thread.sleep(DISCONNECT_GRACE_MS)
                // Still no clients after the grace period — trigger shutdown.
                if (activeClients.get() <= 0) {
                    onAllClientsDisconnected?.invoke()
                }
            } catch (_: InterruptedException) {
                // Cancelled — a new client connected in time.
            }
        }, "pristinecam-grace-timer")
        timer.isDaemon = true
        graceTimer = timer
        timer.start()
    }

    /** Cancel any pending grace-period timer (e.g. when a new client connects). */
    private fun cancelGraceTimer() {
        graceTimer?.interrupt()
        graceTimer = null
    }

    override fun stop() {
        cancelGraceTimer()
        for (client in h264Clients) {
            client.isClosed = true
        }
        h264Clients.clear()
        synchronized(frameLock) { frameLock.notifyAll() }
        super.stop()
    }

    companion object {
        private const val PIPE_BUFFER_BYTES     = 512 * 1024 // 512 KB per client connection
        private const val WAIT_TIMEOUT_MS       = 100L       // Fallback wait timeout
        /** Seconds to wait after the last client disconnects before auto-stopping. */
        private const val DISCONNECT_GRACE_MS   = 5_000L     // 5 seconds
    }
}
