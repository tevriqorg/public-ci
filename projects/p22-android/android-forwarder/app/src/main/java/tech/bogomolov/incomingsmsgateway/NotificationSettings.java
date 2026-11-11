package tech.bogomolov.incomingsmsgateway;

import android.app.NotificationManager;
import android.content.ComponentName;
import android.content.Context;
import android.os.Build;
import android.content.SharedPreferences;
import android.provider.Settings;
import android.text.TextUtils;

import java.util.Map;

/** Separate, explicit user opt-in; independent of Android notification display permission. */
public final class NotificationSettings {
    private static final String PREFS = "p22_notification_settings";
    private static final String ENABLED = "forward_app_notifications";
    private static final String BLOCKED = "blocked_packages";
    private static final String ALLOWED = "allowed_packages";
    private static final String LAST_POST = "last_notification_post";
    private static final String LAST_ENQUEUE = "last_notification_enqueued";
    private static final String LAST_SMS = "last_sms_received";
    private static final String DUP_PREFIX = "dedup_";
    private static final long DEDUP_MS = 90_000L;
    private static final int MAX_DEDUP = 100;

    private NotificationSettings() { }

    public static boolean enabled(Context ctx) {
        return prefs(ctx).getBoolean(ENABLED, false);
    }

    public static String blocked(Context ctx) {
        return prefs(ctx).getString(BLOCKED, "");
    }

    public static String allowed(Context ctx) {
        return prefs(ctx).getString(ALLOWED, "");
    }

    public static void save(Context ctx, boolean enabled, String blocked, String allowed) {
        prefs(ctx).edit().putBoolean(ENABLED, enabled)
                .putString(BLOCKED, blocked == null ? "" : blocked.trim())
                .putString(ALLOWED, allowed == null ? "" : allowed.trim()).apply();
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static boolean accessGranted(Context ctx) {
        if (Build.VERSION.SDK_INT >= 27) {
            NotificationManager manager = ctx.getSystemService(NotificationManager.class);
            return manager != null && manager.isNotificationListenerAccessGranted(
                    new ComponentName(ctx, P22NotificationListener.class));
        }
        String enabled = Settings.Secure.getString(ctx.getContentResolver(),
                "enabled_notification_listeners");
        return !TextUtils.isEmpty(enabled)
                && enabled.contains(ctx.getPackageName() + "/");
    }

    public static void markListenerConnected(Context ctx) {
        prefs(ctx).edit().putLong("listener_connected", System.currentTimeMillis()).apply();
    }

    public static long lastListenerConnected(Context ctx) {
        return prefs(ctx).getLong("listener_connected", 0);
    }

    public static void markPost(Context ctx) {
        prefs(ctx).edit().putLong(LAST_POST, System.currentTimeMillis()).apply();
    }

    public static void markEnqueued(Context ctx) {
        prefs(ctx).edit().putLong(LAST_ENQUEUE, System.currentTimeMillis()).apply();
    }

    public static void markSms(Context ctx) {
        prefs(ctx).edit().putLong(LAST_SMS, System.currentTimeMillis()).apply();
    }

    public static long lastPost(Context ctx) {
        return prefs(ctx).getLong(LAST_POST, 0);
    }

    public static long lastEnqueued(Context ctx) {
        return prefs(ctx).getLong(LAST_ENQUEUE, 0);
    }

    public static long lastSms(Context ctx) {
        return prefs(ctx).getLong(LAST_SMS, 0);
    }

    public static void markDeliveryAttempt(Context ctx) {
        prefs(ctx).edit().putLong("delivery_attempt", System.currentTimeMillis()).apply();
    }
    public static void markDeliverySuccess(Context ctx) {
        prefs(ctx).edit().putLong("delivery_success", System.currentTimeMillis()).apply();
    }
    public static void markDeliveryFailure(Context ctx) {
        prefs(ctx).edit().putLong("delivery_failure", System.currentTimeMillis()).apply();
    }
    public static long lastDeliveryAttempt(Context ctx) {
        return prefs(ctx).getLong("delivery_attempt", 0);
    }
    public static long lastDeliverySuccess(Context ctx) {
        return prefs(ctx).getLong("delivery_success", 0);
    }
    public static long lastDeliveryFailure(Context ctx) {
        return prefs(ctx).getLong("delivery_failure", 0);
    }

    /** Suppress repeated notification updates including process restarts. */
    public static synchronized boolean seenRecently(Context ctx, String fingerprint) {
        SharedPreferences p = prefs(ctx);
        String key = DUP_PREFIX + fingerprint;
        long now = System.currentTimeMillis();
        long previous = p.getLong(key, 0);
        if (previous > 0 && now - previous >= 0 && now - previous < DEDUP_MS)
            return true;
        SharedPreferences.Editor edit = p.edit().putLong(key, now);
        int count = 0;
        for (Map.Entry<String, ?> entry : p.getAll().entrySet()) {
            if (!entry.getKey().startsWith(DUP_PREFIX) || entry.getKey().equals(key)) continue;
            count++;
            Object value = entry.getValue();
            if (!(value instanceof Long) || now - (Long) value > DEDUP_MS)
                edit.remove(entry.getKey());
        }
        if (count > MAX_DEDUP) {
            // Fall back to clearing stale fingerprints, preserving only latest.
            for (String entryKey : p.getAll().keySet())
                if (entryKey.startsWith(DUP_PREFIX) && !entryKey.equals(key))
                    edit.remove(entryKey);
        }
        edit.apply();
        return false;
    }
}
