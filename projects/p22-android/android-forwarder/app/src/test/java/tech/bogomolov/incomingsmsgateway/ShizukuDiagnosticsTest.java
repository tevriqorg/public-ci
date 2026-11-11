package tech.bogomolov.incomingsmsgateway;

import static org.junit.Assert.*;
import org.junit.Test;

public class ShizukuDiagnosticsTest {
    @Test public void optionalAndDisconnectedCannotBeMistakenForAuthorized() {
        assertTrue(ShizukuDiagnostics.describe(false, false, false, false, -1).contains("未检测到"));
        assertTrue(ShizukuDiagnostics.describe(true, false, false, true, 0).contains("未连接"));
        assertTrue(ShizukuDiagnostics.describe(true, true, true, true, 0).contains("版本过旧"));
    }

    @Test public void privilegeLabelDoesNotClaimRootForAdb() {
        String adb = ShizukuDiagnostics.describe(true, true, false, true, 2000);
        assertTrue(adb.contains("ADB shell"));
        assertFalse(adb.contains("root"));
        assertTrue(adb.contains("不执行特权命令"));
        assertTrue(ShizukuDiagnostics.describe(true, true, false, false, 2000).contains("未授权"));
    }
}
