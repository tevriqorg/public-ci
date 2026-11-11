package tech.bogomolov.incomingsmsgateway;

import android.content.Context;

/** User-controlled status-only encryption; independent of SMS/notifications. */
public final class P22DeviceStatusSettings {
    private P22DeviceStatusSettings() {}
    private static final String NAME="p22_device_status";
    public static boolean enabled(Context ctx){
        return ctx.getSharedPreferences(NAME,Context.MODE_PRIVATE)
                .getBoolean("enabled",false);
    }
    public static void record(Context ctx,String event){
        // Metadata-only diagnostics. Never persist payloads, keys, phone numbers or OTPs.
        if(!("queued".equals(event)||"success".equals(event)||
             "failure".equals(event)||"no_rule".equals(event)||
             "prepare_failed".equals(event)))return;
        ctx.getSharedPreferences(NAME,Context.MODE_PRIVATE).edit()
                .putLong("last_"+event,System.currentTimeMillis()).apply();
    }
    public static long last(Context ctx,String event){
        return ctx.getSharedPreferences(NAME,Context.MODE_PRIVATE)
                .getLong("last_"+event,0);
    }
    public static void setEnabled(Context ctx,boolean value){
        ctx.getSharedPreferences(NAME,Context.MODE_PRIVATE)
                .edit().putBoolean("enabled",value).apply();
    }
}
