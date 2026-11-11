package tech.bogomolov.incomingsmsgateway;

import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.UUID;
import java.util.regex.Pattern;

import javax.net.ssl.HttpsURLConnection;

/**
 * Owner-authorized QR pairing. The QR has only a 2-minute ticket.
 * The 32-byte sender HMAC key is generated ON the Android device and is sent
 * once over authenticated HTTPS to the server. No production/global key
 * is available via the QR or claim response.
 */
public final class Pxt9Enrollment {
    private static final Pattern TOKEN = Pattern.compile("[A-Za-z0-9_-]{40,64}");
    private static final Pattern UUID_RE = Pattern.compile(
            "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern PUBLIC_KEY = Pattern.compile("[A-Za-z0-9_-]{87}");
    private static final String ENDPOINT = "https://relay.p22.example.invalid/api/android-canary";
    private static final String CLAIM = "https://relay.p22.example.invalid/api/enroll/claim";
    private static final String PATH_PREFIX = "/setup/android/";
    private static final int MAX_BYTES = 16384;

    private Pxt9Enrollment() { }

    public static String ticketFromQr(String scannedText) {
        if (scannedText == null || scannedText.length() > 512)
            throw new IllegalArgumentException("Invalid QR");
        final URI uri;
        try {
            uri = URI.create(scannedText.trim());
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("Invalid QR");
        }
        String path = uri.getRawPath();
        if (!"https".equals(uri.getScheme())
                || !"relay.p22.example.invalid".equals(uri.getHost())
                || (uri.getPort() != -1 && uri.getPort() != 443)
                || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                || uri.getRawFragment() != null || path == null
                || !path.startsWith(PATH_PREFIX)
                || !TOKEN.matcher(path.substring(PATH_PREFIX.length())).matches()) {
            throw new IllegalArgumentException("Untrusted QR");
        }
        return path.substring(PATH_PREFIX.length());
    }

    /** Claim only after user confirms the preview. Never write the secret to Downloads. */
    public static String claimAndBuildRule(String qr) throws Exception {
        String token = ticketFromQr(qr);
        String senderId = UUID.randomUUID().toString();
        byte[] key = new byte[32];
        new SecureRandom().nextBytes(key);
        String localSecret = Base64.encodeToString(key,
                Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
        JSONObject request = new JSONObject();
        request.put("ticket", token);
        request.put("senderId", senderId);
        request.put("senderSecret", localSecret);
        byte[] body = request.toString().getBytes(StandardCharsets.UTF_8);
        HttpsURLConnection connection = (HttpsURLConnection)
                new URI(CLAIM).toURL().openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(12000);
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setRequestProperty("X-Personal-Relay-Pairing", "android-v1");
        connection.setDoOutput(true);
        connection.setFixedLengthStreamingMode(body.length);
        try {
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body);
            }
            if (connection.getResponseCode() != 200)
                throw new IllegalArgumentException("Pairing expired or already used");
            if (connection.getContentLength() > MAX_BYTES)
                throw new IllegalArgumentException("Pairing response too large");
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            try (InputStream in = connection.getInputStream()) {
                byte[] chunk = new byte[2048];
                int n;
                while ((n = in.read(chunk)) != -1) {
                    if (out.size() + n > MAX_BYTES)
                        throw new IllegalArgumentException("Pairing response too large");
                    out.write(chunk, 0, n);
                }
            }
            JSONObject response = new JSONObject(
                    new String(out.toByteArray(), StandardCharsets.UTF_8));
            if (!senderId.equals(response.getString("senderId"))
                    || !ENDPOINT.equals(response.getString("endpoint"))
                    || !UUID_RE.matcher(response.getString("recipientDeviceId")).matches()
                    || !PUBLIC_KEY.matcher(response.getString("recipientPublicKey")).matches()) {
                throw new JSONException("Unexpected pairing response");
            }
            JSONObject headers = new JSONObject();
            headers.put("User-Agent", "PersonalRelay-Android-QR");
            headers.put("X-PXT9-Sender-ID", senderId);
            headers.put("X-PXT9-Recipient-Device", response.getString("recipientDeviceId"));
            headers.put("X-PXT9-Recipient-Key", response.getString("recipientPublicKey"));

            JSONObject config = new JSONObject();
            config.put("key", "p22-sender-" + senderId);
            config.put("sender", "*");
            config.put("is_sender_regex", false);
            config.put("sms_filter", "");
            config.put("url", ENDPOINT);
            config.put("sim_slot", 0);
            config.put("template",
                    "{\"from\":\"%from%\",\"text\":\"%text%\",\"sentStamp\":%sentStamp%,"
                    + "\"receivedStamp\":%receivedStamp%,\"sim\":\"%sim%\"}");
            config.put("headers", headers.toString());
            config.put("retries_number", 10);
            config.put("ignore_ssl", false);
            config.put("chunked_mode", false);
            config.put("is_sms_enabled", false);
            config.put("sign_hmac_sha256", true);
            config.put("sign_hmac_sha256_secret", localSecret);
            config.put("store_failed", true);
            config.put("local_mode", false);
            String result = new JSONArray().put(config).toString();
            validate(result);
            return result;
        } finally {
            connection.disconnect();
        }
    }

    public static JSONObject validate(String content) throws JSONException {
        JSONArray array = new JSONArray(content);
        if (array.length() != 1) throw new JSONException("Expected one rule");
        JSONObject rule = array.getJSONObject(0);
        String key = rule.getString("key");
        if (!key.matches("p22-sender-[0-9a-f-]{36}")
                || !"*".equals(rule.getString("sender"))
                || !ENDPOINT.equals(rule.getString("url"))
                || rule.getBoolean("is_sms_enabled") || rule.getBoolean("ignore_ssl")
                || !rule.getBoolean("sign_hmac_sha256")
                || rule.getString("sign_hmac_sha256_secret").length() != 43) {
            throw new JSONException("Unsafe pairing rule");
        }
        JSONObject headers = new JSONObject(rule.getString("headers"));
        if (!UUID_RE.matcher(headers.getString("X-PXT9-Sender-ID")).matches()
                || !UUID_RE.matcher(headers.getString("X-PXT9-Recipient-Device")).matches()
                || !PUBLIC_KEY.matcher(headers.getString("X-PXT9-Recipient-Key")).matches()
                || !key.equals("p22-sender-" + headers.getString("X-PXT9-Sender-ID"))) {
            throw new JSONException("Invalid sender/recipient identity");
        }
        return rule;
    }
}
