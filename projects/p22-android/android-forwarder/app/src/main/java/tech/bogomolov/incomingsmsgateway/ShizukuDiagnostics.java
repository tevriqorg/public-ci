package tech.bogomolov.incomingsmsgateway;

import android.content.Context;
import android.content.pm.PackageManager;
import rikka.shizuku.Shizuku;

/** Optional user-controlled Shizuku inspection; never executes shell commands. */
public final class ShizukuDiagnostics {
    private ShizukuDiagnostics() { }

    public static boolean binderAvailable() {
        try { return Shizuku.pingBinder(); }
        catch (RuntimeException ignored) { return false; }
    }

    public static String status(Context ctx) {
        boolean installed = false;
        try {
            ctx.getPackageManager().getPackageInfo("moe.shizuku.privileged.api", 0);
            installed = true;
        } catch (PackageManager.NameNotFoundException ignored) {
            // Sui may also supply a binder without Shizuku app installed.
        }
        if (!binderAvailable()) return describe(installed, false, false, false, -1);
        try {
            boolean old = Shizuku.isPreV11();
            boolean granted = !old &&
                    Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
            return describe(installed, true, old, granted, Shizuku.getUid());
        } catch (RuntimeException ignored) {
            return "连接状态变化，请刷新；短信/通知转发不依赖 Shizuku";
        }
    }

    /** Pure formatter for unit testing; never includes phone or message data. */
    static String describe(boolean installed, boolean connected, boolean legacy,
                           boolean granted, int uid) {
        if (!connected) return installed
                ? "已安装但服务未连接（不影响转发）"
                : "未检测到运行中的服务（不影响转发）";
        if (legacy) return "已连接，但服务版本过旧，未使用特权 API";
        String identity = uid == 2000 ? "ADB shell" : uid == 0 ? "root" : "受限 UID";
        return "服务已连接 / " + identity + " / "
                + (granted ? "用户已授权" : "未授权")
                + "（目前仅显示状态，不执行特权命令）";
    }
}
