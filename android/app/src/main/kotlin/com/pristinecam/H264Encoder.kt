package com.pristinecam

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Hardware H.264 / AVC video encoder using Android's native MediaCodec API.
 *
 * Configured in All-Intra mode (KEY_I_FRAME_INTERVAL = 0) for zero inter-frame
 * dependency, providing instant client sync and zero video artifacting on Wi-Fi packet drops.
 */
class H264Encoder(
    val width: Int,
    val height: Int,
    val fps: Int = 30,
    val bitrate: Int = 6_000_000,
    private val onOutputPacket: (ByteArray, Boolean) -> Unit
) {
    private var codec: MediaCodec? = null
    private val isRunning = AtomicBoolean(false)
    private var drainThread: Thread? = null

    private var spsPpsHeader: ByteArray? = null
    private var colorFormat: Int = MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
    private var reusableNv12 = ByteArray(width * height * 3 / 2)

    init {
        setupCodec()
    }

    private fun setupCodec() {
        try {
            colorFormat = selectColorFormat()

            val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT, colorFormat)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
                setInteger(MediaFormat.KEY_FRAME_RATE, fps)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 0) // ALL-INTRA: every frame is a keyframe!
                setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    setInteger(MediaFormat.KEY_LATENCY, 0)
                }
            }

            val encoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
            try {
                encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (e: Exception) {
                // Fallback for hardware encoders that reject I_FRAME_INTERVAL = 0
                format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
                encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
            encoder.start()

            codec = encoder
            isRunning.set(true)

            // Background drain thread to read encoded NAL units without blocking camera analysis
            drainThread = Thread({ drainLoop() }, "pristinecam-h264-drain").also {
                it.isDaemon = true
                it.start()
            }
        } catch (e: Exception) {
            e.printStackTrace()
            release()
        }
    }

    companion object {
        // Standard color format constants used by Android hardware & software encoders
        private const val COLOR_FORMAT_NV12 = 21
        private const val COLOR_FORMAT_I420 = 19
    }

    private fun selectColorFormat(): Int {
        val codecInfo = selectEncoderInfo(MediaFormat.MIMETYPE_VIDEO_AVC)
            ?: return MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
        val capabilities = codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)
        val formats = capabilities.colorFormats

        // Prefer NV12 (21), then I420 (19), then Flexible
        for (f in formats) {
            if (f == COLOR_FORMAT_NV12) return f
        }
        for (f in formats) {
            if (f == COLOR_FORMAT_I420) return f
        }
        for (f in formats) {
            if (f == MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible) return f
        }
        return formats.firstOrNull() ?: MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible
    }

    private fun selectEncoderInfo(mimeType: String): MediaCodecInfo? {
        val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
        for (info in list.codecInfos) {
            if (!info.isEncoder) continue
            val types = info.supportedTypes
            for (t in types) {
                if (t.equals(mimeType, ignoreCase = true)) return info
            }
        }
        return null
    }

    /**
     * Submit an NV21 camera frame for hardware encoding.
     */
    fun encodeFrame(nv21: ByteArray, ptsUs: Long) {
        val encoder = codec ?: return
        if (!isRunning.get()) return

        try {
            val inIndex = encoder.dequeueInputBuffer(5000L) // 5 ms timeout
            if (inIndex >= 0) {
                val inBuffer = encoder.getInputBuffer(inIndex) ?: return
                inBuffer.clear()

                val totalSize = width * height * 3 / 2
                if (reusableNv12.size != totalSize) {
                    reusableNv12 = ByteArray(totalSize)
                }

                if (colorFormat == COLOR_FORMAT_NV12) {
                    nv21ToNv12(nv21, reusableNv12, width, height)
                    inBuffer.put(reusableNv12, 0, totalSize)
                } else if (colorFormat == COLOR_FORMAT_I420) {
                    nv21ToI420(nv21, reusableNv12, width, height)
                    inBuffer.put(reusableNv12, 0, totalSize)
                } else {
                    inBuffer.put(nv21, 0, Math.min(nv21.size, inBuffer.remaining()))
                }

                encoder.queueInputBuffer(inIndex, 0, totalSize, ptsUs, 0)
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    /**
     * Converts NV21 (Y... + V,U,V,U...) to NV12 (Y... + U,V,U,V...) in-place into dest.
     */
    private fun nv21ToNv12(nv21: ByteArray, nv12: ByteArray, w: Int, h: Int) {
        val ySize = w * h
        val uvSize = ySize / 2
        // Copy Y plane directly
        System.arraycopy(nv21, 0, nv12, 0, ySize)
        // Swap U and V bytes in chroma plane
        var i = ySize
        val end = ySize + uvSize
        while (i < end) {
            val v = nv21[i]
            val u = nv21[i + 1]
            nv12[i] = u
            nv12[i + 1] = v
            i += 2
        }
    }

    /**
     * Converts NV21 (Y... + V,U,V,U...) to I420/Planar (Y... + U... + V...) into dest.
     */
    private fun nv21ToI420(nv21: ByteArray, i420: ByteArray, w: Int, h: Int) {
        val ySize = w * h
        val quarter = ySize / 4
        System.arraycopy(nv21, 0, i420, 0, ySize)
        var uIndex = ySize
        var vIndex = ySize + quarter
        var i = ySize
        val end = ySize + ySize / 2
        while (i < end) {
            val v = nv21[i]
            val u = nv21[i + 1]
            i420[uIndex++] = u
            i420[vIndex++] = v
            i += 2
        }
    }

    /**
     * Forces an immediate I-frame sync request to the hardware encoder.
     */
    fun requestKeyFrame() {
        val encoder = codec ?: return
        try {
            val params = Bundle().apply {
                putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0)
            }
            encoder.setParameters(params)
        } catch (_: Exception) {}
    }

    private fun drainLoop() {
        val encoder = codec ?: return
        val bufferInfo = MediaCodec.BufferInfo()

        while (isRunning.get()) {
            try {
                val outIndex = encoder.dequeueOutputBuffer(bufferInfo, 10_000L) // 10 ms
                if (outIndex >= 0) {
                    val outBuffer = encoder.getOutputBuffer(outIndex)
                    if (outBuffer != null && bufferInfo.size > 0) {
                        outBuffer.position(bufferInfo.offset)
                        outBuffer.limit(bufferInfo.offset + bufferInfo.size)

                        val chunk = ByteArray(bufferInfo.size)
                        outBuffer.get(chunk)

                        val isConfig = (bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                        val isKey = (bufferInfo.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0

                        if (isConfig) {
                            // Save SPS/PPS header to prepend to keyframes
                            spsPpsHeader = chunk
                        } else {
                            val packetToSend = if (isKey && spsPpsHeader != null && !containsSps(chunk)) {
                                spsPpsHeader!! + chunk
                            } else {
                                chunk
                            }
                            onOutputPacket(packetToSend, isKey)
                        }
                    }
                    encoder.releaseOutputBuffer(outIndex, false)
                }
            } catch (e: Exception) {
                if (isRunning.get()) {
                    e.printStackTrace()
                }
                break
            }
        }
    }

    private fun containsSps(data: ByteArray): Boolean {
        val offset = if (data.size >= 5 && data[0] == 0.toByte() && data[1] == 0.toByte() && data[2] == 0.toByte() && data[3] == 1.toByte()) {
            4
        } else if (data.size >= 4 && data[0] == 0.toByte() && data[1] == 0.toByte() && data[2] == 1.toByte()) {
            3
        } else {
            return false
        }
        return offset < data.size && (data[offset].toInt() and 0x1F) == 7
    }

    fun release() {
        isRunning.set(false)
        try {
            drainThread?.interrupt()
            drainThread = null
        } catch (_: Exception) {}

        try {
            codec?.stop()
            codec?.release()
        } catch (_: Exception) {}
        codec = null
    }
}
