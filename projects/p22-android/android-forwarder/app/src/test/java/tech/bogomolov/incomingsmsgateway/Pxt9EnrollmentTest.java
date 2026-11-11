package tech.bogomolov.incomingsmsgateway;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class Pxt9EnrollmentTest {
    private static final String SENDER = "12345678-1234-4123-8123-123456789abc";
    private static final String RECIPIENT = "12345678-1234-4123-8123-123456789abd";

    private String validRule() throws JSONException {
        JSONObject headers = new JSONObject()
                .put("X-PXT9-Sender-ID", SENDER)
                .put("X-PXT9-Recipient-Device", RECIPIENT)
                .put("X-PXT9-Recipient-Key", new String(new char[87]).replace('\0', 'A'));
        JSONObject rule = new JSONObject()
                .put("key", "p22-sender-" + SENDER)
                .put("sender", "*")
                .put("url", "https://relay.p22.example.invalid/api/android-canary")
                .put("is_sms_enabled", false)
                .put("ignore_ssl", false)
                .put("sign_hmac_sha256", true)
                .put("sign_hmac_sha256_secret", new String(new char[43]).replace('\0', 'a'))
                .put("headers", headers.toString());
        return new JSONArray().put(rule).toString();
    }

    @Test public void qrOnlyHasShortTicketAndTrustedOrigin() {
        String token = new String(new char[43]).replace('\0', 'a');
        String qr = "https://relay.p22.example.invalid/setup/android/" + token;
        assertEquals(token, Pxt9Enrollment.ticketFromQr(qr));
    }

    @Test public void rejectCrossSiteOrMalformedQr() {
        String token = new String(new char[43]).replace('\0', 'a');
        for (String bad : new String[]{
                "http://relay.p22.example.invalid/setup/android/" + token,
                "https://attacker.example/setup/android/" + token,
                "https://relay.p22.example.invalid.evil.example/setup/android/" + token,
                "https://relay.p22.example.invalid:444/setup/android/" + token,
                "https://relay.p22.example.invalid/setup/android/" + token + "/download",
                "https://relay.p22.example.invalid/setup/android/" + token + "?x=1",
                "https://relay.p22.example.invalid/setup/android/" + token + "#x",
                "https://person@relay.p22.example.invalid/setup/android/" + token
        }) {
            try { Pxt9Enrollment.ticketFromQr(bad); fail(bad); }
            catch (IllegalArgumentException expected) { }
        }
    }

    @Test public void validSenderBoundDisabledRule() throws JSONException {
        JSONObject rule = Pxt9Enrollment.validate(validRule());
        assertFalse(rule.getBoolean("is_sms_enabled"));
        assertEquals("p22-sender-" + SENDER, rule.getString("key"));
    }

    @Test public void rejectsEnabledPlaintextAndWrongDestination() throws JSONException {
        JSONObject base = new JSONArray(validRule()).getJSONObject(0);
        for (String mutation : new String[]{"enabled", "unsigned", "nonTls", "differentHost", "noSender", "differentKey"}) {
            JSONObject rule = new JSONObject(base.toString());
            switch (mutation) {
                case "enabled": rule.put("is_sms_enabled", true); break;
                case "unsigned": rule.put("sign_hmac_sha256", false); break;
                case "nonTls": rule.put("ignore_ssl", true); break;
                case "differentHost": rule.put("url", "https://evil.invalid/api/inbox"); break;
                case "differentKey": rule.put("key", "p22-sender-"+RECIPIENT); break;
                case "noSender": rule.put("headers", "{}"); break;
            }
            try { Pxt9Enrollment.validate(new JSONArray().put(rule).toString()); fail(mutation); }
            catch (JSONException expected) { }
        }
        try {
            Pxt9Enrollment.validate(new JSONArray().put(base).put(base).toString());
            fail("multi");
        } catch (JSONException expected) { }
    }
}
