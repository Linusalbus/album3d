package dk.linusalbus.claudepocket

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.widget.Toast
import java.net.HttpURLConnection
import java.net.URL

/** Over-the-air updates: downloads the newest APK from the relay and installs it over this one. */
object Updater {
    fun installedVersion(c: Context): Long =
        c.packageManager.getPackageInfo(c.packageName, 0).longVersionCode

    fun start(activity: Activity) {
        val pm = activity.packageManager
        if (!pm.canRequestPackageInstalls()) {
            Toast.makeText(activity, "Allow Claude Pocket to install updates, then go back and tap Install again", Toast.LENGTH_LONG).show()
            activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}")))
            return
        }
        val p = Pairing(activity)
        if (!p.paired) return
        Toast.makeText(activity, "Downloading update…", Toast.LENGTH_SHORT).show()
        val app = activity.applicationContext
        Thread {
            try {
                val installer = app.packageManager.packageInstaller
                val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
                    setAppPackageName(app.packageName)
                    if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
                }
                val id = installer.createSession(params)
                installer.openSession(id).use { session ->
                    val c = URL("${p.url}/api/app/apk").openConnection() as HttpURLConnection
                    c.setRequestProperty("Authorization", "Bearer ${p.token}")
                    c.connectTimeout = 15000
                    c.readTimeout = 60000
                    if (c.responseCode != 200) throw Exception("the Mac answered ${c.responseCode}")
                    session.openWrite("ClaudePocket.apk", 0, c.contentLengthLong.takeIf { it > 0 } ?: -1).use { out ->
                        c.inputStream.use { it.copyTo(out) }
                        session.fsync(out)
                    }
                    c.disconnect()
                    val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                    val done = PendingIntent.getBroadcast(app, 7, Intent(app, InstallReceiver::class.java), flags)
                    session.commit(done.intentSender)
                }
            } catch (e: Exception) {
                activity.runOnUiThread { Toast.makeText(activity, "Update failed: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }
}

/** Receives the installer's result; asks the user to confirm when Android requires it. */
class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT) ?: return
                context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
            PackageInstaller.STATUS_SUCCESS -> Unit // the app restarts on the new version
            else -> Toast.makeText(context, "Update failed: ${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)}", Toast.LENGTH_LONG).show()
        }
    }
}
