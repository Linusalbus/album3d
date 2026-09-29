package dk.linusalbus.claudepocket

import android.app.RemoteInput
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.widget.Toast
import org.json.JSONObject

/** Handles Allow / Deny / Done / Reply straight from a notification, without opening the app. */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val p = Pairing(context)
        if (!p.paired) return
        val nid = intent.getIntExtra("nid", 0)
        val typed = RemoteInput.getResultsFromIntent(intent)?.getCharSequence(Notifier.REPLY_KEY)?.toString()?.trim()
        val pending = goAsync()
        Thread {
            try {
                when (intent.getStringExtra("op")) {
                    "answer" -> {
                        Relay.request(p, "POST", "/api/phone/answer/${intent.getStringExtra("id")}",
                            JSONObject().put("answer", JSONObject(intent.getStringExtra("answer") ?: "{}")))
                        Notifier.cancel(context, nid)
                    }
                    "stop-reply" -> if (!typed.isNullOrEmpty()) {
                        Relay.request(p, "POST", "/api/phone/answer/${intent.getStringExtra("id")}",
                            JSONObject().put("answer", JSONObject().put("text", typed)))
                        Notifier.cancel(context, nid)
                    }
                    "thread-reply" -> if (!typed.isNullOrEmpty()) {
                        val thread = intent.getStringExtra("thread")!!
                        Relay.request(p, "POST", "/api/phone/threads/$thread/reply", JSONObject().put("text", typed))
                        Relay.request(p, "POST", "/api/phone/threads/$thread/read")
                        // Show the sent reply in the conversation notification, like Messages does.
                        Notifier.postMessage(context, thread, intent.getStringExtra("name") ?: "Claude",
                            intent.getStringExtra("text") ?: "", 0, fromMe = typed)
                    }
                }
            } catch (e: Exception) {
                android.os.Handler(context.mainLooper).post {
                    Toast.makeText(context, "Could not reach your Mac", Toast.LENGTH_LONG).show()
                }
            } finally {
                pending.finish()
            }
        }.start()
    }
}
