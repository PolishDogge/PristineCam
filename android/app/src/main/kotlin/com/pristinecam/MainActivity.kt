package com.pristinecam

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.IBinder
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.view.PreviewView
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CameraAlt
import androidx.compose.material.icons.filled.Cameraswitch
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.DpOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.pristinecam.ui.theme.PristineCamTheme

class MainActivity : ComponentActivity() {

    /** Non-null once the service is bound; Compose reacts to changes automatically. */
    private var service by mutableStateOf<StreamingService?>(null)
    private var serviceBound = false

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            service = (binder as StreamingService.LocalBinder).service()
            serviceBound = true
        }
        override fun onServiceDisconnected(name: ComponentName) {
            service = null
            serviceBound = false
        }
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) bindToService() else finish() // can't operate without camera
    }

    // ── Activity lifecycle ──────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        enableEdgeToEdge()
        checkAndRequestPermission()
        setContent {
            CompositionLocalProvider(LocalLifecycleOwner provides this) {
                PristineCamTheme {
                    val svc = service
                    if (svc != null) {
                        MainScreen(service = svc)
                    } else {
                        SplashScreen()
                    }
                }
            }
        }
    }

    override fun onStop() {
        // Detach the preview surface when the Activity leaves the screen.
        // The camera and HTTP server keep running in the service.
        service?.attachPreview(null)
        super.onStop()
    }

    override fun onDestroy() {
        if (serviceBound) {
            service?.attachPreview(null)
            unbindService(serviceConnection)
            serviceBound = false
        }
        super.onDestroy()
    }

    // ── Permission + binding ────────────────────────────────────────────────────────

    private fun checkAndRequestPermission() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            bindToService()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun bindToService() {
        // Start the service so it outlives the activity binding,
        // then bind to obtain a reference to the service object.
        val intent = Intent(this, StreamingService::class.java)
        ContextCompat.startForegroundService(this, intent)
        bindService(intent, serviceConnection, BIND_AUTO_CREATE)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Composables
// ─────────────────────────────────────────────────────────────────────────────

@Composable
private fun MainScreen(service: StreamingService) {
    val context      = LocalContext.current
    val isStreaming  by service.isStreaming.collectAsStateWithLifecycle()
    val streamUrl    by service.streamUrl.collectAsStateWithLifecycle()
    val currentRes   by service.resolution.collectAsStateWithLifecycle()

    var showPreview   by remember { mutableStateOf(true) }
    var isFrontCamera by remember { mutableStateOf(false) }
    val previewView   = remember { PreviewView(context) }

    // Attach/detach the CameraX Preview use case when relevant state changes.
    LaunchedEffect(showPreview, isStreaming) {
        service.attachPreview(if (showPreview && isStreaming) previewView else null)
    }
    DisposableEffect(Unit) {
        onDispose { service.attachPreview(null) }
    }

    // Manage screen wake / brightness based on streaming state.
    // • Streaming  → keep screen on but dim it (saves battery, stream continues).
    // • Idle       → restore normal brightness and allow the OS to lock the screen.
    val window = (context as? ComponentActivity)?.window
    SideEffect {
        window?.let { w ->
            if (isStreaming) {
                // Keep screen alive so the camera service isn't throttled by Doze,
                // but dim the display to save battery.
                w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                w.attributes = w.attributes.apply {
                    screenBrightness = 0.01f
                }
            } else {
                // Let Android's normal timeout and lock apply.
                w.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                w.attributes = w.attributes.apply {
                    screenBrightness = WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
                }
            }
        }
    }

    val btnColor by animateColorAsState(
        targetValue = if (isStreaming) Color(0xFFE53935) else Color(0xFF00897B),
        animationSpec = tween(300),
        label = "btnColor"
    )

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .statusBarsPadding()
            .navigationBarsPadding()
    ) {

        // ── App bar ────────────────────────────────────────────────────────────────
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 20.dp, vertical = 18.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                imageVector    = Icons.Default.CameraAlt,
                contentDescription = null,
                tint           = MaterialTheme.colorScheme.primary,
                modifier       = Modifier.size(22.dp)
            )
            Spacer(Modifier.width(10.dp))
            Text(
                text  = "PristineCam",
                style = MaterialTheme.typography.titleLarge,
            )
            Spacer(Modifier.weight(1f))
            // Live indicator dot
            if (isStreaming) {
                Surface(
                    shape = RoundedCornerShape(50),
                    color = Color(0xFF4CAF50).copy(alpha = 0.15f)
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(5.dp)
                    ) {
                        Surface(
                            modifier = Modifier.size(7.dp),
                            shape    = RoundedCornerShape(50),
                            color    = Color(0xFF4CAF50)
                        ) {}
                        Text(
                            text  = "LIVE",
                            style = MaterialTheme.typography.labelSmall,
                            color = Color(0xFF4CAF50)
                        )
                    }
                }
            }
        }

        // ── Camera preview ─────────────────────────────────────────────────────────
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp)
                .aspectRatio(16f / 9f)
                .clip(RoundedCornerShape(20.dp))
                .background(Color(0xFF111316)),
            contentAlignment = Alignment.Center
        ) {
            if (showPreview && isStreaming) {
                AndroidView(
                    factory  = { previewView },
                    modifier = Modifier.fillMaxSize()
                )
            } else {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector    = Icons.Default.CameraAlt,
                        contentDescription = null,
                        tint           = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.3f),
                        modifier       = Modifier.size(52.dp)
                    )
                    Text(
                        text  = when {
                            !isStreaming -> "Tap \u201cStart Streaming\u201d to begin"
                            else         -> "Preview is hidden"
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.4f)
                    )
                }
            }
        }

        Spacer(Modifier.height(20.dp))

        // ── Controls ─────────────────────────────────────────────────────────────────
        Column(
            modifier = Modifier.padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {

            // URL card
            Surface(
                modifier      = Modifier.fillMaxWidth(),
                shape         = RoundedCornerShape(14.dp),
                color         = MaterialTheme.colorScheme.surfaceVariant,
                tonalElevation = 1.dp
            ) {
                Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 14.dp)) {
                    Text(
                        text  = if (isStreaming) "Connect your PC to:" else "Stream address",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        text  = streamUrl ?: "Start streaming to see the URL",
                        style = MaterialTheme.typography.bodyMedium.copy(
                            fontFamily = if (streamUrl != null) FontFamily.Monospace else FontFamily.Default
                        ),
                        color = if (streamUrl != null)
                            MaterialTheme.colorScheme.onSurface
                        else
                            MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }

            // Start / Stop button
            Button(
                onClick = {
                    val action = if (isStreaming) StreamingService.ACTION_STOP
                                 else             StreamingService.ACTION_START
                    ContextCompat.startForegroundService(
                        context,
                        Intent(context, StreamingService::class.java).apply { this.action = action }
                    )
                },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(54.dp),
                shape  = RoundedCornerShape(14.dp),
                colors = ButtonDefaults.buttonColors(containerColor = btnColor)
            ) {
                Text(
                    text  = if (isStreaming) "Stop Streaming" else "Start Streaming",
                    style = MaterialTheme.typography.labelLarge
                )
            }

            // Preview toggle + Camera flip + Settings
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                OutlinedButton(
                    onClick   = { showPreview = !showPreview },
                    modifier  = Modifier.weight(1f).height(48.dp),
                    shape     = RoundedCornerShape(12.dp)
                ) {
                    Icon(
                        imageVector = if (showPreview) Icons.Default.Visibility
                                      else             Icons.Default.VisibilityOff,
                        contentDescription = null,
                        modifier = Modifier.size(16.dp)
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(
                        text  = if (showPreview) "Preview On" else "Preview Off",
                        style = MaterialTheme.typography.labelMedium
                    )
                }

                OutlinedButton(
                    onClick  = {
                        isFrontCamera = !isFrontCamera
                        service.setLensFacing(isFrontCamera)
                    },
                    modifier = Modifier.weight(1f).height(48.dp),
                    shape    = RoundedCornerShape(12.dp)
                ) {
                    Icon(
                        imageVector        = Icons.Default.Cameraswitch,
                        contentDescription = null,
                        modifier           = Modifier.size(16.dp)
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(
                        text  = if (isFrontCamera) "Front" else "Back",
                        style = MaterialTheme.typography.labelMedium
                    )
                }
            }

            // ── Resolution selector row ──────────────────────────────────────
            ResolutionSelector(
                current   = currentRes,
                onSelect  = { service.setResolution(it) }
            )
        }
    }
}

// ── Resolution selector component ────────────────────────────────────────────

@Composable
private fun ResolutionSelector(
    current: StreamingService.StreamResolution,
    onSelect: (StreamingService.StreamResolution) -> Unit,
) {
    var expanded by remember { mutableStateOf(false) }

    Surface(
        modifier      = Modifier.fillMaxWidth(),
        shape         = RoundedCornerShape(14.dp),
        color         = MaterialTheme.colorScheme.surfaceVariant,
        tonalElevation = 1.dp
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(
                imageVector        = Icons.Default.Settings,
                contentDescription = null,
                modifier           = Modifier.size(18.dp),
                tint               = MaterialTheme.colorScheme.primary
            )
            Spacer(Modifier.width(10.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text  = "Stream Resolution",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary
                )
                Spacer(Modifier.height(2.dp))
                Text(
                    text  = "${current.label}  (${current.size.width}×${current.size.height})",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurface
                )
            }

            Box {
                OutlinedButton(
                    onClick = { expanded = true },
                    shape   = RoundedCornerShape(10.dp),
                    contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp)
                ) {
                    Text(
                        text  = "Change",
                        style = MaterialTheme.typography.labelMedium
                    )
                }

                DropdownMenu(
                    expanded       = expanded,
                    onDismissRequest = { expanded = false },
                    offset         = DpOffset(0.dp, 4.dp)
                ) {
                    StreamingService.StreamResolution.entries.forEach { res ->
                        DropdownMenuItem(
                            text = {
                                Row(
                                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Text(
                                        text  = res.label,
                                        style = MaterialTheme.typography.bodyMedium,
                                        fontWeight = if (res == current) FontWeight.Bold else FontWeight.Normal
                                    )
                                    Text(
                                        text  = "${res.size.width}×${res.size.height}",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant
                                    )
                                }
                            },
                            onClick = {
                                onSelect(res)
                                expanded = false
                            }
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun SplashScreen() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(20.dp)
        ) {
            Icon(
                imageVector        = Icons.Default.CameraAlt,
                contentDescription = null,
                tint               = MaterialTheme.colorScheme.primary,
                modifier           = Modifier.size(64.dp)
            )
            Text(
                text       = "PristineCam",
                style      = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.SemiBold
            )
            CircularProgressIndicator(
                modifier  = Modifier.size(28.dp),
                color     = MaterialTheme.colorScheme.primary,
                strokeWidth = 2.dp
            )
        }
    }
}
