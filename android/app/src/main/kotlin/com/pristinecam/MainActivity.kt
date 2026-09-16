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
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CameraAlt
import androidx.compose.material.icons.filled.Cameraswitch
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.zIndex
import kotlin.OptIn
import androidx.annotation.OptIn as AndroidXOptIn
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
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
        if (granted) bindToService() else finish()
    }

    // ── Activity lifecycle ──────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        checkAndRequestPermission()
        setContent {
            androidx.compose.runtime.CompositionLocalProvider(LocalLifecycleOwner provides this) {
                PristineCamTheme {
                    val svc = service
                    if (svc != null) {
                        MainScreen(service = svc, activity = this)
                    } else {
                        SplashScreen()
                    }
                }
            }
        }
    }

    override fun onStop() {
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
        val intent = Intent(this, StreamingService::class.java)
        ContextCompat.startForegroundService(this, intent)
        bindService(intent, serviceConnection, BIND_AUTO_CREATE)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Composables
// ─────────────────────────────────────────────────────────────────────────────

@OptIn(ExperimentalMaterial3Api::class)
@AndroidXOptIn(ExperimentalCamera2Interop::class)
@Composable
private fun MainScreen(service: StreamingService, activity: ComponentActivity) {
    val isStreaming       by service.isStreaming.collectAsStateWithLifecycle()
    val streamUrl         by service.streamUrl.collectAsStateWithLifecycle()
    val currentRes        by service.resolution.collectAsStateWithLifecycle()
    val screenSaverActive by service.isScreenSaverActive.collectAsStateWithLifecycle()
    val torchEnabled      by service.torchEnabled.collectAsStateWithLifecycle()
    val exposureIndex     by service.exposureIndex.collectAsStateWithLifecycle()
    val wbMode            by service.wbMode.collectAsStateWithLifecycle()

    var showPreview          by remember { mutableStateOf(true) }
    var isFrontCamera        by remember { mutableStateOf(false) }
    var showOptions          by remember { mutableStateOf(false) }
    var showCameraControls   by remember { mutableStateOf(false) }   // per-session option, default OFF
    val previewView          = remember { PreviewView(activity) }

    // Attach/detach CameraX Preview.
    LaunchedEffect(showPreview, isStreaming) {
        service.attachPreview(if (showPreview && isStreaming) previewView else null)
    }
    DisposableEffect(Unit) {
        onDispose { service.attachPreview(null) }
    }

    // Brightness management.
    SideEffect {
        activity.window?.let { w ->
            if (isStreaming || screenSaverActive) {
                w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            } else {
                w.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
            w.attributes = w.attributes.apply {
                screenBrightness = when {
                    screenSaverActive -> 0.0f
                    isStreaming       -> 0.01f
                    else              -> WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
                }
            }
        }
    }

    val btnColor by animateColorAsState(
        targetValue  = if (isStreaming) Color(0xFFE53935) else Color(0xFF00897B),
        animationSpec = tween(300),
        label        = "btnColor"
    )

    // Wrap everything in a Box so we can layer the OLED overlay on top
    // WITHOUT removing the rest of the UI (including the camera preview surface)
    // from the composition tree. Removing the AndroidView via early return caused
    // the PreviewView to lose its window and release its surface, stalling CameraX.
    Box(modifier = Modifier.fillMaxSize()) {

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
                imageVector        = Icons.Default.CameraAlt,
                contentDescription = null,
                tint               = MaterialTheme.colorScheme.primary,
                modifier           = Modifier.size(22.dp)
            )
            Spacer(Modifier.width(10.dp))
            Text(
                text  = "PristineCam",
                style = MaterialTheme.typography.titleLarge,
            )
            Spacer(Modifier.weight(1f))
            // Live indicator
            if (isStreaming) {
                Surface(
                    shape = RoundedCornerShape(50),
                    color = Color(0xFF4CAF50).copy(alpha = 0.15f)
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
                        verticalAlignment    = Alignment.CenterVertically,
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
                Spacer(Modifier.width(8.dp))
            }
            // ⚙ Options button
            IconButton(onClick = { showOptions = true }) {
                Icon(
                    imageVector        = Icons.Default.Settings,
                    contentDescription = "Options",
                    tint               = MaterialTheme.colorScheme.onSurfaceVariant
                )
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
                    modifier = Modifier
                        .fillMaxSize()
                        .pointerInput(Unit) {
                            detectTapGestures { offset ->
                                val point = previewView.meteringPointFactory.createPoint(
                                    offset.x, offset.y
                                )
                                service.tapToFocus(point)
                            }
                        }
                )
            } else {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Icon(
                        imageVector        = Icons.Default.CameraAlt,
                        contentDescription = null,
                        tint               = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.3f),
                        modifier           = Modifier.size(52.dp)
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

        // ── Controls ──────────────────────────────────────────────────────────────
        Column(
            modifier = Modifier.padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {

            // URL card
            Surface(
                modifier       = Modifier.fillMaxWidth(),
                shape          = RoundedCornerShape(14.dp),
                color          = MaterialTheme.colorScheme.surfaceVariant,
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
                        style = MaterialTheme.typography.bodyMedium,
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
                        activity,
                        Intent(activity, StreamingService::class.java).apply { this.action = action }
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

            // Preview toggle + Camera flip row
            Row(
                modifier              = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                OutlinedButton(
                    onClick  = { showPreview = !showPreview },
                    modifier = Modifier.weight(1f).height(48.dp),
                    shape    = RoundedCornerShape(12.dp)
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

            // ── Advanced camera controls (shown only when enabled in Options) ─────
            if (showCameraControls && isStreaming) {
                Surface(
                    modifier       = Modifier.fillMaxWidth(),
                    shape          = RoundedCornerShape(14.dp),
                    color          = MaterialTheme.colorScheme.surfaceVariant,
                    tonalElevation = 1.dp
                ) {
                    Column(
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Text(
                            "Camera Controls",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.primary
                        )

                        // Torch
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween,
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text("Torch", style = MaterialTheme.typography.bodyMedium)
                            Switch(
                                checked         = torchEnabled,
                                onCheckedChange = { service.setTorch(it) }
                            )
                        }

                        // Exposure
                        Text("Exposure", style = MaterialTheme.typography.bodyMedium)
                        Slider(
                            value         = exposureIndex.toFloat(),
                            onValueChange = { service.stepExposure(it.toInt()) },
                            valueRange    = -4f..4f,
                            steps         = 7
                        )

                        // White balance
                        val wbOptions = listOf("AUTO", "DAYLIGHT", "CLOUDY", "FLUORESCENT", "INCANDESCENT")
                        var wbExpanded by remember { mutableStateOf(false) }
                        Box {
                            OutlinedButton(
                                onClick        = { wbExpanded = true },
                                shape          = RoundedCornerShape(10.dp),
                                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 6.dp),
                                modifier       = Modifier.fillMaxWidth()
                            ) {
                                Text("WB: $wbMode", style = MaterialTheme.typography.labelMedium)
                            }
                            DropdownMenu(
                                expanded        = wbExpanded,
                                onDismissRequest = { wbExpanded = false }
                            ) {
                                wbOptions.forEach { opt ->
                                    DropdownMenuItem(
                                        text    = { Text(opt) },
                                        onClick = {
                                            service.setWhiteBalance(opt)
                                            wbExpanded = false
                                        }
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
        }   // end Column

        // ── OLED saver overlay — layered on top of the Column.
        // The Column (and its AndroidView/PreviewView) stays in the composition
        // tree the whole time, keeping the CameraX surface alive.
        if (screenSaverActive) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color.Black)
                    .pointerInput(Unit) {
                        detectTapGestures { service.dismissScreenSaver() }
                    }
            )
        }

    }   // end Box

    // ── Options bottom sheet ───────────────────────────────────────────────────
    if (showOptions) {
        val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
        ModalBottomSheet(
            onDismissRequest = { showOptions = false },
            sheetState       = sheetState,
            shape            = RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp)
        ) {
            OptionsSheet(
                currentRes          = currentRes,
                showCameraControls  = showCameraControls,
                onResolutionChange  = { service.setResolution(it) },
                onCameraControlsToggle = { showCameraControls = it },
                onDismiss           = { showOptions = false }
            )
        }
    }
}

// ── Options bottom sheet content ──────────────────────────────────────────────

@Composable
private fun OptionsSheet(
    currentRes: StreamingService.StreamResolution,
    showCameraControls: Boolean,
    onResolutionChange: (StreamingService.StreamResolution) -> Unit,
    onCameraControlsToggle: (Boolean) -> Unit,
    onDismiss: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp)
            .padding(bottom = 32.dp),
        verticalArrangement = Arrangement.spacedBy(0.dp)
    ) {
        Text(
            text  = "Options",
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.padding(bottom = 20.dp)
        )

        // ── Stream Quality ─────────────────────────────────────────────────
        Text(
            text  = "Stream Quality",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(bottom = 10.dp)
        )

        StreamingService.StreamResolution.entries.forEach { res ->
            val selected = res == currentRes
            Surface(
                onClick  = { onResolutionChange(res); onDismiss() },
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 3.dp),
                shape          = RoundedCornerShape(12.dp),
                color          = if (selected)
                    MaterialTheme.colorScheme.primaryContainer
                else
                    MaterialTheme.colorScheme.surfaceVariant,
                tonalElevation = if (selected) 2.dp else 0.dp
            ) {
                Row(
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 14.dp),
                    verticalAlignment    = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    Column {
                        Text(
                            text      = res.label,
                            style     = MaterialTheme.typography.bodyMedium,
                            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                            color     = if (selected)
                                MaterialTheme.colorScheme.onPrimaryContainer
                            else
                                MaterialTheme.colorScheme.onSurface
                        )
                        Text(
                            text  = "${res.size.width}\u00d7${res.size.height} \u00b7 30 FPS",
                            style = MaterialTheme.typography.bodySmall,
                            color = if (selected)
                                MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.7f)
                            else
                                MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    if (selected) {
                        Surface(
                            shape = RoundedCornerShape(50),
                            color = MaterialTheme.colorScheme.primary
                        ) {
                            Text(
                                text     = "Active",
                                style    = MaterialTheme.typography.labelSmall,
                                color    = MaterialTheme.colorScheme.onPrimary,
                                modifier = Modifier.padding(horizontal = 10.dp, vertical = 4.dp)
                            )
                        }
                    }
                }
            }
        }

        HorizontalDivider(modifier = Modifier.padding(vertical = 20.dp))

        // ── Advanced Camera Controls toggle ────────────────────────────────
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment    = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text  = "Advanced Camera Controls",
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium
                )
                Text(
                    text  = "Torch, exposure, white balance",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Spacer(Modifier.width(12.dp))
            Switch(
                checked         = showCameraControls,
                onCheckedChange = onCameraControlsToggle
            )
        }
    }
}



// ── Splash screen ─────────────────────────────────────────────────────────────

@Composable
private fun SplashScreen() {
    Box(
        modifier         = Modifier
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
                modifier    = Modifier.size(28.dp),
                color       = MaterialTheme.colorScheme.primary,
                strokeWidth = 2.dp
            )
        }
    }
}
