package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;

import org.junit.Test;

public class NotificationPolicyTest {
    private boolean accepts(String pkg, boolean enabled, boolean summary,
                            boolean ongoing, String blocked, String allowed) {
        return NotificationPolicy.accepts(pkg,"tech.bogomolov.incomingsmsgateway",
                enabled,summary,ongoing,blocked,allowed);
    }

    @Test public void requiresOptInAndExcludesSelfSystemNoise() {
        assertFalse(accepts("com.tencent.mm",false,false,false,"",""));
        assertFalse(accepts("tech.bogomolov.incomingsmsgateway",true,false,false,"",""));
        assertFalse(accepts("com.tencent.mm",true,true,false,"",""));
        assertFalse(accepts("com.tencent.mm",true,false,true,"",""));
        assertTrue(accepts("com.tencent.mm",true,false,false,"",""));
    }

    @Test public void allowlistAndDenylistCannotFailOpen() {
        assertTrue(accepts("com.tencent.mm",true,false,false,"", "com.tencent.mm,com.tencent.mobileqq"));
        assertFalse(accepts("com.android.mms",true,false,false,"", "com.tencent.mm"));
        assertFalse(accepts("com.tencent.mm",true,false,false,"com.tencent.mm", ""));
        assertFalse(accepts("com.tencent.mm",true,false,false,"com.tencent.mm", "com.tencent.mm"));
        assertFalse(accepts("com.tencent.mm",true,false,false,"", "not-a-package"));
        assertFalse(accepts("com.tencent.mm",true,false,false,"", "com.example.bank"));
        assertTrue(accepts("com.android.mms",true,false,false,"", "com.android.mms"));
        assertTrue(accepts("COM.TENCENT.MM",true,false,false,"", "com.tencent.mm"));
    }

    @Test public void contentIsBoundedAndHashDoesNotLeakIt() {
        assertEquals("abcd",NotificationPolicy.bounded(" abcdef \u0000",4));
        assertEquals("",NotificationPolicy.bounded(null,200));
        String original="secret-notification-token-0001";
        String fingerprint=NotificationPolicy.fingerprint("com.test.app","key",100L,"Title",original);
        assertEquals(32,fingerprint.length());
        assertFalse(fingerprint.contains("secret"));
        assertEquals(fingerprint,NotificationPolicy.fingerprint("com.test.app","key",100L,"Title",original));
        assertNotEquals(fingerprint,NotificationPolicy.fingerprint("com.test.app","key",100L,"Title",original+"2"));
    }
}
