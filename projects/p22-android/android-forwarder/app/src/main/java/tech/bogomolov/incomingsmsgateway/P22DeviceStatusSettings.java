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
    public static void setEnabled(Context ctx,boolean value){
        ctx.getSharedPreferences(NAME,Context.MODE_PRIVATE)
                .edit().putBoolean("enabled",value).apply();
    }
}
