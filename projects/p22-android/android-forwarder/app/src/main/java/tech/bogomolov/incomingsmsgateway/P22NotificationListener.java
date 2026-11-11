package tech.bogomolov.incomingsmsgateway;

import android.app.Notification;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Log;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.RejectedExecutionException;

/**
 * Captures only system-delivered notification text after explicit notification
 * access and the app's own forwarding opt-in. Never uses Accessibility/Shizuku
 * to bypass Android's OTP or private-notification redaction.
 */
public class P22NotificationListener extends NotificationListenerService {
    // Listener callbacks run on the system/UI thread. HPKE and WorkManager I/O
    // are moved to a bounded worker to avoid ANR during bursts.
    private final ThreadPoolExecutor forwarder = new ThreadPoolExecutor(
            1, 1, 0L, TimeUnit.MILLISECONDS,
            new ArrayBlockingQueue<>(64),
            job -> new Thread(job, "P22NotificationEncrypt"));

    @Override
    public void onDestroy() {
        forwarder.shutdown();
        super.onDestroy();
    }

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        NotificationSettings.markListenerConnected(this);
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || sbn.getNotification() == null) return;
        NotificationSettings.markPost(this);
        Notification notification = sbn.getNotification();
        boolean summary = (notification.flags & Notification.FLAG_GROUP_SUMMARY) != 0;
        boolean ongoing = (notification.flags & Notification.FLAG_ONGOING_EVENT) != 0
                || (notification.flags & Notification.FLAG_FOREGROUND_SERVICE) != 0;
        if (!NotificationPolicy.accepts(sbn.getPackageName(), getPackageName(),
                NotificationSettings.enabled(this), summary, ongoing,
                NotificationSettings.blocked(this), NotificationSettings.allowed(this))) return;
        Bundle extras = notification.extras;
        if (extras == null) return;
        String title = NotificationPolicy.bounded(
                extras.getCharSequence(Notification.EXTRA_TITLE), 256);
        CharSequence expanded = extras.getCharSequence(Notification.EXTRA_BIG_TEXT);
        if (expanded == null || expanded.length() == 0)
            expanded = extras.getCharSequence(Notification.EXTRA_TEXT);
        if (expanded == null || expanded.length() == 0) {
            CharSequence[] lines = extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
            if (lines != null && lines.length > 0) {
                StringBuilder text = new StringBuilder();
                for (CharSequence line : lines) {
                    if (line != null) text.append(line).append('\n');
                    if (text.length() >= 1200) break;
                }
                expanded = text;
            }
        }
        // 1200 characters keeps even 3-byte text below WorkManager's 10KB
        // job-data limit after HPKE/JSON encoding.
        String content = NotificationPolicy.bounded(expanded, 1200);
        if (title.isEmpty() && content.isEmpty()
                || !P22NotificationForwarder.hasEligibleRule(this)) return;
        final String digest = NotificationPolicy.fingerprint(sbn.getPackageName(),
                sbn.getKey(), sbn.getPostTime(), title, content);
        String appName = sbn.getPackageName();
        try {
            CharSequence label = getPackageManager().getApplicationLabel(
                    getPackageManager().getApplicationInfo(sbn.getPackageName(), 0));
            if (label != null) appName = NotificationPolicy.bounded(label, 80);
        } catch (Exception ignored) {
            // Package name is a reliable fallback even if label lookup is blocked.
        }
        final String finalAppName = appName;
        final String sourcePackage = sbn.getPackageName();
        final long postedAt = sbn.getPostTime();
        try {
            forwarder.execute(() -> {
                if (!NotificationSettings.enabled(this)
                        || !P22NotificationForwarder.hasEligibleRule(this)
                        || NotificationSettings.seenRecently(this, digest)) return;
                try {
                    if (P22NotificationForwarder.enqueue(this, sourcePackage,
                            finalAppName, title, content, postedAt) > 0)
                        NotificationSettings.markEnqueued(this);
                } catch (Exception error) {
                    Log.e("P22Notification", "Notification was not queued; no content logged");
                }
            });
        } catch (RejectedExecutionException overflow) {
            Log.w("P22Notification", "Queue full; no notification content logged");
        }
    }
}
