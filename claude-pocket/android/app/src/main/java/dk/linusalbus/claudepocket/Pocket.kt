package dk.linusalbus.claudepocket

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/** Where the relay lives and the token for it, set once by scanning the pairing QR code. */
class Pairing(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("pocket", Context.MODE_PRIVATE)

    val url: String? get() = prefs.getString("url", null)
    val token: String? get() = prefs.getString("token", null)
    val paired: Boolean get() = url != null && token != null

    /** Accepts claudepocket://pair?url=…&token=… or a https://relay/#token=… link. */
    fun save(link: Uri): Boolean {
        val (url, token) = when (link.scheme) {
            "claudepocket" -> link.getQueryParameter("url") to link.getQueryParameter("token")
            "http", "https" -> {
                val t = (link.fragment ?: "").split('&').firstOrNull { it.startsWith("token=") }?.removePrefix("token=")
                "${link.scheme}://${link.encodedAuthority}" to t
            }
            else -> null to null
        }
        if (url.isNullOrBlank() || token.isNullOrBlank() || !url.startsWith("http")) return false
        prefs.edit().putString("url", url.trimEnd('/')).putString("token", token).apply()
        return true
    }

    fun clear() = prefs.edit().clear().apply()

    // Notification bookkeeping, kept next to the pairing so a re-pair starts fresh.
    var seenRequests: Set<String>
        get() = prefs.getStringSet("seenRequests", emptySet()) ?: emptySet()
        set(v) = prefs.edit().putStringSet("seenRequests", v).apply()
    var lastMessageTs: Long
        get() = prefs.getLong("lastMessageTs", 0)
        set(v) = prefs.edit().putLong("lastMessageTs", v).apply()
    var offeredVersion: Long
        get() = prefs.getLong("offeredVersion", 0)
        set(v) = prefs.edit().putLong("offeredVersion", v).apply()
    var primed: Boolean
        get() = prefs.getBoolean("primed", false)
        set(v) = prefs.edit().putBoolean("primed", v).apply()
}

/** Tiny blocking HTTP client for the relay API. Call off the main thread. */
object Relay {
    fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

    fun request(p: Pairing, method: String, path: String, body: JSONObject? = null): JSONObject {
        val c = URL(p.url + path).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 15000
            c.readTimeout = 20000
            c.setRequestProperty("Authorization", "Bearer ${p.token}")
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = c.responseCode
            val text = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.use { it.readText() } ?: ""
            if (code >= 400) throw RelayError(code, text)
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        } finally {
            c.disconnect()
        }
    }
}

class RelayError(val code: Int, message: String) : Exception("HTTP $code $message")

fun startPocketService(context: Context) {
    if (!Pairing(context).paired) return
    val i = Intent(context, PocketService::class.java)
    if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
}
