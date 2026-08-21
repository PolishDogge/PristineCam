package com.pristinecam.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val PristineDarkColors = darkColorScheme(
    primary             = Teal80,
    onPrimary           = Color(0xFF00352E),
    primaryContainer    = Teal20,
    onPrimaryContainer  = Color(0xFFA7F3ED),
    secondary           = Teal60,
    onSecondary         = Color(0xFF003733),
    background          = Surface0,
    onBackground        = OnSurface,
    surface             = Surface0,
    onSurface           = OnSurface,
    surfaceVariant      = Surface2,
    onSurfaceVariant    = Subtle,
    outline             = Color(0xFF3A3F3F),
    error               = Crimson,
    onError             = Color(0xFFFFFFFF)
)

@Composable
fun PristineCamTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = PristineDarkColors,
        typography  = AppTypography,
        content     = content
    )
}
