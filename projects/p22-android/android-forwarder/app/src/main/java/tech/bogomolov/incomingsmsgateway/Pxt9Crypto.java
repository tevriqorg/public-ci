package tech.bogomolov.incomingsmsgateway;

import com.google.crypto.tink.AccessesPartialKey;
import com.google.crypto.tink.HybridEncrypt;
import com.google.crypto.tink.hybrid.HpkeParameters;
import com.google.crypto.tink.hybrid.HpkePublicKey;
import com.google.crypto.tink.hybrid.internal.HpkeEncrypt;
import com.google.crypto.tink.util.Bytes;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.Base64;
import java.util.UUID;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

public final class Pxt9Crypto {
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final int HPKE_P256_ENC_SIZE = 65;

    private Pxt9Crypto() {}
    private static String b64u(byte[] input) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(input);
    }

    private static byte[] decodeB64u(String input) {
        return Base64.getUrlDecoder().decode(input);
    }

    @AccessesPartialKey
    private static HybridEncrypt createHpkeEncrypt(byte[] recipientPublicKey)
            throws GeneralSecurityException {
        HpkeParameters parameters = HpkeParameters.builder()
                .setKemId(HpkeParameters.KemId.DHKEM_P256_HKDF_SHA256)
                .setKdfId(HpkeParameters.KdfId.HKDF_SHA256)
                .setAeadId(HpkeParameters.AeadId.AES_128_GCM)
                .setVariant(HpkeParameters.Variant.NO_PREFIX)
                .build();

        HpkePublicKey publicKey = HpkePublicKey.create(
                parameters,
                Bytes.copyFrom(recipientPublicKey),
                null
        );
        return HpkeEncrypt.create(publicKey);
    }
    public static String encryptSms(
            String sender, String body, long receivedAt, String sim,
            String recipientDeviceId, String recipientPublicKeyB64
    ) throws Exception {
        JSONObject plaintextJson = new JSONObject();
        plaintextJson.put("sender", sender);
        plaintextJson.put("body", body);
        plaintextJson.put("receivedAt", receivedAt);
        plaintextJson.put("sim", sim);
        return encryptPayload(plaintextJson, recipientDeviceId, recipientPublicKeyB64);
    }

    public static String encryptNotification(
            String appPackage, String appName, String title, String content,
            long receivedAt, String recipientDeviceId, String recipientPublicKeyB64
    ) throws Exception {
        return encryptNotification(appPackage, appName, title, content, receivedAt,
                recipientDeviceId, recipientPublicKeyB64, "app_notification");
    }

    public static String encryptNotification(
            String appPackage, String appName, String title, String content,
            long receivedAt, String recipientDeviceId, String recipientPublicKeyB64,
            String sourceKind
    ) throws Exception {
        JSONObject plaintextJson = new JSONObject();
        plaintextJson.put("type", "notification");
        plaintextJson.put("sourceKind", sourceKind);
        plaintextJson.put("sender", appName);
        plaintextJson.put("appName", appName);
        plaintextJson.put("appPackage", appPackage);
        plaintextJson.put("title", title);
        plaintextJson.put("body", title.isEmpty() ? content
                : (content.isEmpty() ? title : title + "\n" + content));
        plaintextJson.put("receivedAt", receivedAt);
        return encryptPayload(plaintextJson, recipientDeviceId, recipientPublicKeyB64);
    }

    public static String encryptStatus(
            JSONObject status, String recipientDeviceId, String recipientPublicKeyB64
    ) throws Exception {
        if (status == null || !"device_status".equals(status.optString("type")))
            throw new IllegalArgumentException("Invalid device status snapshot");
        return encryptPayload(status, recipientDeviceId, recipientPublicKeyB64);
    }

    /** Keep the established v1 envelope/AAD interoperable with deployed iPhone. */
    @AccessesPartialKey
    private static String encryptPayload(JSONObject plaintextJson,
                                         String recipientDeviceId,
                                         String recipientPublicKeyB64) throws Exception {
        if (recipientDeviceId == null || recipientDeviceId.isEmpty())
            throw new IllegalArgumentException("Recipient device id is empty");
        byte[] recipientPublicKey = decodeB64u(recipientPublicKeyB64);
        if (recipientPublicKey.length != 65)
            throw new IllegalArgumentException("P-256 public key must be 65 bytes");
        String id = UUID.randomUUID().toString();
        // PXT9/SMS/v1 is intentionally retained for backwards compatibility.
        // Notification-vs-SMS type lives inside the encrypted plaintext.
        byte[] aad = ("PXT9/SMS/v1/" + id).getBytes(StandardCharsets.UTF_8);
        plaintextJson.put("id", id);
        byte[] plaintext = plaintextJson.toString().getBytes(StandardCharsets.UTF_8);
        byte[] dataKeyBytes = new byte[32];
        byte[] iv = new byte[12];
        RANDOM.nextBytes(dataKeyBytes);
        RANDOM.nextBytes(iv);

        Cipher aes = Cipher.getInstance("AES/GCM/NoPadding");
        aes.init(
                Cipher.ENCRYPT_MODE,
                new SecretKeySpec(dataKeyBytes, "AES"),
                new GCMParameterSpec(128, iv)
        );
        aes.updateAAD(aad);
        byte[] ciphertext = aes.doFinal(plaintext);

        HybridEncrypt hpke = createHpkeEncrypt(recipientPublicKey);
        byte[] wrappedCombined = hpke.encrypt(dataKeyBytes, aad);
        Arrays.fill(dataKeyBytes, (byte) 0);

        if (wrappedCombined.length <= HPKE_P256_ENC_SIZE) {
            throw new GeneralSecurityException("HPKE output is too short");
        }
        byte[] hpkeEnc = Arrays.copyOfRange(wrappedCombined, 0, HPKE_P256_ENC_SIZE);
        byte[] hpkeCt = Arrays.copyOfRange(
                wrappedCombined,
                HPKE_P256_ENC_SIZE,
                wrappedCombined.length
        );
        JSONObject recipient = new JSONObject();
        recipient.put("deviceId", recipientDeviceId);
        recipient.put("enc", b64u(hpkeEnc));
        recipient.put("ct", b64u(hpkeCt));

        JSONArray recipients = new JSONArray();
        recipients.put(recipient);

        JSONObject envelope = new JSONObject();
        envelope.put("v", 1);
        envelope.put("id", id);
        envelope.put("contentAlg", "AES-256-GCM");
        envelope.put("wrapAlg", "HPKE-P256-HKDF-SHA256-AES128GCM");
        envelope.put("iv", b64u(iv));
        envelope.put("ciphertext", b64u(ciphertext));
        envelope.put("recipients", recipients);
        return envelope.toString();
    }
}
