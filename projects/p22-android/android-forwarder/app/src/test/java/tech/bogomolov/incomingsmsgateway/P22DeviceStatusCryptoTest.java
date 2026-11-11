package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;
import org.junit.Test;
import org.json.JSONObject;

/** Only synthetic values; never output real device status or OTP to CI. */
public class P22DeviceStatusCryptoTest {
    private static final String KEY=
        "BIKTGDD9i4Qlkep9ZZPng6Ac_nknbOEcNkPvVeO_6uLRYdd5zwBsKiZTR_PqC4Zxvqmvg4O0InE1y-PxtjQVCA0";

    @Test public void separateEncryptedStatusHasNoPlaintextMarkers() throws Exception {
        JSONObject input=new JSONObject();
        input.put("type","device_status");
        input.put("model","Synthetic Testing Phone");
        input.put("battery",76);
        String envelope=Pxt9Crypto.encryptStatus(input,"synthetic-device",KEY);
        JSONObject obj=new JSONObject(envelope);
        assertEquals(1,obj.getInt("v"));
        assertFalse(envelope.contains("Synthetic Testing Phone"));
        assertFalse(envelope.contains("device_status"));
    }
    @Test public void smsNotificationSourceIdentityIsEncrypted() throws Exception {
        String envelope=Pxt9Crypto.encryptNotification(
            "com.android.mms","短信","合成通知","只供测试的内容",
            1794362400000L,"synthetic-device",KEY,"sms_app_notification");
        assertFalse(envelope.contains("sms_app_notification"));
        assertFalse(envelope.contains("合成通知"));
        assertEquals(1,new JSONObject(envelope).getJSONArray("recipients").length());
    }
}
