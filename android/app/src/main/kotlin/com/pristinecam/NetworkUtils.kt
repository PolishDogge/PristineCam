package com.pristinecam

import java.net.NetworkInterface

/**
 * Utilities for discovering the device's local network address.
 * No external network calls are made anywhere in this codebase.
 */
object NetworkUtils {

    /**
     * Returns the first non-loopback IPv4 address found on any active network
     * interface (Wi-Fi, USB-tethering, etc.), or null if the device is offline.
     */
    fun getLocalIpAddress(): String? = try {
        NetworkInterface.getNetworkInterfaces()
            ?.asSequence()
            ?.filter { iface -> iface.isUp && !iface.isLoopback }
            ?.sortedByDescending { it.name.startsWith("wlan") || it.name.startsWith("rndis") || it.name.startsWith("usb") }
            ?.flatMap { iface ->
                iface.inetAddresses.asSequence().filter { addr ->
                    !addr.isLoopbackAddress
                        && addr.hostAddress?.contains(':') == false  // exclude IPv6
                }
            }
            ?.firstOrNull()
            ?.hostAddress
    } catch (_: Exception) { null }
}
