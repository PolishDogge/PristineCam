package com.pristinecam

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import android.util.Log
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
        val candidates: List<() -> MediaCodec> = listOf(
            { MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC) },
            { MediaCodec.createByCodecName("c2.android.avc.encoder") },
            { MediaCodec.createByCodecName("OMX.google.h264.encoder") }
        )

        for (candidate in candidates) {
            var encoder: MediaCodec? = null
            try {
                encoder = candidate()
                val codecInfo = encoder.codecInfo
                val caps = codecInfo.getCapabilitiesForType(MediaFormat.MIMETYPE_VIDEO_AVC)
                colorFormat = selectSupportedColorFormat(caps)

                val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height).apply {
                    setInteger(MediaFormat.KEY_COLOR_FORMAT, colorFormat)
                    setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
                    setInteger(MediaFormat.KEY_FRAME_RATE, fps)
                    setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)

                    val encCaps = caps.encoderCapabilities
                    if (encCaps != null && encCaps.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)) {
                        setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)
                    } else if (encCaps != null && encCaps.isBitrateModeSupported(MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)) {
                        setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)
                    }
                }

                encoder.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                encoder.start()

                codec = encoder
                isRunning.set(true)
                Log.i(TAG, "Initialized H.264 encoder: ${codecInfo.name} (${width}x${height} @ ${fps}fps, colorFormat=$colorFormat)")

                drainThread = Thread({ drainLoop() }, "pristinecam-h264-drain").also {
                    it.isDaemon = true
                    it.start()
                }
                return
            } catch (e: Exception) {
                Log.w(TAG, "Codec initialization attempt failed: ${e.message}", e)
                try {
                    encoder?.release()
                } catch (_: Exception) {}
            }
        }

        Log.e(TAG, "All H.264 encoder initialization attempts failed!")
    }

    companion object {
        private const val TAG = "H264Encoder"
        // Standard color format constants used by Android hardware & software encoders
        private const val COLOR_FORMAT_NV12 = 21
        private const val COLOR_FORMAT_I420 = 19
    }

    private fun selectSupportedColorFormat(capabilities: MediaCodecInfo.CodecCapabilities): Int {
        val formats = capabilities.colorFormats
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
                    inBuffer.put(nv21, 0, minOf(nv21.size, inBuffer.remaining()))
                }

                encoder.queueInputBuffer(inIndex, 0, totalSize, ptsUs, 0)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error encoding frame", e)
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
                if (outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    try {
                        val newFormat = encoder.outputFormat
                        val sps = newFormat.getByteBuffer("csd-0")
                        val pps = newFormat.getByteBuffer("csd-1")
                        if (sps != null && pps != null) {
                            val spsBytes = ByteArray(sps.remaining()).also { sps.get(it); sps.rewind() }
                            val ppsBytes = ByteArray(pps.remaining()).also { pps.get(it); pps.rewind() }
                            spsPpsHeader = spsBytes + ppsBytes
                        }
                    } catch (_: Exception) {}
                } else if (outIndex >= 0) {
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
                    Log.e(TAG, "Error in drain loop", e)
                }
                break
            }
        }
    }

    private fun containsSps(data: ByteArray): Boolean {
        var i = 0
        while (i < data.size - 4) {
            if (data[i] == 0.toByte() && data[i + 1] == 0.toByte()) {
                val nalStart = if (data[i + 2] == 1.toByte()) {
                    i + 3
                } else if (data[i + 2] == 0.toByte() && i + 3 < data.size && data[i + 3] == 1.toByte()) {
                    i + 4
                } else {
                    -1
                }
                if (nalStart != -1 && nalStart < data.size) {
                    val nalType = data[nalStart].toInt() and 0x1F
                    if (nalType == 7) return true
                }
            }
            i++
        }
        return false
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
