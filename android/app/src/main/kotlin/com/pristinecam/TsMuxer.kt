package com.pristinecam

import java.io.ByteArrayOutputStream

/**
 * Lightweight in-memory MPEG-TS packetizer for H.264 video streams.
 *
 * Wraps H.264 Annex-B NAL units into standard 188-byte MPEG-TS packets
 * (ITU-T H.222.0 / ISO/IEC 13818-1).
 *
 * Supported streams:
 *   - Video: H.264 / AVC (Stream Type 0x1B, PID 0x0100)
 *   - PAT: Program Association Table (PID 0x0000)
 *   - PMT: Program Map Table (PID 0x1000)
 */
class TsMuxer {

    private var patCc: Int = 0
    private var pmtCc: Int = 0
    private var videoCc: Int = 0

    private val tsPacketSize = 188
    private val maxPayloadPerTs = 184 // 188 - 4 header bytes

    companion object {
        private const val SYNC_BYTE: Byte = 0x47
        private const val VIDEO_PID = 0x0100
        private const val PMT_PID   = 0x1000
        private const val PAT_PID   = 0x0000
        private const val STREAM_TYPE_H264: Byte = 0x1B
        private const val PES_STREAM_ID_VIDEO: Byte = 0xE0.toByte()

        // MPEG-2 CRC32 Table
        private val crcTable = IntArray(256) { i ->
            var crc = i shl 24
            for (j in 0 until 8) {
                crc = if ((crc and 0x80000000.toInt()) != 0) {
                    (crc shl 1) xor 0x04C11DB7
                } else {
                    crc shl 1
                }
            }
            crc
        }

        fun calculateCrc32(data: ByteArray, offset: Int, length: Int): Int {
            var crc = 0xFFFFFFFF.toInt()
            for (i in offset until (offset + length)) {
                val b = data[i].toInt() and 0xFF
                crc = (crc shl 8) xor crcTable[((crc ushr 24) xor b) and 0xFF]
            }
            return crc
        }
    }

    /**
     * Creates a 188-byte PAT (Program Association Table) TS packet.
     */
    fun createPat(): ByteArray {
        val packet = ByteArray(tsPacketSize)
        // 4-byte TS header: sync, payload_unit_start=1, PID=0, adaptation=01 (payload only), cc
        packet[0] = SYNC_BYTE
        packet[1] = 0x40.toByte() // payload unit start indicator = 1, PID high = 0
        packet[2] = 0x00.toByte() // PID low = 0
        packet[3] = (0x10 or (patCc and 0x0F)).toByte()
        patCc = (patCc + 1) and 0x0F

        // Pointer field
        packet[4] = 0x00

        // Table Syntax (12 bytes without CRC)
        val section = ByteArray(16)
        section[0] = 0x00 // table_id = 0 (PAT)
        section[1] = 0xB0.toByte() // section_syntax_indicator=1, section_length high
        section[2] = 0x0D.toByte() // section_length = 13 bytes to follow
        section[3] = 0x00 // transport_stream_id high
        section[4] = 0x01 // transport_stream_id low
        section[5] = 0xC1.toByte() // version=0, current_next=1
        section[6] = 0x00 // section_number = 0
        section[7] = 0x00 // last_section_number = 0
        // Program 1 -> PMT PID 0x1000
        section[8] = 0x00
        section[9] = 0x01 // program_number = 1
        section[10] = (0xE0 or ((PMT_PID shr 8) and 0x1F)).toByte()
        section[11] = (PMT_PID and 0xFF).toByte()

        // CRC32 over bytes 0..11
        val crc = calculateCrc32(section, 0, 12)
        section[12] = (crc ushr 24).toByte()
        section[13] = (crc ushr 16).toByte()
        section[14] = (crc ushr 8).toByte()
        section[15] = crc.toByte()

        System.arraycopy(section, 0, packet, 5, section.size)
        // Pad the remainder with 0xFF
        for (i in (5 + section.size) until tsPacketSize) {
            packet[i] = 0xFF.toByte()
        }
        return packet
    }

    /**
     * Creates a 188-byte PMT (Program Map Table) TS packet.
     */
    fun createPmt(): ByteArray {
        val packet = ByteArray(tsPacketSize)
        packet[0] = SYNC_BYTE
        packet[1] = (0x40 or ((PMT_PID shr 8) and 0x1F)).toByte()
        packet[2] = (PMT_PID and 0xFF).toByte()
        packet[3] = (0x10 or (pmtCc and 0x0F)).toByte()
        pmtCc = (pmtCc + 1) and 0x0F

        // Pointer field
        packet[4] = 0x00

        // Table Syntax (17 bytes syntax + 4 bytes CRC = 21 bytes)
        val section = ByteArray(17)
        section[0] = 0x02 // table_id = 2 (PMT)
        section[1] = 0xB0.toByte()
        section[2] = 0x12.toByte() // section_length = 18 bytes to follow (14 bytes syntax + 4 bytes CRC)
        section[3] = 0x00
        section[4] = 0x01 // program_number = 1
        section[5] = 0xC1.toByte() // version=0, current_next=1
        section[6] = 0x00
        section[7] = 0x00
        // PCR PID = Video PID
        section[8] = (0xE0 or ((VIDEO_PID shr 8) and 0x1F)).toByte()
        section[9] = (VIDEO_PID and 0xFF).toByte()
        section[10] = 0xF0.toByte() // program_info_length = 0
        section[11] = 0x00

        // Stream 1: H.264
        section[12] = STREAM_TYPE_H264
        section[13] = (0xE0 or ((VIDEO_PID shr 8) and 0x1F)).toByte()
        section[14] = (VIDEO_PID and 0xFF).toByte()
        section[15] = 0xF0.toByte() // ES info length = 0
        section[16] = 0x00

        val crc = calculateCrc32(section, 0, 17)
        val fullSection = ByteArray(17 + 4)
        System.arraycopy(section, 0, fullSection, 0, 17)
        fullSection[17] = (crc ushr 24).toByte()
        fullSection[18] = (crc ushr 16).toByte()
        fullSection[19] = (crc ushr 8).toByte()
        fullSection[20] = crc.toByte()

        System.arraycopy(fullSection, 0, packet, 5, fullSection.size)
        for (i in (5 + fullSection.size) until tsPacketSize) {
            packet[i] = 0xFF.toByte()
        }
        return packet
    }

    private var framesSincePatPmt: Int = 0

    /**
     * Packages a single H.264 NAL unit into one or more 188-byte MPEG-TS packets.
     * Includes PTS timestamp in the PES header.
     */
    fun muxNal(nalData: ByteArray, ptsUs: Long, isKeyFrame: Boolean): ByteArray {
        val out = ByteArrayOutputStream()

        // For keyframes (IDR / All-Intra) or periodically (~every 30 frames / 1s), emit PAT and PMT tables
        if (isKeyFrame || framesSincePatPmt >= 30) {
            out.write(createPat())
            out.write(createPmt())
            framesSincePatPmt = 0
        } else {
            framesSincePatPmt++
        }

        // Build PES Header with PTS
        // 90 kHz timebase for MPEG-TS: pts90 = ptsUs * 90 / 1000
        val pts90 = (ptsUs * 90 / 1000) and 0x1FFFFFFFFL
        val pesHeader = ByteArray(14)
        pesHeader[0] = 0x00
        pesHeader[1] = 0x00
        pesHeader[2] = 0x01
        pesHeader[3] = PES_STREAM_ID_VIDEO
        
        // PES packet length: 0 = unconstrained video stream length
        pesHeader[4] = 0x00
        pesHeader[5] = 0x00
        
        pesHeader[6] = 0x80.toByte() // Marker bits
        pesHeader[7] = 0x80.toByte() // PTS flag set
        pesHeader[8] = 0x05 // Header data length (5 bytes for PTS)

        // 33-bit PTS encoded into 5 bytes
        pesHeader[9]  = (0x21 or (((pts90 shr 30) and 0x07).toInt() shl 1)).toByte()
        pesHeader[10] = ((pts90 shr 22) and 0xFF).toByte()
        pesHeader[11] = (0x01 or (((pts90 shr 15) and 0x7F).toInt() shl 1)).toByte()
        pesHeader[12] = ((pts90 shr 7) and 0xFF).toByte()
        pesHeader[13] = (0x01 or ((pts90 and 0x7F).toInt() shl 1)).toByte()

        // Total payload for this PES = pesHeader + nalData
        val totalPesLength = pesHeader.size + nalData.size
        var bytesWritten = 0

        var isFirstPacket = true
        while (bytesWritten < totalPesLength) {
            val ts = ByteArray(tsPacketSize)
            val remaining = totalPesLength - bytesWritten

            if (isFirstPacket) {
                // First packet of PES
                ts[0] = SYNC_BYTE
                ts[1] = (0x40 or ((VIDEO_PID shr 8) and 0x1F)).toByte() // payload unit start indicator = 1
                ts[2] = (VIDEO_PID and 0xFF).toByte()

                if (remaining >= maxPayloadPerTs) {
                    ts[3] = (0x10 or (videoCc and 0x0F)).toByte() // Payload only
                    videoCc = (videoCc + 1) and 0x0F
                    writePesSlice(ts, 4, maxPayloadPerTs, pesHeader, nalData, bytesWritten)
                    bytesWritten += maxPayloadPerTs
                } else {
                    // Requires Adaptation Field for padding
                    val padding = maxPayloadPerTs - remaining
                    ts[3] = (0x30 or (videoCc and 0x0F)).toByte() // Adaptation + Payload
                    videoCc = (videoCc + 1) and 0x0F
                    writeAdaptationField(ts, 4, padding)
                    writePesSlice(ts, 4 + padding, remaining, pesHeader, nalData, bytesWritten)
                    bytesWritten += remaining
                }
                isFirstPacket = false
            } else {
                // Continuation packets
                ts[0] = SYNC_BYTE
                ts[1] = ((VIDEO_PID shr 8) and 0x1F).toByte() // payload unit start indicator = 0
                ts[2] = (VIDEO_PID and 0xFF).toByte()

                if (remaining >= maxPayloadPerTs) {
                    ts[3] = (0x10 or (videoCc and 0x0F)).toByte() // Payload only
                    videoCc = (videoCc + 1) and 0x0F
                    writePesSlice(ts, 4, maxPayloadPerTs, pesHeader, nalData, bytesWritten)
                    bytesWritten += maxPayloadPerTs
                } else {
                    // Final packet: pad with adaptation field
                    val padding = maxPayloadPerTs - remaining
                    ts[3] = (0x30 or (videoCc and 0x0F)).toByte() // Adaptation + Payload
                    videoCc = (videoCc + 1) and 0x0F
                    writeAdaptationField(ts, 4, padding)
                    writePesSlice(ts, 4 + padding, remaining, pesHeader, nalData, bytesWritten)
                    bytesWritten += remaining
                }
            }

            out.write(ts)
        }

        return out.toByteArray()
    }

    private fun writeAdaptationField(dest: ByteArray, offset: Int, paddingLen: Int) {
        if (paddingLen <= 0) return
        dest[offset] = (paddingLen - 1).toByte()
        if (paddingLen > 1) {
            dest[offset + 1] = 0x00 // flags = 0
            for (i in 2 until paddingLen) {
                dest[offset + i] = 0xFF.toByte() // Stuffing bytes
            }
        }
    }

    private fun writePesSlice(
        dest: ByteArray,
        destOffset: Int,
        len: Int,
        header: ByteArray,
        nal: ByteArray,
        srcOffset: Int
    ) {
        var written = 0
        var currentSrc = srcOffset

        // If part of header still needs to be written
        if (currentSrc < header.size) {
            val toCopy = Math.min(len - written, header.size - currentSrc)
            System.arraycopy(header, currentSrc, dest, destOffset + written, toCopy)
            written += toCopy
            currentSrc += toCopy
        }

        // Write from nal
        if (written < len) {
            val nalOffset = currentSrc - header.size
            val toCopy = len - written
            System.arraycopy(nal, nalOffset, dest, destOffset + written, toCopy)
        }
    }
}
