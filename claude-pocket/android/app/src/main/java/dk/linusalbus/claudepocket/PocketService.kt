package dk.linusalbus.claudepocket

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import java.net.HttpURLConnection
import java.net.URL

/**
 * Holds one live connection to the relay's event stream (like a chat app does) and
 * turns new approvals and messages into notifications, even when the app is closed.
 */
class PocketService : Service() {

    @Volatile private var running = true
    @Volatile private var conn: HttpURLConnection? = null
    private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Notifier.ensureChannels(this)
        val n = Notifier.status(this, "Connecting…")
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(Notifier.STATUS_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(Notifier.STATUS_ID, n)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (worker == null) worker = Thread(::loop, "pocket-stream").also { it.start() }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        conn?.disconnect()
        super.onDestroy()
    }

    private fun loop() {
        var backoff = 2000L
        while (running) {
            val p = Pairing(this)
            if (!p.paired) { stopSelf(); return }
            try {
                check(p)
                listen(p)
                backoff = 2000L
            } catch (e: RelayError) {
                if (e.code == 401) {
                    Notifier.updateStatus(this, "Token rejected — open the app to pair again")
                    backoff = 5 * 60_000L
                } else {
                    Notifier.updateStatus(this, "Reconnecting…")
                }
            } catch (e: Exception) {
                Notifier.updateStatus(this, "Offline — is your Mac awake?")
            }
            if (!running) return
            Thread.sleep(backoff)
            backoff = (backoff * 2).coerceAtMost(60_000L)
        }
    }

    /** Fetches the full state and lets the notifier diff it against what it already showed. */
    private fun check(p: Pairing) {
        Notifier.process(this, p, Relay.request(p, "GET", "/api/state"))
    }

    private fun listen(p: Pairing) {
        val c = URL("${p.url}/api/events?token=${Relay.enc(p.token!!)}").openConnection() as HttpURLConnection
        conn = c
        c.connectTimeout = 15000
        c.readTimeout = 70000 // the relay pings every 20 s
        c.setRequestProperty("Accept", "text/event-stream")
        if (c.responseCode == 401) throw RelayError(401, "")
        if (c.responseCode >= 400) throw RelayError(c.responseCode, "")
        Notifier.updateStatus(this, "Connected")
        c.inputStream.bufferedReader().use { r ->
            var event = ""
            while (running) {
                val line = r.readLine() ?: break
                when {
                    line.startsWith("event:") -> event = line.substring(6).trim()
                    line.isEmpty() -> {
                        if (event == "update") check(p)
                        event = ""
                    }
                }
            }
        }
        c.disconnect()
    }
}
