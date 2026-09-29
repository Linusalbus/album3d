package dk.linusalbus.claudepocket

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebChromeClient.FileChooserParams
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject

/**
 * The app itself: a full-screen WebView showing the relay's UI, plus the native bits
 * a web page can't do — the share sheet, QR pairing, file picking and notifications.
 */
class MainActivity : Activity() {

    companion object {
        /** True while the app is on screen; the service skips notifications then. */
        @Volatile var visible = false
        private const val PICK_FILES = 42
        private const val MAX_SHARE_BYTES = 40L * 1024 * 1024
    }

    private lateinit var web: WebView
    private lateinit var pairing: Pairing
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pageReady = false

    // Files shared from other apps, waiting for the page to pick them up.
    private val shared = JSONArray()
    private var sharedText = ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pairing = Pairing(this)

        web = WebView(this)
        setContentView(web)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = true
        }
        web.addJavascriptInterface(Bridge(), "PocketNative")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                if (uri.scheme == "claudepocket") { pair(uri); return true }
                val relayHost = pairing.url?.let { Uri.parse(it).host }
                if (uri.scheme == "file" || (relayHost != null && uri.host == relayHost)) return false
                // Anything else (tracking pages, links in messages) opens in the browser.
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, uri)) }
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                pageReady = true
                deliverShared()
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                val pick = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    putExtra(Intent.EXTRA_ALLOW_MULTIPLE, params.mode == FileChooserParams.MODE_OPEN_MULTIPLE)
                }
                return try {
                    startActivityForResult(Intent.createChooser(pick, null), PICK_FILES)
                    true
                } catch (e: Exception) {
                    fileCallback = null
                    false
                }
            }
        }

        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }

        val route = handleIntent(intent)
        load(route)
        startPocketService(this)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val route = handleIntent(intent)
        if (route != null && pageReady && pairing.paired) {
            web.evaluateJavascript("location.hash=${JSONObject.quote(route)}", null)
        }
    }

    override fun onResume() { super.onResume(); visible = true }
    override fun onPause() { super.onPause(); visible = false }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != PICK_FILES) return
        val uris = mutableListOf<Uri>()
        if (resultCode == RESULT_OK && data != null) {
            val clip = data.clipData
            if (clip != null) for (i in 0 until clip.itemCount) uris.add(clip.getItemAt(i).uri)
            else data.data?.let { uris.add(it) }
        }
        fileCallback?.onReceiveValue(uris.toTypedArray())
        fileCallback = null
    }

    private fun load(route: String?) {
        pageReady = false
        if (!pairing.paired) {
            web.loadUrl("file:///android_asset/setup.html")
            return
        }
        val go = route?.let { "&go=" + Uri.encode(it.removePrefix("#")) } ?: ""
        web.loadUrl("${pairing.url}/#token=${Uri.encode(pairing.token)}$go")
    }

    private fun pair(uri: Uri): Boolean {
        val ok = pairing.save(uri)
        if (ok) {
            pairing.primed = false
            Toast.makeText(this, "Paired with your Mac", Toast.LENGTH_SHORT).show()
            startPocketService(this)
            load(null)
        } else {
            Toast.makeText(this, "That is not a Claude Pocket pairing link", Toast.LENGTH_LONG).show()
        }
        return ok
    }

    /** Returns a route (e.g. "#/m/ideas") to open, if the intent asked for one. */
    private fun handleIntent(intent: Intent?): String? {
        intent ?: return null
        when (intent.action) {
            Intent.ACTION_VIEW -> intent.data?.let { if (it.scheme == "claudepocket") pair(it) }
            Intent.ACTION_SEND, Intent.ACTION_SEND_MULTIPLE -> {
                readShare(intent)
                return "#/send"
            }
        }
        if (intent.getBooleanExtra("update", false)) {
            intent.removeExtra("update")
            Updater.start(this)
        }
        return intent.getStringExtra("route")
    }

    @Suppress("DEPRECATION")
    private fun readShare(intent: Intent) {
        val uris = mutableListOf<Uri>()
        if (intent.action == Intent.ACTION_SEND) {
            (intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))?.let { uris.add(it) }
        } else {
            intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.addAll(it) }
        }
        sharedText = listOfNotNull(intent.getStringExtra(Intent.EXTRA_SUBJECT), intent.getStringExtra(Intent.EXTRA_TEXT))
            .joinToString("\n")
        Thread {
            for (uri in uris) {
                try {
                    var name = "shared"
                    var size = -1L
                    contentResolver.query(uri, null, null, null, null)?.use { c ->
                        if (c.moveToFirst()) {
                            c.getColumnIndex(OpenableColumns.DISPLAY_NAME).takeIf { it >= 0 }?.let { name = c.getString(it) ?: name }
                            c.getColumnIndex(OpenableColumns.SIZE).takeIf { it >= 0 }?.let { size = c.getLong(it) }
                        }
                    }
                    if (size > MAX_SHARE_BYTES) {
                        runOnUiThread { Toast.makeText(this, "$name is too big to send (max 40 MB)", Toast.LENGTH_LONG).show() }
                        continue
                    }
                    val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: continue
                    val type = contentResolver.getType(uri) ?: intent.type ?: "application/octet-stream"
                    synchronized(shared) {
                        shared.put(JSONObject().put("name", name).put("type", type).put("b64", Base64.encodeToString(bytes, Base64.NO_WRAP)))
                    }
                } catch (e: Exception) {
                    runOnUiThread { Toast.makeText(this, "Could not read a shared file", Toast.LENGTH_SHORT).show() }
                }
            }
            runOnUiThread { deliverShared() }
        }.start()
    }

    private fun deliverShared() {
        val has = synchronized(shared) { shared.length() > 0 } || sharedText.isNotEmpty()
        if (pageReady && has) web.evaluateJavascript("window.pocketReceiveShare && pocketReceiveShare()", null)
    }

    /** Exposed to the page as window.PocketNative. */
    inner class Bridge {
        @JavascriptInterface fun isNative(): Boolean = true

        @JavascriptInterface fun version(): String = runCatching {
            packageManager.getPackageInfo(packageName, 0).versionName
        }.getOrNull() ?: ""

        @JavascriptInterface fun versionCode(): Long = Updater.installedVersion(this@MainActivity)

        @JavascriptInterface fun installUpdate() = runOnUiThread { Updater.start(this@MainActivity) }

        @JavascriptInterface fun takeShared(): String = synchronized(shared) {
            val out = JSONObject().put("files", JSONArray(shared.toString())).put("text", sharedText)
            while (shared.length() > 0) shared.remove(0)
            sharedText = ""
            out.toString()
        }

        @JavascriptInterface fun pair(link: String): Boolean {
            val uri = Uri.parse(link.trim())
            if (!Pairing(this@MainActivity).save(uri)) return false
            runOnUiThread { this@MainActivity.pair(uri) }
            return true
        }

        @JavascriptInterface fun resetPairing() {
            pairing.clear()
            runOnUiThread {
                stopService(Intent(this@MainActivity, PocketService::class.java))
                web.clearHistory()
                load(null)
            }
        }
    }
}
