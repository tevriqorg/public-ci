package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;
import org.junit.Test;
import org.json.JSONObject;

public class P22NotificationCryptoTest {
    private static final String TEST_RECIPIENT_KEY =
            "BIKTGDD9i4Qlkep9ZZPng6Ac_nknbOEcNkPvVeO_6uLRYdd5zwBsKiZTR_PqC4Zxvqmvg4O0InE1y-PxtjQVCA0";

    @Test public void encryptedNotificationUsesSameV1EnvelopeAsSms() throws Exception {
        String title="仅供测试的聊天提醒";
        String body="synthetic-test-message-987654";
        String payload=Pxt9Crypto.encryptNotification(
                "com.example.chat","Chat App",title,body,
                1794362400000L,"interop-device",TEST_RECIPIENT_KEY);
        JSONObject outer=new JSONObject(payload);
        assertEquals(1,outer.getInt("v"));
        assertEquals("AES-256-GCM",outer.getString("contentAlg"));
        assertEquals("HPKE-P256-HKDF-SHA256-AES128GCM",outer.getString("wrapAlg"));
        assertEquals(1,outer.getJSONArray("recipients").length());
        assertFalse(payload.contains(title));
        assertFalse(payload.contains(body));
        assertFalse(payload.contains("com.example.chat"));
        assertFalse(payload.contains("notification"));
    }
}
