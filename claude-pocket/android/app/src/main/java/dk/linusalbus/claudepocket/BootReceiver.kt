package dk.linusalbus.claudepocket

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Reconnects after the phone restarts, so notifications keep working without opening the app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) startPocketService(context)
    }
}
