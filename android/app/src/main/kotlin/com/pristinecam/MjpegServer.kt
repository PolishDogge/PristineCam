package com.pristinecam

import fi.iki.elonen.NanoHTTPD
import java.io.IOException
import java.io.PipedInputStream
import java.io.PipedOutputStream
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * Lightweight MJPEG HTTP server built on NanoHTTPD.
 *
 * Endpoints:
 *   GET /           → simple HTML page that embeds the stream in an <img> tag
 *   GET /video_feed → multipart/x-mixed-replace MJPEG stream
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

    /** Number of clients currently receiving the MJPEG stream. */
    val activeClients = AtomicInteger(0)

    /**
     * Called (on a background thread) when the last streaming client
     * disconnects and no new client reconnects within the grace period.
     * Set this before calling [start].
     */
    var onAllClientsDisconnected: (() -> Unit)? = null

    /** Grace-period timer thread — cancelled if a new client connects. */
    @Volatile
    private var graceTimer: Thread? = null

    /** Replace the current frame; all connected clients will send it on their next poll. */
    fun pushFrame(jpeg: ByteArray) {
        latestJpeg.set(jpeg)
        frameVersion.incrementAndGet()
    }

    // ── Request routing ───────────────────────────────────────────────────────

    override fun serve(session: IHTTPSession): Response {
        return when (session.uri) {
            "/video_feed" -> serveMjpegStream()
            "/"           -> serveIndexPage()
            else          -> newFixedLengthResponse(
                Response.Status.NOT_FOUND, MIME_PLAINTEXT, "Not found"
            )
        }
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
                        val jpeg = latestJpeg.get() ?: run {
                            Thread.sleep(POLL_INTERVAL_MS)
                            return@run null
                        } ?: continue

                        pipedOut.write(
                            "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.size}\r\n\r\n"
                                .toByteArray(Charsets.US_ASCII)
                        )
                        pipedOut.write(jpeg)
                        pipedOut.write("\r\n".toByteArray(Charsets.US_ASCII))
                        pipedOut.flush()
                    } else {
                        Thread.sleep(POLL_INTERVAL_MS)
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
        super.stop()
    }

    companion object {
        private const val PIPE_BUFFER_BYTES     = 512 * 1024 // 512 KB per client connection
        private const val POLL_INTERVAL_MS      = 5L
        /** Seconds to wait after the last client disconnects before auto-stopping. */
        private const val DISCONNECT_GRACE_MS   = 5_000L     // 5 seconds
    }
}
