package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;
import org.junit.Test;
import org.json.JSONArray;
import org.json.JSONObject;

/** Synthetic only. Actual PXT9 v1 ciphertext is immutable with dual HPKE recipients. */
public class P22ArchiveCryptoTest {
    private static final String PRIMARY_KEY=
        "BIKTGDD9i4Qlkep9ZZPng6Ac_nknbOEcNkPvVeO_6uLRYdd5zwBsKiZTR_PqC4Zxvqmvg4O0InE1y-PxtjQVCA0";
    private static final String ARCHIVE_KEY=
        "BFD2lpCi5kG2tDXSzOgFSCZNbG67JeyRHaGtX4cBHT8ZgghstuT62P-fm2YGAnNkst7xKv9Dib9mz14_QHNyJpQ";
    private static final String MAIN_ID="12345678-1234-4123-8123-aaaaaaaaaaaa";
    private static final String ARCHIVE_ID="87654321-4321-4123-8123-bbbbbbbbbbbb";

    @Test public void smsReceivesTwoDistinctKeyWrapsButOneCiphertext() throws Exception {
        String json=Pxt9Crypto.encryptSms(
            "synthetic","just a test",1794362400000L,"sim1",
            MAIN_ID,PRIMARY_KEY,ARCHIVE_ID,ARCHIVE_KEY);
        JSONObject env=new JSONObject(json);
        assertEquals(1,env.getInt("v"));
        assertEquals("AES-256-GCM",env.getString("contentAlg"));
        JSONArray recipients=env.getJSONArray("recipients");
        assertEquals(2,recipients.length());
        assertEquals(MAIN_ID,recipients.getJSONObject(0).getString("deviceId"));
        assertEquals(ARCHIVE_ID,recipients.getJSONObject(1).getString("deviceId"));
        assertNotEquals(recipients.getJSONObject(0).getString("enc"),
            recipients.getJSONObject(1).getString("enc"));
        assertNotEquals(recipients.getJSONObject(0).getString("ct"),
            recipients.getJSONObject(1).getString("ct"));
        assertFalse(json.contains("just a test"));
        assertFalse(json.contains("synthetic"));
    }

    @Test public void notificationSupportsArchiveWithoutLeakingPackageOrContent() throws Exception {
        String json=Pxt9Crypto.encryptNotification(
            "org.example.app","合成应用","仅测试","合成通知",
            1794362400000L,MAIN_ID,PRIMARY_KEY,
            "app_notification",ARCHIVE_ID,ARCHIVE_KEY);
        JSONObject env=new JSONObject(json);
        assertEquals(2,env.getJSONArray("recipients").length());
        assertFalse(json.contains("合成通知"));
        assertFalse(json.contains("org.example.app"));
    }

    @Test public void existingSingleRecipientOverloadRemainsCompatible() throws Exception {
        String json=Pxt9Crypto.encryptSms(
            "synthetic","legacy",1794362400000L,"sim1",MAIN_ID,PRIMARY_KEY);
        assertEquals(1,new JSONObject(json).getJSONArray("recipients").length());
    }

    @Test public void invalidArchivePairsFailClosed() throws Exception {
        try {
            Pxt9Crypto.encryptSms("synthetic","text",0L,"sim1",
                MAIN_ID,PRIMARY_KEY,ARCHIVE_ID,null);
            fail("Must reject incomplete archive recipient");
        } catch (IllegalArgumentException expected) { }
        try {
            Pxt9Crypto.encryptSms("synthetic","text",0L,"sim1",
                MAIN_ID,PRIMARY_KEY,MAIN_ID,ARCHIVE_KEY);
            fail("Must reject identical primary and archive identities");
        } catch (IllegalArgumentException expected) { }
    }
}
