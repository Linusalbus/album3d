package dk.linusalbus.claudepocket

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.os.Build
import org.json.JSONObject

object Notifier {
    const val STATUS_ID = 1
    private const val CH_STATUS = "status"
    private const val CH_APPROVALS = "approvals"
    private const val CH_MESSAGES = "messages"
    const val REPLY_KEY = "reply"
    private const val ACCENT = 0xFFD97757.toInt()

    private fun nm(c: Context) = c.getSystemService(NotificationManager::class.java)

    fun ensureChannels(c: Context) {
        nm(c).createNotificationChannels(listOf(
            NotificationChannel(CH_APPROVALS, "Approvals and questions", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Claude Code needs a permission, an answer or a reply"
            },
            NotificationChannel(CH_MESSAGES, "Messages", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Updates Claude sends you"
            },
            NotificationChannel(CH_STATUS, "Connection", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shows that Claude Pocket is connected. You can hide this channel."
                setShowBadge(false)
            },
        ))
    }

    fun status(c: Context, text: String): Notification =
        Notification.Builder(c, CH_STATUS)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(ACCENT)
            .setContentTitle("Claude Pocket")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open(c, "#/inbox", 0))
            .build()

    fun updateStatus(c: Context, text: String) = nm(c).notify(STATUS_ID, status(c, text))

    private fun open(c: Context, route: String, code: Int): PendingIntent {
        val i = Intent(c, MainActivity::class.java)
            .putExtra("route", route)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(c, code, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun action(c: Context, code: Int, extras: Intent.() -> Unit, mutable: Boolean = false): PendingIntent {
        val i = Intent(c, ActionReceiver::class.java).apply(extras)
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (mutable && Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else if (mutable) 0 else PendingIntent.FLAG_IMMUTABLE)
        return PendingIntent.getBroadcast(c, code, i, flags)
    }

    private fun replyAction(c: Context, code: Int, label: String, extras: Intent.() -> Unit): Notification.Action {
        val input = RemoteInput.Builder(REPLY_KEY).setLabel(label).build()
        return Notification.Action.Builder(null, "Reply", action(c, code, extras, mutable = true))
            .addRemoteInput(input)
            .setAllowGeneratedReplies(false)
            .build()
    }

    fun requestNotificationId(id: String) = 1000 + (id.hashCode() and 0x3fffffff) % 1_000_000
    fun threadNotificationId(id: String) = 2_000_000 + (id.hashCode() and 0x3fffffff) % 1_000_000

    fun cancel(c: Context, id: Int) = nm(c).cancel(id)

    /** Diffs the relay state against what was already shown and posts what is new. */
    @Synchronized
    fun process(c: Context, p: Pairing, state: JSONObject) {
        val first = !p.primed
        val show = !first && !MainActivity.visible

        // ---- approvals, questions, finished turns
        val requests = state.optJSONArray("requests")
        val open = mutableSetOf<String>()
        val seen = p.seenRequests.toMutableSet()
        for (i in 0 until (requests?.length() ?: 0)) {
            val r = requests!!.getJSONObject(i)
            val id = r.getString("id")
            open.add(id)
            if (id !in seen) {
                seen.add(id)
                if (show) postRequest(c, r)
            }
        }
        // Answered elsewhere (on the Mac or in the app): clear the notification.
        for (id in seen - open) cancel(c, requestNotificationId(id))
        p.seenRequests = seen.intersect(open)

        // ---- messages Claude sent
        val threads = state.optJSONArray("threads")
        var newest = p.lastMessageTs
        for (i in 0 until (threads?.length() ?: 0)) {
            val t = threads!!.getJSONObject(i)
            val last = t.optJSONObject("last") ?: continue
            val ts = last.optLong("ts")
            val unread = t.optInt("unread")
            if (unread == 0) cancel(c, threadNotificationId(t.getString("id")))
            if (ts > p.lastMessageTs && last.optString("from") == "claude" && unread > 0 && show) {
                postMessage(c, t.getString("id"), t.getString("name"), last.optString("text"), unread)
            }
            if (ts > newest) newest = ts
        }
        p.lastMessageTs = newest
        p.primed = true
    }

    private fun postRequest(c: Context, r: JSONObject) {
        val id = r.getString("id")
        val nid = requestNotificationId(id)
        val session = r.optString("sessionName").ifBlank { "Claude Code" }
        val payload = r.optJSONObject("payload") ?: JSONObject()
        val b = Notification.Builder(c, CH_APPROVALS)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(ACCENT)
            .setSubText(session)
            .setAutoCancel(true)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setContentIntent(open(c, "#/inbox", nid))

        when (r.optString("kind")) {
            "permission" -> {
                val input = payload.optJSONObject("input") ?: JSONObject()
                val detail = listOf("command", "file_path", "url", "pattern", "description", "prompt")
                    .firstNotNullOfOrNull { k -> input.optString(k).takeIf { it.isNotBlank() } } ?: input.toString()
                b.setContentTitle("Allow ${payload.optString("tool")}?")
                    .setContentText(detail)
                    .setStyle(Notification.BigTextStyle().bigText(detail.take(1500)))
                    .addAction(Notification.Action.Builder(null, "Allow", action(c, nid * 4, {
                        putExtra("op", "answer"); putExtra("id", id); putExtra("answer", """{"allow":true}"""); putExtra("nid", nid)
                    })).build())
                    .addAction(Notification.Action.Builder(null, "Deny", action(c, nid * 4 + 1, {
                        putExtra("op", "answer"); putExtra("id", id); putExtra("answer", """{"allow":false}"""); putExtra("nid", nid)
                    })).build())
            }
            "question" -> {
                val q = payload.optJSONArray("questions")?.optJSONObject(0)
                b.setContentTitle("Claude has a question")
                    .setContentText(q?.optString("question") ?: "Tap to answer")
                    .setStyle(Notification.BigTextStyle().bigText(q?.optString("question") ?: ""))
            }
            else -> { // stop: Claude finished and waits for a reply
                val last = payload.optString("last").ifBlank { "Finished." }
                b.setContentTitle("Claude is done — reply?")
                    .setContentText(last)
                    .setStyle(Notification.BigTextStyle().bigText(last.take(2000)))
                    .addAction(replyAction(c, nid * 4 + 2, "Reply to Claude") {
                        putExtra("op", "stop-reply"); putExtra("id", id); putExtra("nid", nid)
                    })
                    .addAction(Notification.Action.Builder(null, "Done", action(c, nid * 4 + 3, {
                        putExtra("op", "answer"); putExtra("id", id); putExtra("answer", """{"done":true}"""); putExtra("nid", nid)
                    })).build())
            }
        }
        nm(c).notify(nid, b.build())
    }

    fun postMessage(c: Context, threadId: String, name: String, text: String, unread: Int, fromMe: String? = null) {
        val nid = threadNotificationId(threadId)
        val me = Person.Builder().setName("You").build()
        val claude = Person.Builder().setName(name).setKey(threadId).build()
        val style = Notification.MessagingStyle(me).setConversationTitle(null)
        style.addMessage(Notification.MessagingStyle.Message(text, System.currentTimeMillis(), claude))
        if (fromMe != null) style.addMessage(Notification.MessagingStyle.Message(fromMe, System.currentTimeMillis(), null as Person?))
        val b = Notification.Builder(c, CH_MESSAGES)
            .setSmallIcon(R.drawable.ic_stat)
            .setColor(ACCENT)
            .setStyle(style)
            .setNumber(unread)
            .setAutoCancel(true)
            .setOnlyAlertOnce(fromMe != null)
            .setCategory(Notification.CATEGORY_MESSAGE)
            .setContentIntent(open(c, "#/m/$threadId", nid))
            .addAction(replyAction(c, nid * 2, "Message") {
                putExtra("op", "thread-reply"); putExtra("thread", threadId); putExtra("name", name)
                putExtra("text", text); putExtra("nid", nid)
            })
        nm(c).notify(nid, b.build())
    }
}
