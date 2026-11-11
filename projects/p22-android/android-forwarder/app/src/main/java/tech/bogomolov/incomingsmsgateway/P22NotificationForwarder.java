package tech.bogomolov.incomingsmsgateway;

import android.content.Context;
import android.util.Log;

import androidx.work.Data;

import org.json.JSONObject;

import java.util.List;

/** E2EE-only transport. Only an existing, explicitly enabled P22 rule is used. */
public final class P22NotificationForwarder {
    private P22NotificationForwarder() { }

    public static boolean hasEligibleRule(Context context) {
        for (ForwardingConfig rule : ForwardingConfig.getAll(context)) {
            if (isEligible(rule)) return true;
        }
        return false;
    }

    public static int enqueue(Context context, String packageName, String appName,
                              String title, String content, long postTime) {
        List<ForwardingConfig> configs = ForwardingConfig.getAll(context);
        int enqueued = 0;
        for (ForwardingConfig config : configs) {
            if (!isEligible(config)) continue;
            try {
                JSONObject json = new JSONObject(config.getHeaders());
                String device = json.getString("X-PXT9-Recipient-Device");
                String publicKey = json.getString("X-PXT9-Recipient-Key");
                String envelope = Pxt9Crypto.encryptNotification(
                        packageName, appName, title, content, postTime, device, publicKey);
                json.remove("X-PXT9-Recipient-Device");
                json.remove("X-PXT9-Recipient-Key");
                Data payload = new Data.Builder()
                        .putString(RequestWorker.DATA_URL, config.getUrl())
                        .putString(RequestWorker.DATA_TEXT, envelope)
                        .putString(RequestWorker.DATA_HEADERS, json.toString())
                        .putBoolean(RequestWorker.DATA_PXT9_MODE, true)
                        .putBoolean(RequestWorker.DATA_IGNORE_SSL, false)
                        .putBoolean(RequestWorker.DATA_CHUNKED_MODE, config.getChunkedMode())
                        .putInt(RequestWorker.DATA_MAX_RETRIES, config.getRetriesNumber())
                        .putBoolean(RequestWorker.DATA_SIGN_HMAC_SHA256, true)
                        .putString(RequestWorker.DATA_SIGN_HMAC_SHA256_SECRET,
                                config.getSignHmacSha256Secret())
                        .putBoolean(RequestWorker.DATA_STORE_FAILED, true)
                        .putBoolean(RequestWorker.DATA_LOCAL_MODE, config.getLocalMode())
                        .build();
                RequestWorker.enqueue(context, payload);
                enqueued++;
            } catch (Exception error) {
                // Never log app notification title, content, headers or secrets.
                Log.e("P22Notification", "Unable to encrypt/enqueue app notification; no plaintext logged");
            }
        }
        return enqueued;
    }

    static boolean isEligible(ForwardingConfig config) {
        return config != null && config.getIsSmsEnabled()
                && config.getKey() != null
                && config.getKey().startsWith("p22-sender-")
                && "https://relay.p22.example.invalid/api/android-canary".equals(config.getUrl())
                && config.getSignHmacSha256()
                && config.getSignHmacSha256Secret() != null
                && !config.getSignHmacSha256Secret().isEmpty()
                && config.getHeaders() != null
                && config.getHeaders().contains("X-PXT9-Sender-ID");
    }
}
